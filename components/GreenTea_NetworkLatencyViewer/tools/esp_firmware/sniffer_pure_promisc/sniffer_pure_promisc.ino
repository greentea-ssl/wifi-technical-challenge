// sniffer_pure_promisc — STA associate せず純 promiscuous モードでチャンネル固定
//
// 目的: ESP32-C5 + STA + PROMIS では他 STA 宛てユニキャストフレームが chip
// レベルで cb に届かないことが分かったので、STA 接続なしの pure promiscuous
// (WIFI_MODE_NULL + esp_wifi_set_promiscuous) で他 STA ユニキャストが取れるか
// 確認する。
//
// 本sketch は sniffer.ino のバイナリプロトコル/ring/UART 設定を継承し、
// WiFi モード初期化と filter (BSSID matching なし) だけ差し替えたもの。
//
// 注意: 本構成では AP TSF と同期しないので、`rx_timestamp_us` は esp_timer に
// 沿った内部時刻になり、AP TSF への変換は別経路 (TSF-sync sniffer から)
// 取り込んだ較正で行う必要がある (本リポでの最終形は 2 sniffer 構成)。
//
// FQBN:
//   XIAO C5:    esp32:esp32:XIAO_ESP32C5
//   devkit C5:  esp32:esp32:esp32c5

#include <WiFi.h>
#include <esp_wifi.h>
#include <esp_wifi_types.h>
#include <esp_timer.h>
#include <string.h>

// ============================================================
// User-tunable
// ============================================================
static const uint8_t  CHANNEL          = 44;             // 5GHz ch44 (TEAM_SSID_OPEN)
static const uint32_t SERIAL_BAUD      = 2000000;
static const size_t   RING_N           = 4096;           // power of 2
static const bool     FILTER_HE_ONLY   = false;
static const bool     LEARN_BSSID_FROM_BEACONS = false;  // 既知の AP BSSID をハードコード
static const bool     APPLY_BSSID_FILTER       = false;  // 一旦 filter 無効 (デバッグ)
// 既知の AP (TEAM_SSID_OPEN, 5GHz ch44)
static const uint8_t  HARDCODED_BSSID[6] = { 0x76, 0x7F, 0xF0, 0x3B, 0x74, 0x26 };

// ============================================================
// Binary protocol (sniffer.ino と共通フォーマット)
// ============================================================
static const uint8_t SYNC_LO    = 0xC5;
static const uint8_t SYNC_HI    = 0xC5;
static const uint8_t TYPE_FRAME = 0x01;
static const uint8_t TYPE_HB    = 0x02;
static const uint8_t LEN_FRAME  = 36;
static const uint8_t LEN_HB     = 16;

struct __attribute__((packed)) Entry {
  uint32_t rx_seq;
  uint32_t t_local_us_lo;
  uint32_t rx_timestamp_us;
  uint8_t  bb_format;
  uint8_t  rate;
  uint8_t  channel;
  int8_t   rssi;
  uint16_t sig_len;
  uint16_t hdr_seq;
  uint8_t  src[6];
  uint8_t  dst[6];
  uint8_t  fc_lo;
  uint8_t  fc_hi;
};
static_assert(sizeof(Entry) == 34, "Entry must be 34 bytes");

static Entry              g_ring[RING_N];
static volatile uint32_t  g_head = 0;
static volatile uint32_t  g_tail = 0;
static volatile uint32_t  g_dropped_total = 0;
static volatile uint32_t  g_captured_total = 0;
static volatile uint32_t  g_cb_total = 0;
static volatile uint32_t  g_cb_data = 0;
static volatile uint32_t  g_cb_mgmt = 0;
static uint32_t           g_rx_seq = 0;

static uint8_t g_bssid[6] = {0};
static volatile bool g_bssid_known = false;

// ============================================================
// Promiscuous callback
// ============================================================
static void IRAM_ATTR promisc_cb(void *buf, wifi_promiscuous_pkt_type_t type) {
  g_cb_total++;
  if (type == WIFI_PKT_MGMT) {
    g_cb_mgmt++;
    // Try to learn BSSID from beacon frames (type=0, subtype=8 → fc=0x80)
    if (LEARN_BSSID_FROM_BEACONS && !g_bssid_known) {
      const wifi_promiscuous_pkt_t *pkt = (const wifi_promiscuous_pkt_t*)buf;
      const uint8_t *frame = pkt->payload;
      if (pkt->rx_ctrl.sig_len >= 24 && (frame[0] & 0xFC) == 0x80) {
        // addr3 = BSSID for beacon
        memcpy(g_bssid, frame + 16, 6);
        __sync_synchronize();
        g_bssid_known = true;
      }
    }
    return;
  }
  if (type != WIFI_PKT_DATA) return;
  g_cb_data++;
  const wifi_promiscuous_pkt_t *pkt = (const wifi_promiscuous_pkt_t*)buf;
  const wifi_pkt_rx_ctrl_t *rx = &pkt->rx_ctrl;
  const uint8_t *frame = pkt->payload;
  if (rx->sig_len < 24) return;
  if (FILTER_HE_ONLY && rx->cur_bb_format < 4) return;

  const uint8_t *addr1 = frame + 4;
  const uint8_t *addr2 = frame + 10;
  if (APPLY_BSSID_FILTER && memcmp(addr2, g_bssid, 6) != 0) return;

  uint32_t head = g_head;
  uint32_t next = (head + 1) & (RING_N - 1);
  if (next == g_tail) { g_dropped_total++; return; }
  Entry &e = g_ring[head];
  uint64_t t_local = (uint64_t)esp_timer_get_time();
  e.rx_seq          = g_rx_seq++;
  e.t_local_us_lo   = (uint32_t)(t_local & 0xFFFFFFFFu);
  e.rx_timestamp_us = (uint32_t)rx->timestamp;
  e.bb_format       = (uint8_t)rx->cur_bb_format;
  e.rate            = (uint8_t)rx->rate;
  e.channel         = (uint8_t)rx->channel;
  e.rssi            = (int8_t)rx->rssi;
  e.sig_len         = (uint16_t)rx->sig_len;
  uint16_t seqctl   = (uint16_t)frame[22] | ((uint16_t)frame[23] << 8);
  e.hdr_seq         = (uint16_t)(seqctl >> 4);
  memcpy(e.src, addr2, 6);
  memcpy(e.dst, addr1, 6);
  e.fc_lo           = frame[0];
  e.fc_hi           = frame[1];
  __sync_synchronize();
  g_head = next;
  g_captured_total++;
}

static inline void write_frame_record(const Entry &e) {
  uint8_t hdr[4] = { SYNC_LO, SYNC_HI, TYPE_FRAME, LEN_FRAME };
  Serial.write(hdr, sizeof(hdr));
  Serial.write((const uint8_t*)&e, sizeof(e));
  uint16_t dropped_lo = (uint16_t)(g_dropped_total & 0xFFFFu);
  Serial.write((const uint8_t*)&dropped_lo, sizeof(dropped_lo));
}

static inline void write_hb_record() {
  uint8_t hdr[4] = { SYNC_LO, SYNC_HI, TYPE_HB, LEN_HB };
  Serial.write(hdr, sizeof(hdr));
  uint32_t captured = g_captured_total;
  uint32_t dropped  = g_dropped_total;
  uint64_t t_now    = (uint64_t)esp_timer_get_time();
  uint32_t t_now_lo = (uint32_t)(t_now & 0xFFFFFFFFu);
  int32_t  rssi_now = 0;  // 関連付け無し
  Serial.write((const uint8_t*)&captured, sizeof(captured));
  Serial.write((const uint8_t*)&dropped, sizeof(dropped));
  Serial.write((const uint8_t*)&t_now_lo, sizeof(t_now_lo));
  Serial.write((const uint8_t*)&rssi_now, sizeof(rssi_now));
}

void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(200);
  Serial.println();
  Serial.printf("# sniffer_pure_promisc v0 baud=%u ch=%u ring=%u\n",
                (unsigned)SERIAL_BAUD, (unsigned)CHANNEL, (unsigned)RING_N);

  WiFi.mode(WIFI_STA);
  delay(200);

  // 5GHz 動作に必要な country code (JP は ch36-64 等の 5GHz が許可)
  wifi_country_t country = { .cc = "JP", .schan = 1, .nchan = 14, .max_tx_power = 20, .policy = WIFI_COUNTRY_POLICY_MANUAL };
  esp_err_t rc_country = esp_wifi_set_country(&country);
  Serial.printf("# set_country(JP) rc=%d\n", (int)rc_country);

  // Promiscuous filter (DATA + MGMT)
  wifi_promiscuous_filter_t filt = {
    .filter_mask = WIFI_PROMIS_FILTER_MASK_DATA | WIFI_PROMIS_FILTER_MASK_MGMT
  };
  esp_err_t rc_filt = esp_wifi_set_promiscuous_filter(&filt);
  esp_err_t rc_cb   = esp_wifi_set_promiscuous_rx_cb(&promisc_cb);
  esp_err_t rc_on   = esp_wifi_set_promiscuous(true);
  Serial.printf("# PROMISC_ON filt_rc=%d cb_rc=%d on_rc=%d\n",
                (int)rc_filt, (int)rc_cb, (int)rc_on);

  // Hardcoded BSSID (post-filter)
  if (!LEARN_BSSID_FROM_BEACONS) {
    memcpy(g_bssid, HARDCODED_BSSID, 6);
    __sync_synchronize();
    g_bssid_known = true;
    Serial.printf("# bssid hardcoded: %02X:%02X:%02X:%02X:%02X:%02X\n",
                  g_bssid[0], g_bssid[1], g_bssid[2], g_bssid[3], g_bssid[4], g_bssid[5]);
  }

  // Channel 固定 — set_promiscuous(true) の後で繰返し試す
  for (int attempt = 0; attempt < 3; attempt++) {
    esp_err_t rc_ch = esp_wifi_set_channel(CHANNEL, WIFI_SECOND_CHAN_NONE);
    uint8_t cur_ch = 0;
    wifi_second_chan_t cur_2 = WIFI_SECOND_CHAN_NONE;
    esp_wifi_get_channel(&cur_ch, &cur_2);
    Serial.printf("# attempt %d: set_channel(%u) rc=%d  current=%u second=%d\n",
                  attempt, (unsigned)CHANNEL, (int)rc_ch, (unsigned)cur_ch, (int)cur_2);
    if (cur_ch == CHANNEL) break;
    delay(100);
  }

  uint8_t mac[6];
  esp_wifi_get_mac(WIFI_IF_STA, mac);
  Serial.printf("# MAC=%02X:%02X:%02X:%02X:%02X:%02X\n",
                mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);

  Serial.println("# BEGIN_BINARY (waiting for beacon to learn BSSID)");
}

void loop() {
  while (g_tail != g_head) {
    write_frame_record(g_ring[g_tail]);
    g_tail = (g_tail + 1) & (RING_N - 1);
  }

  static uint32_t last_hb = 0;
  uint32_t now = millis();
  if (now - last_hb >= 1000) {
    last_hb = now;
    Serial.printf("\n# DIAG cb_total=%u cb_data=%u cb_mgmt=%u captured=%u dropped=%u bssid=%02X:%02X:%02X:%02X:%02X:%02X\n",
                  (unsigned)g_cb_total, (unsigned)g_cb_data, (unsigned)g_cb_mgmt,
                  (unsigned)g_captured_total, (unsigned)g_dropped_total,
                  g_bssid[0], g_bssid[1], g_bssid[2], g_bssid[3], g_bssid[4], g_bssid[5]);
    write_hb_record();
  }
  delay(1);
}
