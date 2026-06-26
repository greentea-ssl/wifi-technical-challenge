// metrics_radio — HID 起源 Radio Metrics チャネル (port 52000+id) 実装ライブラリ
//
// robot_comm_spec/radio_metrics.md (v2.0.0) の rx_dl / tx_ul / hb メッセージを
// broadcast する小さな再利用可能モジュール。ESP-IDF v5 / Arduino ESP32 core 3.x
// で動作 (ESP32-C5 を想定)。
//
// 公開 API:
//   metrics_init(robot_id, subnet_third)
//       WiFi associate 完了後に呼ぶ。送信 UDP の準備とリング初期化。
//
//   metrics_record_rx(payload, payload_len)
//       下りコマンド (40000+id) 受信直後に呼ぶ。esp_timer_get_time() を取り、
//       payload offset 38-45 (8B double LE) を corr_unix_time として読む。
//       ISR から呼ぶことは想定しない (esp_timer は ISR safe だがリング更新を
//       排他制御していない)。Arduino の WiFi RX コールバックや loop() 文脈で OK。
//
//   metrics_record_tx(tx_port, frame_size)
//       上り (50000+id / 51000+id) 送信直前に呼ぶ。
//       ※ tx_ul は robot_comm_spec v2.1.0 で任意化したため本実装では no-op
//         (上り OWD は rx_dl の送信アンカー t_tx_tsf_us / hb の t_now_tsf_us で計測)。
//         API は callers 互換のため残置。
//
//   metrics_task()
//       loop() か専用 FreeRTOS タスクから周期的に呼ぶ。リング drain →
//       esp_timer↔TSF 較正 (100ms 周期、中点フィット) → JSON 構築 →
//       52000+id へ broadcast。1Hz で hb も発行。

#pragma once

#include <stdint.h>
#include <stddef.h>

#ifdef __cplusplus
extern "C" {
#endif

void metrics_init(uint8_t robot_id, uint8_t subnet_third);

// broadcast 先 (52000+id の宛先) を現在の WiFi.localIP() から再計算する。
// 別サブネットへ再接続 (set_ssid 等) した GOT_IP 時に呼ぶ。metrics_init 済みでないと no-op。
void metrics_update_broadcast(void);

// PPS 出力を有効化 (TSF 1秒境界で gpio_pin にパルス + pps JSON broadcast)。
// metrics_init 後に呼ぶ。docs/pps_sync_design.md 参照。
void metrics_pps_enable(int gpio_pin);

void metrics_record_rx(const uint8_t* payload, size_t payload_len);

void metrics_record_tx(uint16_t tx_port, uint16_t frame_size);

void metrics_task(void);

// 再associate / AP 切替 (set_ssid) 時に呼ぶ。TSF 較正リングをクリアし、新 AP の TSF で
// 再較正させる (旧 AP の不連続ペアによる回帰破綻を防止)。
void metrics_on_reassociate(void);

#ifdef __cplusplus
}
#endif
