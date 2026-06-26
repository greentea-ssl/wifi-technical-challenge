// R12 promiscuous test — ESP32-C5
//
// 1) STA-associates to TEAM_SSID_OPEN to lock TSF to the AP.
// 2) Enables promiscuous mode with WIFI_PROMIS_FILTER_MASK_DATA.
// 3) For each downlink data frame (addr2 == learned BSSID) the callback
//    pushes a compact record into a lock-free SPSC ring. loop() drains
//    the ring and prints CSV to Serial @ 921600.
//
// This isolates Serial.printf() from the high-priority WiFi callback,
// so we can run at moderate frame rates without blocking the WiFi task.
//
// CSV columns:
//   R12, rx_seq, t_local_us, rx_timestamp_us, bb_format, rate, channel,
//        rssi, sig_len, src_mac, dst_mac, fc_lo, fc_hi, hdr_seq, dropped
//
// bb_format values (wifi_rx_bb_format_t):
//   0=11B, 1=11G/11A, 2=HT, 3=VHT, 4=HE_SU, 5=HE_MU, 6=HE_ERSU, 7=HE_TB, 11=VHT_MU
//
// What we are looking for (architecture.md R12, lessons_learned §3):
//  * rx_timestamp_us monotonically increases across consecutive frames
//    from the same BSSID — no duplicate or 0-trail bug
//    (cf. https://github.com/espressif/esp-idf/issues/2468)
//  * bb_format >= 4 (HE_*) appears when AP is in 11ax mode
//  * channel matches the AP channel
//
// FQBN:
//   XIAO C5:    esp32:esp32:XIAO_ESP32C5
//   devkit C5:  esp32:esp32:esp32c5

#include <WiFi.h>
#include <esp_wifi.h>
#include <esp_wifi_types.h>
#include <esp_timer.h>
#include <string.h>

static const char* WIFI_SSID = "TEAM_SSID_OPEN";
static const char* WIFI_PASS = "";

static uint8_t  g_bssid[6] = {0};
static bool     g_bssid_known = false;

// SPSC ring. Producer: promisc_cb (WiFi task). Consumer: loop() (Arduino task).
struct Entry {
  uint64_t t_local_us;
  uint32_t rx_timestamp_us;
  int8_t   rssi;
  uint8_t  bb_format;
  uint8_t  rate;
  uint8_t  channel;
  uint16_t sig_len;
  uint16_t hdr_seq;     // 12-bit 802.11 seq#
  uint8_t  fc_lo;
  uint8_t  fc_hi;
  uint8_t  src[6];
  uint8_t  dst[6];
};

static const size_t RING_N = 256;  // power of two
static Entry           g_ring[RING_N];
static volatile uint32_t g_head = 0;  // written by cb
static volatile uint32_t g_tail = 0;  // written by loop
static volatile uint32_t g_dropped_total = 0;
static uint32_t          g_rx_seq = 0;

static void IRAM_ATTR promisc_cb(void *buf, wifi_promiscuous_pkt_type_t type) {
  if (type != WIFI_PKT_DATA) return;
  const wifi_promiscuous_pkt_t *pkt = (const wifi_promiscuous_pkt_t*)buf;
  const wifi_pkt_rx_ctrl_t *rx = &pkt->rx_ctrl;
  const uint8_t *frame = pkt->payload;
  if (rx->sig_len < 24) return;

  if (!g_bssid_known) return;
  const uint8_t *addr1 = frame + 4;
  const uint8_t *addr2 = frame + 10;
  if (memcmp(addr2, g_bssid, 6) != 0) return;  // only AP-sourced downlink

  uint32_t head = g_head;
  uint32_t next = (head + 1) & (RING_N - 1);
  if (next == g_tail) {
    g_dropped_total++;
    return;
  }
  Entry *e = &g_ring[head];
  e->t_local_us      = (uint64_t)esp_timer_get_time();
  e->rx_timestamp_us = rx->timestamp;
  e->rssi            = rx->rssi;
  e->bb_format       = rx->cur_bb_format;
  e->rate            = rx->rate;
  e->channel         = rx->channel;
  e->sig_len         = rx->sig_len;
  e->fc_lo           = frame[0];
  e->fc_hi           = frame[1];
  uint16_t seqctl    = (uint16_t)frame[22] | ((uint16_t)frame[23] << 8);
  e->hdr_seq         = seqctl >> 4;
  memcpy(e->src, addr2, 6);
  memcpy(e->dst, addr1, 6);
  __sync_synchronize();
  g_head = next;
}

static void print_mac(char *out, const uint8_t mac[6]) {
  sprintf(out, "%02X:%02X:%02X:%02X:%02X:%02X",
          mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

void setup() {
  Serial.begin(921600);
  delay(200);
  Serial.println();
  Serial.println("# R12_PROMISC_TEST v1");

  WiFi.mode(WIFI_STA);

  uint8_t mac[6];
  esp_wifi_get_mac(WIFI_IF_STA, mac);
  char macs[18]; print_mac(macs, mac);
  Serial.printf("# MAC=%s\n", macs);

  Serial.printf("# Connecting to SSID=%s (open, hidden)\n", WIFI_SSID);
  WiFi.begin(WIFI_SSID, WIFI_PASS);

  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && (millis() - t0) < 30000) {
    delay(200);
  }

  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("# ERROR: WiFi connect timeout (30s)");
    return;
  }

  uint8_t *bssid = WiFi.BSSID();
  memcpy(g_bssid, bssid, 6);
  __sync_synchronize();
  g_bssid_known = true;

  char bs[18]; print_mac(bs, g_bssid);
  Serial.printf("# CONNECTED ch=%d rssi=%d ip=%s bssid=%s\n",
                WiFi.channel(), (int)WiFi.RSSI(),
                WiFi.localIP().toString().c_str(), bs);

  wifi_promiscuous_filter_t filt = { .filter_mask = WIFI_PROMIS_FILTER_MASK_DATA };
  esp_err_t e1 = esp_wifi_set_promiscuous_filter(&filt);
  esp_err_t e2 = esp_wifi_set_promiscuous_rx_cb(&promisc_cb);
  esp_err_t e3 = esp_wifi_set_promiscuous(true);
  Serial.printf("# PROMISC_ON filt=DATA filter_err=%d cb_err=%d enable_err=%d\n",
                (int)e1, (int)e2, (int)e3);
  Serial.println("# CSV: rx_seq,t_local_us,rx_timestamp_us,bb_format,rate,channel,rssi,sig_len,src,dst,fc_lo,fc_hi,hdr_seq,dropped_total");
}

void loop() {
  // Drain ring
  while (g_tail != g_head) {
    const Entry &e = g_ring[g_tail];
    char src[18]; print_mac(src, e.src);
    char dst[18]; print_mac(dst, e.dst);
    Serial.printf("R12,%u,%llu,%u,%u,%u,%u,%d,%u,%s,%s,%02X,%02X,%u,%u\n",
                  (unsigned)(g_rx_seq++),
                  (unsigned long long)e.t_local_us,
                  (unsigned)e.rx_timestamp_us,
                  (unsigned)e.bb_format,
                  (unsigned)e.rate,
                  (unsigned)e.channel,
                  (int)e.rssi,
                  (unsigned)e.sig_len,
                  src, dst,
                  (unsigned)e.fc_lo, (unsigned)e.fc_hi,
                  (unsigned)e.hdr_seq,
                  (unsigned)g_dropped_total);
    g_tail = (g_tail + 1) & (RING_N - 1);
  }
  // Heartbeat every 5s when idle: confirms the sketch is alive even on quiet networks.
  static unsigned long last_hb = 0;
  unsigned long now = millis();
  if (now - last_hb > 5000) {
    last_hb = now;
    Serial.printf("# HB rx_seq=%u dropped=%u head=%u tail=%u rssi=%d\n",
                  (unsigned)g_rx_seq,
                  (unsigned)g_dropped_total,
                  (unsigned)g_head, (unsigned)g_tail,
                  (int)WiFi.RSSI());
  }
  delay(1);
}
