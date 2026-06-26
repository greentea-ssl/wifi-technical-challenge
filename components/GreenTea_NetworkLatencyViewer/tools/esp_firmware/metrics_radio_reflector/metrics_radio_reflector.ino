// metrics_radio_reflector — スタンドアロン HID 模擬 (テスト用)
//
// 目的: AI マシン環境が無くても、metrics_radio モジュール (radio_metrics.md
// v2.0.0 準拠) の動作確認ができるよう、ESP32-C5 で
//   1. WiFi STA として AP に associate
//   2. port 40000+ROBOT_ID で UDP 下り受信 → metrics_record_rx 呼出
//   3. 100ms 周期で fake uplink を 50000+ROBOT_ID に broadcast → metrics_record_tx 呼出
// を行う。CAN・UART・Wio Display などの本物 HID 機能は持たない。
//
// 期待される観測 (pc_emulator から下りパケットを送ったとき):
//   - 52000+ROBOT_ID に rx_dl JSON が下りパケット毎に届く
//   - 52000+ROBOT_ID に tx_ul JSON が 100ms 毎に届く
//   - 52000+ROBOT_ID に hb JSON が 1 秒毎に届く
//
// FQBN:
//   XIAO C5:    esp32:esp32:XIAO_ESP32C5
//   devkit C5:  esp32:esp32:esp32c5

#include <WiFi.h>
#include <WiFiUdp.h>
#include <esp_wifi.h>
#include <string.h>
#include "metrics_radio.h"

// ============================================================
// User-tunable
// ============================================================
static const char*    WIFI_SSID    = "TEAM_SSID_OPEN";
static const char*    WIFI_PASS    = "";
static const uint8_t  ROBOT_ID     = 1;
// SUBNET_THIRD = 0 → WiFi.localIP() から自動算出 (推奨、subnet 移行に追随)
static const uint8_t  SUBNET_THIRD = 0;
static const uint16_t LISTEN_PORT  = 40000 + ROBOT_ID;
static const uint16_t UPLINK_PORT  = 50000 + ROBOT_ID;
static const uint32_t UPLINK_PERIOD_MS = 100;  // 10 Hz fake uplink

// 試験用: true で HT/VHT/HE を無効化 → AP は STA に対し legacy 11a で送信
// (5GHz では 11b/g は使われず、bitmap が 11n より下なら 11a 相当に落ちる)
static const bool     FORCE_LEGACY_11A = false;  // 本走は 11ax (HE)。試験で 11a 強制したい時のみ true

// 試験用 UL unicast (air/wire 時差計測用):
// 100ms 周期で UL_UC_TARGET 宛てに小さな UDP unicast を送信。
// payload: u32 magic 0xCA113AA0 + u32 ul_uc_seq + u64 t_local_us (24 bytes)
static const char*    UL_UC_TARGET = "192.168.4.160";  // host PC
static const uint16_t UL_UC_PORT   = 49000;
static const uint32_t UL_UC_PERIOD_MS = 100;
static WiFiUDP udp_ul_uc;
static uint32_t s_ul_uc_seq = 0;
static uint32_t s_last_ul_uc_ms = 0;

// Runtime: computed broadcast IP (subnet/24)
static IPAddress s_bcast;

// ============================================================
// State
// ============================================================
static WiFiUDP udp_in;
static WiFiUDP udp_uplink;
static uint8_t rxbuf[1500];
static uint32_t s_last_uplink_ms = 0;

void setup() {
  Serial.begin(115200);
  delay(200);
  Serial.println();
  Serial.println("# metrics_radio_reflector v0");

  // RGB LED で役割を視認 (devkit 区別用): reflector = 緑、起動直後は黄
  rgbLedWrite(RGB_BUILTIN, 8, 8, 0);   // 黄 = boot/未接続 (低輝度)

  WiFi.mode(WIFI_STA);
  if (FORCE_LEGACY_11A) {
    // ESP32-C5 は dual-band、esp_wifi_set_protocols() で band 別に指定する
    // 5GHz 側だけ 11A bit を立てる (HT/VHT/HE 無効化)。2.4G 側は触らずデフォルト維持
    wifi_protocols_t protos = {
      .ghz_2g = WIFI_PROTOCOL_11B | WIFI_PROTOCOL_11G | WIFI_PROTOCOL_11N,
      .ghz_5g = WIFI_PROTOCOL_11A,
    };
    esp_err_t e = esp_wifi_set_protocols(WIFI_IF_STA, &protos);
    Serial.printf("# FORCE_LEGACY_11A: set_protocols rc=%d (5G=11a only)\n", (int)e);
  }
  WiFi.begin(WIFI_SSID, WIFI_PASS);
  Serial.printf("# Connecting to %s\n", WIFI_SSID);

  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && (millis() - t0) < 30000) {
    delay(200);
  }
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("# ERR: WiFi connect timeout");
    rgbLedWrite(RGB_BUILTIN, 16, 0, 0);  // 赤 = 接続失敗 (低輝度)
    return;
  }
  rgbLedWrite(RGB_BUILTIN, 0, 16, 0);  // 緑 = reflector (接続済、低輝度)
  Serial.printf("# CONNECTED ip=%s ch=%d rssi=%d\n",
                WiFi.localIP().toString().c_str(), WiFi.channel(), (int)WiFi.RSSI());

  if (!udp_in.begin(LISTEN_PORT)) {
    Serial.println("# ERR: udp_in.begin failed");
    return;
  }
  Serial.printf("# listen UDP %u (downlink)\n", LISTEN_PORT);
  Serial.printf("# uplink   UDP %u (fake @%u ms)\n", UPLINK_PORT, UPLINK_PERIOD_MS);

  metrics_init(ROBOT_ID, SUBNET_THIRD);
  IPAddress me = WiFi.localIP();
  s_bcast = IPAddress(me[0], me[1], me[2], 255);
  Serial.printf("# metrics broadcast -> %s:%u (auto from localIP)\n",
                s_bcast.toString().c_str(), (unsigned)(52000 + ROBOT_ID));
}

void loop() {
  // --- 下り受信 ---
  int avail = udp_in.parsePacket();
  if (avail > 0) {
    int len = udp_in.read(rxbuf, sizeof(rxbuf));
    if (len > 0) {
      metrics_record_rx(rxbuf, (size_t)len);
    }
  }

  // --- fake uplink (100ms 周期) ---
  uint32_t now_ms = millis();
  if (now_ms - s_last_uplink_ms >= UPLINK_PERIOD_MS) {
    s_last_uplink_ms = now_ms;
    // s_bcast は setup() で localIP() から算出済 (subnet 移行に追随)
    // 上り OWD の区間分解 (HID gen → air(sniffer) → wire(SPAN)) 用に、64B 固定
    // payload の offset 51-53 へ ul_seq (24bit LE) を埋める。これは DL の cycle_count
    // と同じ offset なので、sniffer / WireReader の既存パーサがそのまま cycle_count 列
    // に拾い、tx_ul.ul_seq と join できる (s_ul_count は emit_tx_ul の s_ul_seq と 1:1:
    // metrics_record_tx 呼出毎に両者 +1)。
    static uint8_t  ulbuf[64];
    static uint32_t s_ul_count = 0;
    memset(ulbuf, 0, sizeof(ulbuf));
    uint32_t magic = 0x4B4C5055;            // "UPLK"
    memcpy(ulbuf, &magic, 4);
    ulbuf[51] = (uint8_t)(s_ul_count & 0xFF);
    ulbuf[52] = (uint8_t)((s_ul_count >> 8) & 0xFF);
    ulbuf[53] = (uint8_t)((s_ul_count >> 16) & 0xFF);
    metrics_record_tx(UPLINK_PORT, (uint16_t)sizeof(ulbuf));
    udp_uplink.beginPacket(s_bcast, UPLINK_PORT);
    udp_uplink.write(ulbuf, sizeof(ulbuf));
    udp_uplink.endPacket();
    s_ul_count++;
  }

  // --- 試験用 UL unicast (air/wire 時差計測用) ---
  if (now_ms - s_last_ul_uc_ms >= UL_UC_PERIOD_MS) {
    s_last_ul_uc_ms = now_ms;
    uint32_t magic = 0xCA113AA0;
    uint64_t t_local = (uint64_t)esp_timer_get_time();
    uint8_t buf[24];
    memcpy(buf,     &magic,        4);
    memcpy(buf + 4, &s_ul_uc_seq,  4);
    memcpy(buf + 8, &t_local,      8);
    memset(buf + 16, 0, 8);
    udp_ul_uc.beginPacket(UL_UC_TARGET, UL_UC_PORT);
    udp_ul_uc.write(buf, sizeof(buf));
    udp_ul_uc.endPacket();
    s_ul_uc_seq++;
  }

  // --- metrics モジュール周期処理 (ring drain + 較正 + hb) ---
  metrics_task();

  delay(1);  // yield to WiFi
}
