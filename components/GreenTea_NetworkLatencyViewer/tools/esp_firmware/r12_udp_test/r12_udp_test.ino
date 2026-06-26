// R12 promiscuous test — UDP output variant
//
// Same capture pipeline as r12_promisc_test, but each frame record is sent
// as a UDP datagram to UDP_HOST:UDP_PORT instead of Serial. Used for the
// XIAO C5 where native USB CDC TX is broken after the early MSPI init
// error but WiFi is fine (see phase0_runbook §1.2.2).
//
// One frame = one datagram = one CSV line, same column set as the Serial
// variant, so the host UDP runner produces the same CSV schema as the
// serial runner. R11 join analyzer can then merge XIAO-UDP and devkit-serial
// captures uniformly.
//
// FQBN:
//   XIAO C5: esp32:esp32:XIAO_ESP32C5

#include <WiFi.h>
#include <WiFiUdp.h>
#include <esp_wifi.h>
#include <esp_wifi_types.h>
#include <esp_timer.h>
#include <string.h>

static const char* WIFI_SSID = "TEAM_SSID_OPEN";
static const char* WIFI_PASS = "";

static const char*    UDP_HOST = "192.168.1.202";   // host machine on br0
static const uint16_t UDP_PORT = 41250;

static uint8_t  g_bssid[6] = {0};
static bool     g_bssid_known = false;

struct Entry {
  uint64_t t_local_us;
  uint32_t rx_timestamp_us;
  int8_t   rssi;
  uint8_t  bb_format;
  uint8_t  rate;
  uint8_t  channel;
  uint16_t sig_len;
  uint16_t hdr_seq;
  uint8_t  fc_lo;
  uint8_t  fc_hi;
  uint8_t  src[6];
  uint8_t  dst[6];
};

static const size_t RING_N = 256;
static Entry            g_ring[RING_N];
static volatile uint32_t g_head = 0;
static volatile uint32_t g_tail = 0;
static volatile uint32_t g_dropped_total = 0;
static uint32_t          g_rx_seq = 0;

static WiFiUDP udp;

static void IRAM_ATTR promisc_cb(void *buf, wifi_promiscuous_pkt_type_t type) {
  if (type != WIFI_PKT_DATA) return;
  const wifi_promiscuous_pkt_t *pkt = (const wifi_promiscuous_pkt_t*)buf;
  const wifi_pkt_rx_ctrl_t *rx = &pkt->rx_ctrl;
  const uint8_t *frame = pkt->payload;
  if (rx->sig_len < 24) return;
  if (!g_bssid_known) return;

  const uint8_t *addr1 = frame + 4;
  const uint8_t *addr2 = frame + 10;
  if (memcmp(addr2, g_bssid, 6) != 0) return;

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

static void format_mac(char *out, const uint8_t mac[6]) {
  sprintf(out, "%02X:%02X:%02X:%02X:%02X:%02X",
          mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);
}

// XIAO HWCDC is dead; Serial.printf is not visible to the host.
// We still keep Serial as a debugging fallback in case CDC recovers; harmless if not.
void setup() {
  Serial.begin(921600);
  delay(200);
  Serial.println("# R12_UDP v1 (XIAO HWCDC may be dead - output via UDP)");

  WiFi.mode(WIFI_STA);
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && (millis() - t0) < 30000) delay(200);
  if (WiFi.status() != WL_CONNECTED) {
    // No way to report this on XIAO; just spin.
    return;
  }

  uint8_t *bssid = WiFi.BSSID();
  memcpy(g_bssid, bssid, 6);
  __sync_synchronize();
  g_bssid_known = true;

  udp.begin(0);  // any source port

  wifi_promiscuous_filter_t filt = { .filter_mask = WIFI_PROMIS_FILTER_MASK_DATA };
  esp_wifi_set_promiscuous_filter(&filt);
  esp_wifi_set_promiscuous_rx_cb(&promisc_cb);
  esp_wifi_set_promiscuous(true);

  // Announce ourselves to the host so the runner can confirm the board is alive.
  uint8_t mac[6]; esp_wifi_get_mac(WIFI_IF_STA, mac);
  char macs[18]; format_mac(macs, mac);
  char bs[18]; format_mac(bs, g_bssid);
  char hello[160];
  snprintf(hello, sizeof(hello),
           "# HELLO mac=%s bssid=%s ch=%d ip=%s\n",
           macs, bs, WiFi.channel(), WiFi.localIP().toString().c_str());
  udp.beginPacket(UDP_HOST, UDP_PORT); udp.write((const uint8_t*)hello, strlen(hello)); udp.endPacket();
}

void loop() {
  char buf[200];
  while (g_tail != g_head) {
    const Entry &e = g_ring[g_tail];
    char src[18]; format_mac(src, e.src);
    char dst[18]; format_mac(dst, e.dst);
    int n = snprintf(buf, sizeof(buf),
        "R12,%u,%llu,%u,%u,%u,%u,%d,%u,%s,%s,%02X,%02X,%u,%u\n",
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
    if (n > 0) {
      udp.beginPacket(UDP_HOST, UDP_PORT);
      udp.write((const uint8_t*)buf, (size_t)n);
      udp.endPacket();
    }
    g_tail = (g_tail + 1) & (RING_N - 1);
  }
  static unsigned long last_hb = 0;
  unsigned long now = millis();
  if (now - last_hb > 5000) {
    last_hb = now;
    int n = snprintf(buf, sizeof(buf),
        "# HB rx_seq=%u dropped=%u rssi=%d\n",
        (unsigned)g_rx_seq, (unsigned)g_dropped_total, (int)WiFi.RSSI());
    udp.beginPacket(UDP_HOST, UDP_PORT);
    udp.write((const uint8_t*)buf, (size_t)n);
    udp.endPacket();
  }
  delay(1);
}
