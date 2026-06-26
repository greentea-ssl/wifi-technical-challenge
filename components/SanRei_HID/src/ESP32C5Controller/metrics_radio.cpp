// metrics_radio — 実装。詳細仕様は robot_comm_spec/radio_metrics.md (v2.0.0)。
//
// 設計:
//  - hot-path (record_rx/tx) は esp_timer_get_time() + ring push のみ
//  - metrics_task() が:
//      (a) 100ms 周期で (esp_timer 中点, TSF) ペアを取得 → 線形回帰更新
//      (b) ring を drain して JSON 化 → UDP broadcast
//      (c) 1Hz で hb メッセージ発行
//  - corr_unix_time が NaN の場合 (payload < 46 bytes) は JSON 上 null

#include "metrics_radio.h"

#include <Arduino.h>
#include <WiFi.h>
#include <WiFiUdp.h>
#include <esp_wifi.h>
#include <esp_timer.h>
#include <esp_rom_sys.h>
#include <driver/gpio.h>
#include <driver/gptimer.h>
#include <math.h>
#include <string.h>

// rx_dlb (rx_dl バッチ送信、radio_metrics.md §3.1.1、type 0x04) ビルド切替。
//   1 = batch (0x04): 複数 rx_dl を 1 UDP にまとめ broadcast (上り自己干渉削減、既定)
//   0 = per-frame (0x01): 下り受信毎に rx_dl を 1 件ずつ broadcast (旧挙動、A/B 比較用)
// arduino-cli では --build-property "build.extra_flags=-DRX_DL_BATCH=0" で per-frame 化。
#ifndef RX_DL_BATCH
#define RX_DL_BATCH 1
#endif

namespace {

// ============================================================
// State
// ============================================================
uint8_t   s_robot_id = 0;
IPAddress s_broadcast;
WiFiUDP   s_udp;
uint32_t  s_hid_seq = 0;
uint32_t  s_dl_seq  = 0;  // per-type counter for rx_dl emissions (loss detection key)
uint32_t  s_hb_last_ms = 0;
uint32_t  s_cal_last_ms = 0;
bool      s_inited = false;

// ============================================================
// SPSC ring (single producer = caller of record_*, single consumer = task)
// ============================================================
enum EntryType : uint8_t { ENTRY_RX = 1, ENTRY_TX = 2 };

struct Entry {
  uint8_t  type;
  uint16_t tx_port;       // for TX only
  uint16_t frame_size;
  uint64_t t_local_us;
  double   corr_unix_time;  // for RX; NaN if unknown
  uint32_t cycle_count;     // for RX; payload offset 51-53 (24bit LE、downlink_command.md)
  bool     cycle_count_valid;
  int8_t   rssi;            // for RX; 受信時の WiFi.RSSI() (dBm、radio_metrics.md v2.1.0 rx_dl.rssi)
};

constexpr size_t RING_N = 256;  // power of two
static_assert((RING_N & (RING_N - 1)) == 0, "RING_N must be power of 2");
Entry g_ring[RING_N];
volatile uint32_t g_head = 0;
volatile uint32_t g_tail = 0;
volatile uint32_t g_dropped = 0;

// ============================================================
// rx_dlb batch buffer (radio_metrics.md §3.1.1、type 0x04)
//   下り受信を per-record で完全保持したまま 1 UDP にまとめ broadcast し、
//   上り計測トラフィックの自己干渉 (basic rate 無集約 broadcast のエアタイム) を削減。
//   metrics_task (単一 consumer) のみが触るので排他不要。
// ============================================================
constexpr size_t   BATCH_K = 50;           // K: 1 batch 最大件数 (UDP payload ≤1400B 目安)
constexpr uint32_t BATCH_FLUSH_MS = 500;   // 経過 flush 閾値 (報告鮮度 ≤0.5s)
struct BatchRec { uint64_t t_local_us; uint32_t cycle; bool cycle_valid; int8_t rssi; };
BatchRec s_batch[BATCH_K];
size_t   s_batch_n = 0;
uint32_t s_batch_start_ms = 0;
uint32_t s_bseq = 0;   // batch 連番 (起動時 0、batch 毎 +1)。受信側の batch 損失検出キー
uint32_t s_rxc  = 0;   // 累積 rx_dl 受信数 (起動時 0、下り受信毎 +1)。損失検出の冗長チェック

// ============================================================
// TSF <-> esp_timer linear regression: tsf = a * local + b
// ============================================================
struct CalPair { uint64_t t_local_us; uint64_t t_tsf_us; };
constexpr size_t CAL_N = 64;
CalPair  g_cal[CAL_N];
size_t   g_cal_n = 0;        // valid entries (<=CAL_N)
size_t   g_cal_pos = 0;      // next write idx (ring)
bool     g_cal_valid = false;
double   g_cal_a = 1.0;
double   g_cal_b = 0.0;
// seqlock: a/b (double) は RISC-V 32bit で非アトミック。更新 (metrics_broadcast_task) と
// 読出 tsf_from_local (udp_rx_task) がタスク跨ぎで競合すると new-a/old-b の不整合・torn read
// で TSF が外れ値になる (issue #6)。writer は publish_cal で seq を ++ 前後、reader は
// cal_snapshot で一貫読み + retry。
volatile uint32_t g_cal_seq = 0;
// reassociate 要求フラグ (issue #8): リング状態 g_cal_n/g_cal_pos/g_cal[] は
// update_calibration_pair (broadcast task) が RMW する。loop タスクが直接書くと data race。
// loop はこのフラグを立てるだけ、リングのクリアは所有者 (broadcast task) が冒頭で行う。
volatile bool g_reset_pending = false;

static inline void publish_cal(double a, double b, bool valid) {
  g_cal_seq++; __sync_synchronize();
  g_cal_a = a; g_cal_b = b; g_cal_valid = valid;
  __sync_synchronize(); g_cal_seq++;
}

static inline bool cal_snapshot(double *a, double *b) {
  uint32_t s0, s1; bool v;
  do {
    s0 = g_cal_seq; __sync_synchronize();
    *a = g_cal_a; *b = g_cal_b; v = g_cal_valid;
    __sync_synchronize(); s1 = g_cal_seq;
  } while ((s0 & 1u) || s0 != s1);
  return v;
}

// スナップショット済 a/b で TSF を計算 (emit でガードと換算を同一スナップショットに揃える、issue #9)
static inline uint64_t tsf_calc(double a, double b, uint64_t t_local) {
  double v = a * (double)t_local + b;
  return (v < 0.0) ? 0ULL : (uint64_t)v;
}

void update_calibration_pair(uint64_t t_local, uint64_t t_tsf) {
  // reassociate 要求 (g_reset_pending) の消化は run_calibration_step() 冒頭で実施
  // (publish_cal の呼び出しを broadcast task に一本化し seqlock の単一ライターを保証する)。
  // 不連続検出 (issue #7): 再associate / AP 切替 / TSF リセットで (local,TSF) がジャンプすると
  // 直前ペアとの傾き Δtsf/Δlocal が 1.0 から大きく外れる。その場合リングをフラッシュして
  // 旧 AP の不連続ペアが回帰窓に残らないようにする。
  if (g_cal_n > 0) {
    size_t last = (g_cal_pos + CAL_N - 1) % CAL_N;
    double dl = (double)t_local - (double)g_cal[last].t_local_us;
    double dt = (double)t_tsf  - (double)g_cal[last].t_tsf_us;
    if (dl > 0.0) {
      double r = dt / dl;
      if (r < 0.999 || r > 1.001) {
        g_cal_n = 0; g_cal_pos = 0;   // 不連続 → リングフラッシュ
      }
    }
  }
  g_cal[g_cal_pos] = { t_local, t_tsf };
  g_cal_pos = (g_cal_pos + 1) % CAL_N;
  if (g_cal_n < CAL_N) g_cal_n++;

  if (g_cal_n < 4) {
    // Bootstrap: assume slope 1.0, offset is single pair's delta
    publish_cal(1.0, (double)t_tsf - (double)t_local, true);
    return;
  }
  // Linear regression — mean-centred for numerical stability
  double mean_l = 0.0, mean_t = 0.0;
  for (size_t i = 0; i < g_cal_n; i++) {
    mean_l += (double)g_cal[i].t_local_us;
    mean_t += (double)g_cal[i].t_tsf_us;
  }
  mean_l /= (double)g_cal_n;
  mean_t /= (double)g_cal_n;
  double sxx = 0.0, sxy = 0.0;
  for (size_t i = 0; i < g_cal_n; i++) {
    double dl = (double)g_cal[i].t_local_us - mean_l;
    double dt = (double)g_cal[i].t_tsf_us - mean_t;
    sxx += dl * dl;
    sxy += dl * dt;
  }
  if (sxx > 0.0) {
    double a = sxy / sxx;
    double b = mean_t - a * mean_l;
    // 傾き sanity (issue #7): esp_timer↔TSF は ~1.0 (±ppm)。外れ値ペアで a が乖離したら
    // 不採用 (既存 calib を維持)。
    if (a >= 0.99 && a <= 1.01) {
      publish_cal(a, b, true);
    }
  }
}

uint64_t tsf_from_local(uint64_t t_local) {
  double a, b;
  if (!cal_snapshot(&a, &b)) return 0ULL;
  double v = a * (double)t_local + b;
  if (v < 0.0) return 0ULL;
  return (uint64_t)v;
}

// 逆変換: TSF → esp_timer (PPS 境界スケジュール用)。local = (tsf - b) / a
uint64_t local_from_tsf(uint64_t t_tsf) {
  double a, b;
  if (!cal_snapshot(&a, &b) || a == 0.0) return 0ULL;
  double v = ((double)t_tsf - b) / a;
  if (v < 0.0) return 0ULL;
  return (uint64_t)v;
}

// 再associate / AP 切替 (set_ssid) 時に呼ぶ。較正リングと結果をクリアし新 AP の TSF で
// 再較正させる (issue #7)。呼ばないと旧 AP の不連続ペアが窓に残り回帰が壊れる。
extern "C" void metrics_on_reassociate(void) {
  // loop タスクからはフラグを立てるだけ。リング (g_cal_n/g_cal_pos/g_cal[]) のクリアも
  // a/b/valid の無効化 (publish_cal) も、所有者である broadcast task が run_calibration_step()
  // 冒頭で実施する。loop から publish_cal を呼ぶと broadcast task と seqlock を二重 write して
  // しまい (両者は別優先度で互いに preempt し得る) torn read を招くため、ここでは触らない。
  g_reset_pending = true;
  __sync_synchronize();
}

void run_calibration_step() {
  // reassociate 要求の消化 (broadcast task = cal の唯一の writer)。loop の
  // metrics_on_reassociate() は g_reset_pending を立てるだけで、リングのクリアと
  // publish_cal による無効化はここ (broadcast task) で行う → seqlock 単一ライター化。
  // t_tsf==0 (未associate) で早期 return する前に消化するので、切替直後でも即無効化される。
  if (g_reset_pending) {
    g_reset_pending = false;
    g_cal_n = 0;
    g_cal_pos = 0;
    publish_cal(1.0, 0.0, false);   // 旧 AP の較正を即無効化 (reader は再較正まで 0 を返す)
  }
  // Take a (t_mid, t_tsf) pair. Midpoint fit gives ~3.5x better p99 than
  // using t_before alone (Phase 0 R4 result).
  uint64_t t_a = (uint64_t)esp_timer_get_time();
  int64_t  t_tsf = esp_wifi_get_tsf_time(WIFI_IF_STA);
  uint64_t t_b = (uint64_t)esp_timer_get_time();
  if (t_tsf == 0) return;  // not associated to AP yet
  uint64_t t_mid = (t_a + t_b) / 2;
  update_calibration_pair(t_mid, (uint64_t)t_tsf);
}

// ============================================================
// JSON build helpers
// ============================================================

// meta: off-board 観測用 join header (radio_metrics.md §3.0)。
// 9 バイト = 18 桁 大文字 HEX: magic "RM"(52 4D) + ver(01) + type + robot_id +
// hid_seq(uint32 big-endian)。JSON 先頭キーに置くことで payload offset 9 固定。
// type_code: 0x01=rx_dl / 0x02=tx_ul / 0x03=hb。
static void fmt_meta(char* out, uint8_t type_code) {
  uint32_t s = s_hid_seq;
  snprintf(out, 19, "524D01%02X%02X%02X%02X%02X%02X",
           (unsigned)type_code, (unsigned)s_robot_id,
           (unsigned)((s >> 24) & 0xFF), (unsigned)((s >> 16) & 0xFF),
           (unsigned)((s >> 8) & 0xFF), (unsigned)(s & 0xFF));
}

void emit_rx_dl(const Entry& e) {
  // calib を 1 回スナップショットし、ガードと TSF 換算を同一世代に揃える (issue #9)。
  // 直接 g_cal_valid を読んでから tsf_from_local を呼ぶと、間の reassociate で valid=false に
  // なり t_rx/t_tx=0 の不正レコードを流す恐れがある。
  double ca, cb;
  if (!cal_snapshot(&ca, &cb)) return;  // TSF 未確定 (associate 前/再associate直後) は発行見送り
  char buf[384];
  uint64_t t_tsf = tsf_calc(ca, cb, e.t_local_us);  // 下り受信時刻
  // corr_unix_time: number or null
  char corr_buf[32];
  if (isnan(e.corr_unix_time)) {
    strcpy(corr_buf, "null");
  } else {
    snprintf(corr_buf, sizeof(corr_buf), "%.6f", e.corr_unix_time);
  }
  // cycle_count: number or null
  char cc_buf[16];
  if (e.cycle_count_valid) {
    snprintf(cc_buf, sizeof(cc_buf), "%u", (unsigned)e.cycle_count);
  } else {
    strcpy(cc_buf, "null");
  }
  char meta[20]; fmt_meta(meta, 0x01);
  // 送信アンカー (v2.1.0): この rx_dl を broadcast する直前の esp_timer/TSF。
  // 上り OWD の HID->air leg を「このアンカーと off-board air 観測値の差」で求める (§4.3)。
  // 下り受信時刻 (t_rx_*) とは別物 (差は HID の受信→上り発行処理時間)。
  uint64_t t_tx_local = (uint64_t)esp_timer_get_time();
  uint64_t t_tx_tsf   = tsf_calc(ca, cb, t_tx_local);  // 同一スナップショット (issue #9)
  int n = snprintf(buf, sizeof(buf),
      "{\"meta\":\"%s\",\"type\":\"rx_dl\",\"hid_seq\":%u,\"dl_seq\":%u,"
      "\"t_rx_tsf_us\":%llu,\"t_rx_esp_timer_us\":%llu,"
      "\"t_tx_tsf_us\":%llu,\"t_tx_esp_timer_us\":%llu,"
      "\"frame_size\":%u,\"rssi\":%d,\"corr_unix_time\":%s,\"cycle_count\":%s}\n",
      meta, (unsigned)s_hid_seq, (unsigned)s_dl_seq,
      (unsigned long long)t_tsf, (unsigned long long)e.t_local_us,
      (unsigned long long)t_tx_tsf, (unsigned long long)t_tx_local,
      (unsigned)e.frame_size, (int)e.rssi, corr_buf, cc_buf);
  if (n <= 0) return;
  s_udp.beginPacket(s_broadcast, (uint16_t)(52000 + s_robot_id));
  s_udp.write((const uint8_t*)buf, (size_t)n);
  s_udp.endPacket();
  s_hid_seq++;
  s_dl_seq++;
}

// emit_tx_ul は廃止 (robot_comm_spec v2.1.0 で tx_ul を任意化。上り計測は rx_dl の
// 送信アンカー t_tx_tsf_us / hb の t_now_tsf_us で代替するため、本実装では発行しない)。

void emit_hb() {
  double ca, cb;
  if (!cal_snapshot(&ca, &cb)) return;  // 一貫スナップショット (issue #9)
  char buf[256];
  uint64_t t_now_local = (uint64_t)esp_timer_get_time();
  uint64_t t_now_tsf = tsf_calc(ca, cb, t_now_local);
  char meta[20]; fmt_meta(meta, 0x03);
  int n = snprintf(buf, sizeof(buf),
      "{\"meta\":\"%s\",\"type\":\"hb\",\"hid_seq\":%u,\"t_now_tsf_us\":%llu,"
      "\"t_now_esp_timer_us\":%llu,\"dropped\":%u,\"cal_n\":%u}\n",
      meta, (unsigned)s_hid_seq, (unsigned long long)t_now_tsf,
      (unsigned long long)t_now_local, (unsigned)g_dropped,
      (unsigned)g_cal_n);
  if (n <= 0) return;
  s_udp.beginPacket(s_broadcast, (uint16_t)(52000 + s_robot_id));
  s_udp.write((const uint8_t*)buf, (size_t)n);
  s_udp.endPacket();
  s_hid_seq++;
}

// ---- PPS 出力 (TSF 1秒境界で GPIO パルス + pps JSON broadcast) ----
//   docs/pps_sync_design.md §4。HID は USB 不接続のため TSF ラベルを
//   radio_metrics broadcast (52000+id) で送る (sniffer は UART)。
int                s_pps_gpio = -1;
gptimer_handle_t   s_pps_gptimer = nullptr;
volatile uint64_t  s_pps_pulse_esp = 0;   // ISR: パルス発火時の esp_timer (us)
volatile bool      s_pps_pending = false;
bool               s_pps_started = false;

// GPTimer を auto-reload 1Hz で自走 (loop 非依存で PPS が途切れない)。ISR でパルス
// 完結 + 発火時刻 esp_timer 捕捉。位相は loop が回った時だけ TSF 境界へ再同期。
// 1000Hz rx で loop が starve しても PPS は継続 (ppm drift のみ、record は実 TSF)。
bool IRAM_ATTR pps_on_alarm(gptimer_handle_t timer,
                            const gptimer_alarm_event_data_t* edata, void* arg) {
  uint64_t pe = (uint64_t)esp_timer_get_time();
  if (s_pps_gpio >= 0) {
    gpio_set_level((gpio_num_t)s_pps_gpio, 1);
    esp_rom_delay_us(50);
    gpio_set_level((gpio_num_t)s_pps_gpio, 0);
  }
  s_pps_pulse_esp = pe;
  s_pps_pending = true;
  return false;
}

// 位相再同期: 次発火が TSF 1e6 境界に来るよう raw count を調整 (loop が回った時のみ)。
// sniffer と同じ絶対境界定義 = オシロ Δt 比較の前提 (docs §4.1, §10)
void pps_rephase() {
  if (!g_cal_valid || !s_pps_gptimer) return;
  uint64_t now_local = (uint64_t)esp_timer_get_time();
  uint64_t now_tsf = tsf_from_local(now_local);
  if (now_tsf == 0) return;
  uint64_t next_tsf = ((now_tsf / 1000000ULL) + 1ULL) * 1000000ULL;
  uint64_t next_local = local_from_tsf(next_tsf);
  int64_t  delay_us = (int64_t)(next_local - now_local);
  while (delay_us < 1000) {
    next_tsf += 1000000ULL;
    next_local = local_from_tsf(next_tsf);
    delay_us = (int64_t)(next_local - now_local);
  }
  uint64_t d = (uint64_t)delay_us % 1000000ULL;
  gptimer_set_raw_count(s_pps_gptimer, (1000000ULL - d) % 1000000ULL);
}

void emit_pps(uint64_t tsf_us, uint64_t esp_us) {
  char buf[128];
  int n = snprintf(buf, sizeof(buf),
      "{\"type\":\"pps\",\"t_pps_tsf_us\":%llu,\"t_pps_esp_timer_us\":%llu}\n",
      (unsigned long long)tsf_us, (unsigned long long)esp_us);
  if (n <= 0) return;
  s_udp.beginPacket(s_broadcast, (uint16_t)(52000 + s_robot_id));
  s_udp.write((const uint8_t*)buf, (size_t)n);
  s_udp.endPacket();
}

// ---- rx_dlb (type 0x04) batch 蓄積 + flush ----
// flush: 蓄積 0 件でない batch を 1 UDP broadcast。base=先頭 record の TSF で
// delta 圧縮、tx=送信直前 TSF (上り HID→air アンカー、batch 単位)。calib 未確定なら破棄。
// UDP payload を ≤1400B に厳守し、超過分は次 batch へ繰越す (フラグメント禁止、§3.1.1)。
void flush_rx_dlb() {
  if (s_batch_n == 0) return;
  double ca, cb;
  if (!cal_snapshot(&ca, &cb)) { s_batch_n = 0; return; }  // TSF 未確定は発行見送り (破棄)
  uint64_t base = tsf_calc(ca, cb, s_batch[0].t_local_us);
  uint64_t t_tx_local = (uint64_t)esp_timer_get_time();
  uint64_t tx = tsf_calc(ca, cb, t_tx_local);   // 上り HID→air アンカー (§4.3、batch 単位)
  char meta[20]; fmt_meta(meta, 0x04);
  char buf[1500];
  int n = snprintf(buf, sizeof(buf),
      "{\"meta\":\"%s\",\"type\":\"rx_dlb\",\"bseq\":%u,\"rxc\":%u,"
      "\"tx\":%llu,\"base\":%llu,\"recs\":[",
      meta, (unsigned)s_bseq, (unsigned)s_rxc,
      (unsigned long long)tx, (unsigned long long)base);
  if (n <= 0) { s_batch_n = 0; return; }
  size_t i = 0;
  for (; i < s_batch_n; i++) {
    char cc[16];
    if (s_batch[i].cycle_valid) snprintf(cc, sizeof(cc), "%u", (unsigned)s_batch[i].cycle);
    else { strcpy(cc, "null"); }
    uint64_t t_rx = tsf_calc(ca, cb, s_batch[i].t_local_us);
    int32_t  delta = (int32_t)((int64_t)t_rx - (int64_t)base);
    char rec[48];
    int rn = snprintf(rec, sizeof(rec), "%s[%s,%ld,%d]",
                      (i ? "," : ""), cc, (long)delta, (int)s_batch[i].rssi);
    if (rn <= 0) continue;
    // ≤1400B 厳守 ("]}\n" 終端 3B 余地確保)。超過したら break して残りは次 batch へ。
    if (n + rn > 1400 - 3) break;
    memcpy(buf + n, rec, (size_t)rn);
    n += rn;
  }
  n += snprintf(buf + n, sizeof(buf) - n, "]}\n");
  s_udp.beginPacket(s_broadcast, (uint16_t)(52000 + s_robot_id));
  s_udp.write((const uint8_t*)buf, (size_t)n);
  s_udp.endPacket();
  s_hid_seq++;   // meta.hid_seq = この batch フレームの seq (全 frame 通し)
  s_bseq++;      // batch 連番 +1
  // ≤1400B 超過で送れなかった残レコードを先頭へ繰越し (i>0 のときのみ memmove)。
  size_t rem = s_batch_n - i;
  if (rem > 0 && i > 0) memmove(s_batch, s_batch + i, rem * sizeof(BatchRec));
  s_batch_n = rem;
  s_batch_start_ms = millis();   // 次 batch の経過 flush 基点を更新
}

void batch_add(const Entry& e) {
  s_rxc++;   // 累積 rx_dl 受信数 (送出有無に関わらずカウント)
  if (s_batch_n == 0) s_batch_start_ms = millis();
  if (s_batch_n < BATCH_K) {
    s_batch[s_batch_n].t_local_us = e.t_local_us;
    s_batch[s_batch_n].cycle      = e.cycle_count;
    s_batch[s_batch_n].cycle_valid= e.cycle_count_valid;
    s_batch[s_batch_n].rssi       = e.rssi;
    s_batch_n++;
  }
  if (s_batch_n >= BATCH_K) flush_rx_dlb();   // 件数到達 flush
}

}  // namespace

// ============================================================
// Public API
// ============================================================
extern "C" void metrics_init(uint8_t robot_id, uint8_t subnet_third) {
  s_robot_id = robot_id;
  // subnet_third=0 → WiFi.localIP() から自動算出 (推奨)。
  // 互換性のため 1〜254 を渡された場合はそれを採用 (旧コード対応)。
  if (subnet_third == 0) {
    IPAddress me = WiFi.localIP();
    s_broadcast = IPAddress(me[0], me[1], me[2], 255);
  } else {
    s_broadcast = IPAddress(192, 168, subnet_third, 255);
  }
  s_udp.begin(0);  // arbitrary local port
  g_head = g_tail = 0;
  g_dropped = 0;
  g_cal_n = 0;
  g_cal_pos = 0;
  g_cal_valid = false;
  g_cal_a = 1.0;
  g_cal_b = 0.0;
  s_hid_seq = 0;
  s_dl_seq = 0;
  s_batch_n = 0;
  s_batch_start_ms = 0;
  s_bseq = 0;
  s_rxc = 0;
  s_hb_last_ms = millis();
  s_cal_last_ms = 0;
  s_inited = true;
}

// broadcast 先のみを現在の WiFi.localIP() から再計算する (init/PPS は触らない)。
// set_ssid 等で別サブネットへ移行した際に GOT_IP から呼び、52000+id の送信先が
// 旧サブネットの broadcast に固定されたままになるのを防ぐ。
extern "C" void metrics_update_broadcast(void) {
  if (!s_inited) return;
  IPAddress me = WiFi.localIP();
  s_broadcast = IPAddress(me[0], me[1], me[2], 255);
}

extern "C" void metrics_pps_enable(int gpio_pin) {
  // TSF 1秒境界で gpio_pin に PPS パルス出力 + pps JSON を 52000+id に broadcast。
  // metrics_init 後に呼ぶ。docs/pps_sync_design.md §4.2 (HID 系統)。
  // 冪等化 (issue #2 of bug-hunt): 二重 init 経路で再呼出されても gptimer を二重生成しない。
  // 二重生成すると最初のハンドルがリークし PPS パルスが二重に出て bridge を壊す。
  if (s_pps_gptimer != nullptr) return;
  s_pps_gpio = gpio_pin;
  gpio_reset_pin((gpio_num_t)gpio_pin);
  gpio_set_direction((gpio_num_t)gpio_pin, GPIO_MODE_OUTPUT);
  gpio_set_level((gpio_num_t)gpio_pin, 0);
  gptimer_config_t pps_tc = {};
  pps_tc.clk_src = GPTIMER_CLK_SRC_DEFAULT;
  pps_tc.direction = GPTIMER_COUNT_UP;
  pps_tc.resolution_hz = 1000000;   // 1 MHz = 1us tick
  gptimer_new_timer(&pps_tc, &s_pps_gptimer);
  gptimer_alarm_config_t pps_al = {};
  pps_al.alarm_count = 1000000;     // 1e6 tick = 1s (auto-reload で 1Hz 自走)
  pps_al.reload_count = 0;
  pps_al.flags.auto_reload_on_alarm = true;
  gptimer_set_alarm_action(s_pps_gptimer, &pps_al);
  gptimer_event_callbacks_t pps_cbs = {};
  pps_cbs.on_alarm = pps_on_alarm;
  gptimer_register_event_callbacks(s_pps_gptimer, &pps_cbs, nullptr);
  gptimer_enable(s_pps_gptimer);
  gptimer_start(s_pps_gptimer);     // 即 1Hz 自走 (位相は calib 確立後 loop で再同期)
  s_pps_started = false;
}

extern "C" void metrics_record_rx(const uint8_t* payload, size_t payload_len) {
  if (!s_inited) return;
  uint32_t head = g_head;
  uint32_t next = (head + 1) & (RING_N - 1);
  if (next == g_tail) { g_dropped++; return; }
  Entry &e = g_ring[head];
  e.type = ENTRY_RX;
  e.tx_port = 0;
  e.frame_size = (uint16_t)payload_len;
  e.t_local_us = (uint64_t)esp_timer_get_time();
  e.rssi = (int8_t)WiFi.RSSI();  // 下り受信時の STA-AP 受信強度 (dBm)。radio_metrics.md rx_dl.rssi
  if (payload_len >= 46 && payload != nullptr) {
    double v;
    memcpy(&v, payload + 38, sizeof(double));
    e.corr_unix_time = v;
  } else {
    e.corr_unix_time = NAN;
  }
  // cycle_count: payload offset 51-53 (24bit LE、downlink_command.md)
  if (payload_len >= 54 && payload != nullptr) {
    uint32_t cc = (uint32_t)payload[51]
                | ((uint32_t)payload[52] << 8)
                | ((uint32_t)payload[53] << 16);
    e.cycle_count = cc;
    e.cycle_count_valid = true;
  } else {
    e.cycle_count = 0;
    e.cycle_count_valid = false;
  }
  __sync_synchronize();
  g_head = next;
}

extern "C" void metrics_record_tx(uint16_t tx_port, uint16_t frame_size) {
  // tx_ul は robot_comm_spec v2.1.0 で任意化。上り OWD は rx_dl (送信アンカー
  // t_tx_tsf_us) / hb (t_now_tsf_us) の自己アンカーで計測できるため、本実装では
  // tx_ul を発行しない (一旦実装から除外)。callers 互換のため API は no-op で残す。
  (void)tx_port;
  (void)frame_size;
}

extern "C" void metrics_task(void) {
  if (!s_inited) return;
  uint32_t now_ms = millis();

  if (now_ms - s_cal_last_ms >= 100) {
    s_cal_last_ms = now_ms;
    run_calibration_step();
  }

  // PPS: calib 確立後に初回スケジュール、callback 後に pps JSON broadcast + 次境界へ
  if (s_pps_gpio >= 0) {
    if (g_cal_valid && !s_pps_started) {
      s_pps_started = true;
      pps_rephase();
    }
    if (s_pps_pending) {
      s_pps_pending = false;
      uint64_t pe = s_pps_pulse_esp;
      uint64_t tsf = tsf_from_local(pe);   // 発火時刻の実 TSF
      emit_pps(tsf, pe);
      pps_rephase();   // loop が回った時のみ drift 補正 (starve 時は自走継続)
    }
  }

  while (g_tail != g_head) {
    const Entry &e = g_ring[g_tail];
    if (e.type == ENTRY_RX) {
#if RX_DL_BATCH
      batch_add(e);                         // 0x04 batch に蓄積 (件数到達で自動 flush)
#else
      emit_rx_dl(e);                        // 0x01 per-frame (tx_ul は廃止)
#endif
    }
    g_tail = (g_tail + 1) & (RING_N - 1);
  }
#if RX_DL_BATCH
  // 経過 flush: 件数未達でも 0.5s 経過で送出 (報告鮮度 ≤0.5s を保証)
  if (s_batch_n > 0 && (now_ms - s_batch_start_ms) >= BATCH_FLUSH_MS) flush_rx_dlb();
#endif

  if (now_ms - s_hb_last_ms >= 1000) {
    s_hb_last_ms = now_ms;
    emit_hb();
  }
}
