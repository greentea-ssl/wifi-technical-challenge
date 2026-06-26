// sniffer — 本番 ESP32-C5 sniffer ファーム
//
// Phase 0 r12_promisc_test の後継。バイナリプロトコル + UART 2 Mbps + 大きい
// ring + 段階フィルタで、会場想定 2000〜5000 pps への耐性を持たせる。
// architecture.md §4.2.1 に従う。
//
// 出力フォーマット: 同期マーカ `0xC5 0xC5` + 固定長レコード。host runner が
// バイナリ decode して CSV/Parquet 化する。
//
// レコード種別:
//   0x01 (frame, 40 bytes 全体): 1 つの promiscuous キャプチャ
//   0x02 (heartbeat, 20 bytes 全体): 1Hz、累積カウンタ
//   0x03 (cal_recv, 28 bytes 全体): UDP 43000 で受けた calibration パケットの記録
//        (cal_sender.py が unicast/broadcast の AP 処理時間絶対値を測るため使用)
//
// 段階 cb フィルタ:
//   1. WIFI_PKT_DATA 以外を即 reject
//   2. sig_len < 24 (802.11 ヘッダ未満) を reject
//   3. cur_bb_format で HE/VHT 等のみ通す (オプション、定数で制御)
//   4. src_mac == 学習済み BSSID なら通す
//
// FQBN:
//   devkit C5:  esp32:esp32:esp32c5  (CP2102N、UART 2Mbps 想定)
//   XIAO C5:    esp32:esp32:XIAO_ESP32C5  (native USB CDC、baud は無視される)

#include <WiFi.h>
#include <Preferences.h>
#include <WiFiUdp.h>
#include <esp_wifi.h>
#include <esp_wifi_types.h>
#include <esp_timer.h>
#include <esp_rom_sys.h>
#include <driver/gpio.h>
#include <driver/gptimer.h>
#include <string.h>

// ============================================================
// User-tunable
// ============================================================
static const char*    WIFI_SSID = "TEAM_SSID_OPEN";  // 既定 (NVS 未設定時 / 初回)
static const char*    WIFI_PASS = "";
// 対象 AP は runtime 変更可。NVS (Preferences) に永続化し、host から UART コマンド
//   "~CFG <ssid>\t<pass>\n"  (cfg_apply で再 associate)
// で都度切替する。会場 AP が事前不明でも UART 経由でその場設定できる。
static char g_ssid[33] = "";   // 現対象 SSID (最大 32)
static char g_pass[65] = "";   // 現対象 passphrase (最大 64、空=open)
static const uint32_t SERIAL_BAUD = 2000000;     // CP2102N の実用上限
static const uint8_t  CHANNEL_HINT = 0;          // 0 = associate 結果に従う
static const bool     FILTER_HE_ONLY = false;    // true なら bb_format>=4 のみ
static const size_t   RING_N = 2048;             // 2の累乗、84KB 程度 (Entry=42B 拡張に伴い RAM 余裕確保)
static const uint16_t CAL_PORT = 43000;          // UDP cal listener port
static const bool     ENABLE_PROMISCUOUS = true;  // true = air capture (本走時); false = pure STA cal-target mode (cal_sender 試験時のみ)
static const int      PPS_GPIO = 10;              // TSF 1秒境界で PPS パルス出力 (HID と同じ GPIO10、オシロ/RasPi pps-gpio、docs/pps_sync_design.md)

// ============================================================
// dst MAC フィルタ (スマホ等の非計測下りトラフィックを cb で除外)
//   現状の段5 (src=BSSID) は「AP 送信の全 data frame」を通すため、AP→スマホ
//   下り (動画/DL) も ring に入り cb 負荷源になる。dst (addr1) を計測対象に
//   絞ることで過負荷時の dropped (計測 frame 取りこぼし) を防ぐ。
//   - broadcast (ff:ff:ff:ff:ff:ff): 計測 broadcast (cal/metrics) 用に常に通す
//   - multicast (group bit、mDNS 等): 計測対象外なので除外
//   - unicast: DST_FILTER_MODE で OUI (ベンダー=上位24bit) / MAC (個体48bit) 切替
// ============================================================
static const bool     ENABLE_DST_FILTER = true;   // false = 旧挙動 (dst フィルタ無効、全 AP 下り通す)
enum DstFilterMode { DSTFILTER_OUI, DSTFILTER_MAC };
static const DstFilterMode DST_FILTER_MODE = DSTFILTER_OUI;  // ★ 後から OUI / MAC を切替

// OUI 許可リスト (上位 24bit、Espressif 製ロボット)。実機確認して追加していく
static const uint8_t TARGET_OUIS[][3] = {
  { 0xD0, 0xCF, 0x13 },   // ESP32-C5 devkit (sniffer/reflector、確認済)
  { 0x38, 0x44, 0xBE },   // XIAO ESP32-C5 HID (robot1-6、実機確認済 2026-06-22)。
                          // 下り(addr1=HID)・上り ToDS 原送信(addr2=HID) 両方を air 捕捉 → 区間分解可
};

// MAC 許可リスト (48bit 全体、個体指定)。DSTFILTER_MAC モード時に使用
static const uint8_t TARGET_MACS[][6] = {
  { 0xD0, 0xCF, 0x13, 0xE0, 0xB5, 0x10 },  // reflector devkit
  // { ... },  // 本走 robot の MAC をここに列挙
};

// ============================================================
// Binary protocol
// ============================================================
static const uint8_t SYNC_LO = 0xC5;
static const uint8_t SYNC_HI = 0xC5;
static const uint8_t TYPE_FRAME = 0x01;
static const uint8_t TYPE_HB    = 0x02;
static const uint8_t TYPE_CAL   = 0x03;
static const uint8_t TYPE_PPS   = 0x04;
static const uint8_t LEN_FRAME = 49;  // payload bytes after [sync,type,len] = sizeof(Entry)=47 + 2 (dropped_lo)。v3 (cycle_count + robot_id 付き)
static const uint8_t LEN_HB    = 16;
static const uint8_t LEN_CAL   = 24;  // 8B t_recv_local + 8B cal_send_RT_double + 4B cal_seq + 1B type + 3B pad
static const uint8_t LEN_PPS   = 16;  // 8B tsf_at_pps + 8B esp_timer_at_pps

// ============================================================
// Captured-frame record (in-ring, 40 bytes)
// ============================================================
struct __attribute__((packed)) Entry {
  uint32_t rx_seq;            // firmware monotonic counter
  uint32_t t_local_us_lo;     // esp_timer low 32 bits
  uint32_t rx_timestamp_us;   // wifi_pkt_rx_ctrl_t.timestamp (chip local clock, NOT TSF)
  uint64_t tsf_us;            // AP TSF at cb time, via esp_timer↔TSF midpoint fit
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
  uint32_t cycle_count;       // UDP payload offset 51-53 (24bit LE)。0xFFFFFFFF=未取得
  uint8_t  robot_id;          // v3: 計測対象 robot_id。DL=payload off02&0x0F、radio_metrics=meta byte4。0xFF=未取得
};
static_assert(sizeof(Entry) == 47, "Entry must be 47 bytes (packed)");

static Entry              g_ring[RING_N];
static volatile uint32_t  g_head = 0;
static volatile uint32_t  g_tail = 0;
static volatile uint32_t  g_dropped_total = 0;
static volatile uint32_t  g_captured_total = 0;
static volatile uint32_t  g_cb_total = 0;          // every cb invocation
static volatile uint32_t  g_cb_data_only = 0;      // type==WIFI_PKT_DATA だけ通過
static uint32_t           g_rx_seq = 0;

static uint8_t g_bssid[6] = {0};

// ============================================================
// TSF calibration (esp_timer ↔ AP TSF 中点フィット、Phase 0 R4 と同手法)
// 主タスクで CALIB_PERIOD_MS ごとに更新、cb から read してフレーム毎の TSF を算出
// ============================================================
static volatile uint64_t g_calib_tsf_us = 0;     // last calibration: TSF
static volatile uint64_t g_calib_esp_us = 0;     // last calibration: esp_timer midpoint
static volatile bool     g_calib_valid = false;
// seqlock: 64bit calib は RISC-V 32bit で非アトミック。loop(writer) と cb(WiFi task, reader)
// が競合すると上位/下位 32bit が別世代になる torn read で TSF が巨大外れ値になる (issue #4.1)。
// writer は更新前後に g_calib_seq を ++ (奇数=更新中)、reader は seq 不変かつ偶数を確認して retry。
static volatile uint32_t g_calib_seq = 0;

// calib のスナップショットを一貫読み (torn read 防止)。戻り値 = 有効か。
static inline bool calib_snapshot(uint64_t *tsf, uint64_t *esp) {
  uint32_t s0, s1;
  bool valid;
  do {
    s0 = g_calib_seq;
    __sync_synchronize();
    *tsf = g_calib_tsf_us;
    *esp = g_calib_esp_us;
    valid = g_calib_valid;
    __sync_synchronize();
    s1 = g_calib_seq;
  } while ((s0 & 1u) || s0 != s1);   // 更新中(奇数) or 途中変化なら retry
  return valid;
}
static uint32_t          g_last_calib_ms = 0;
static const uint32_t    CALIB_PERIOD_MS = 100;  // 10 Hz 更新
static bool    g_bssid_known = false;

static WiFiUDP g_udp_cal;
static uint8_t g_cal_buf[256];

// ============================================================
// PPS 出力 (docs/pps_sync_design.md §4.1)
//   - GPTimer (1MHz) を auto-reload 1e6 tick = 1Hz で **自走** させる。アラームは
//     loop に依存せず必ず発火するので、高負荷で loop が starve しても PPS は途切れない。
//   - アラーム ISR でパルス完結 (立上り→50us→立下り) + 発火時刻 esp_timer を捕捉。
//   - 位相 (TSF 1e6 境界への整合) は loop が回った時だけ再同期 (pps_rephase)。
//     loop starve 時は再同期されず ppm drift するが PPS 自体は継続。
//   - record は ISR 捕捉した esp_timer から実 TSF を計算 (round 境界に依存しない)。
// ============================================================
static gptimer_handle_t   g_pps_gptimer = nullptr;
static volatile uint64_t  g_pps_pulse_esp = 0;    // ISR: パルス発火時の esp_timer (us)
static volatile bool      g_pps_pending = false;  // ISR → loop へ marker 出力依頼
static bool               g_pps_started = false;

static bool IRAM_ATTR pps_on_alarm(gptimer_handle_t timer,
                                   const gptimer_alarm_event_data_t* edata, void* arg) {
  uint64_t pe = (uint64_t)esp_timer_get_time();   // 発火時刻を即捕捉 (ISR = パルス瞬間)
  gpio_set_level((gpio_num_t)PPS_GPIO, 1);
  esp_rom_delay_us(50);
  gpio_set_level((gpio_num_t)PPS_GPIO, 0);
  g_pps_pulse_esp = pe;
  g_pps_pending = true;
  return false;
}

// 位相再同期: 次の発火が TSF 1e6 境界ちょうどに来るよう GPTimer の raw count を調整。
// raw=1e6-d にすると d us 後 (= 次境界) に alarm(1e6) へ到達 → 以後 auto-reload で 1Hz。
// loop が回った時のみ呼ぶ (drift 補正)。starve 時はスキップされても自走は継続。
static void pps_rephase() {
  if (!g_calib_valid || !g_pps_gptimer) return;
  uint64_t now_esp = (uint64_t)esp_timer_get_time();
  uint64_t now_tsf = g_calib_tsf_us + (now_esp - g_calib_esp_us);
  uint64_t next_tsf = ((now_tsf / 1000000ULL) + 1ULL) * 1000000ULL;
  int64_t  delay_us = (int64_t)((g_calib_esp_us + (next_tsf - g_calib_tsf_us)) - now_esp);
  while (delay_us < 1000) { next_tsf += 1000000ULL; delay_us += 1000000; }
  uint64_t d = (uint64_t)delay_us % 1000000ULL;
  gptimer_set_raw_count(g_pps_gptimer, (1000000ULL - d) % 1000000ULL);
}

// dst (addr1) が計測対象か判定 (broadcast / 許可 OUI or MAC)。multicast は除外。
static inline bool IRAM_ATTR dst_is_target(const uint8_t *a1) {
  // broadcast (全 ff) は計測 broadcast (cal/metrics) 用に常に通す
  if (a1[0]==0xFF && a1[1]==0xFF && a1[2]==0xFF &&
      a1[3]==0xFF && a1[4]==0xFF && a1[5]==0xFF) return true;
  // multicast (group bit set、broadcast 以外 = mDNS 等) は除外
  if (a1[0] & 0x01) return false;
  // unicast: モードに応じて OUI (上位24bit) / MAC (48bit) で許可リスト判定
  if (DST_FILTER_MODE == DSTFILTER_OUI) {
    for (size_t i = 0; i < sizeof(TARGET_OUIS) / 3; i++)
      if (memcmp(a1, TARGET_OUIS[i], 3) == 0) return true;
  } else {
    for (size_t i = 0; i < sizeof(TARGET_MACS) / 6; i++)
      if (memcmp(a1, TARGET_MACS[i], 6) == 0) return true;
  }
  return false;  // スマホ等 (非許可) を除外
}

// ============================================================
// Promiscuous callback (WiFi task context)
// ============================================================
static void IRAM_ATTR promisc_cb(void *buf, wifi_promiscuous_pkt_type_t type) {
  g_cb_total++;
  if (type != WIFI_PKT_DATA) return;
  g_cb_data_only++;
  const wifi_promiscuous_pkt_t *pkt = (const wifi_promiscuous_pkt_t*)buf;
  const wifi_pkt_rx_ctrl_t *rx = &pkt->rx_ctrl;
  const uint8_t *frame = pkt->payload;

  // Stage 1: minimum header length
  if (rx->sig_len < 24) return;

  // Stage 2 (optional): HE-only filter
  if (FILTER_HE_ONLY && rx->cur_bb_format < 4) return;

  if (!g_bssid_known) return;

  // Stage 3+4: AP と計測対象 STA 間のフレームを双方向で通す。
  //   下り / AP 再送 (FromDS): addr2==BSSID かつ addr1 が target (broadcast 含む)
  //   上り 原送信   (ToDS)   : addr1==BSSID かつ addr2 が target (= HID)
  // 上り原送信を通すことで、HID が実際に空中送出した瞬間 (= 真の "air") を観測でき、
  // HID→air→wire を直列分解できる (AP の FromDS 再送ではなく STA の TX を捕捉)。
  const uint8_t *addr1 = frame + 4;   // RA / dst
  const uint8_t *addr2 = frame + 10;  // TA / src
  bool from_ap = (memcmp(addr2, g_bssid, 6) == 0);
  bool to_ap   = (memcmp(addr1, g_bssid, 6) == 0);
  if (from_ap) {
    if (ENABLE_DST_FILTER && !dst_is_target(addr1)) return;  // 下り / 再送
  } else if (to_ap) {
    if (ENABLE_DST_FILTER && !dst_is_target(addr2)) return;  // 上り原送信 (src=HID)
  } else {
    return;  // AP と無関係なフレーム (他 STA 間など)
  }

  uint32_t head = g_head;
  uint32_t next = (head + 1) & (RING_N - 1);
  if (next == g_tail) {
    g_dropped_total++;
    return;
  }
  Entry &e = g_ring[head];
  uint64_t t_local = (uint64_t)esp_timer_get_time();
  e.rx_seq          = g_rx_seq++;
  e.t_local_us_lo   = (uint32_t)(t_local & 0xFFFFFFFFu);
  e.rx_timestamp_us = (uint32_t)rx->timestamp;
  // TSF at cb time = calib_tsf + (esp_now - calib_esp). 主タスクが
  // ~100ms 周期で (calib_tsf, calib_esp) を更新する。drift は ppm 級で
  // 100ms 当たり ~ns、worst でも 10us 以下なので bridge 用途に十分。
  {
    uint64_t ctsf, cesp;
    if (calib_snapshot(&ctsf, &cesp)) {   // seqlock 一貫読み (torn read 防止、issue #4.1)
      e.tsf_us = ctsf + (t_local - cesp);
    } else {
      e.tsf_us = 0;  // calibration not yet done (boot 直後)
    }
  }
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
  // cycle_count: downlink_command.md offset 51-53 (24bit LE)。
  // stage4 通過後 (= 計測対象 frame) のみ parse するので過負荷源にならない。
  // open auth (暗号ヘッダ無し) 前提。802.11 hdr → LLC/SNAP(8) → IPv4 → UDP(8) を
  // 可変長で飛ばし、全 offset を sig_len で bounds-check (truncation 時は未取得=0xFFFFFFFF)。
  // cycle_count 列の 2 系統:
  //  (a) DL command 等: UDP payload offset 51-53 の binary (24bit LE、downlink_command.md)
  //  (b) radio_metrics frame: payload 先頭 {"meta":"524D..(18hex)..."} の hid_seq
  //      (offset 19-26 の ASCII hex、radio_metrics.md §3.0)。上り/hb の air/wire join key。
  //      meta 判定を優先し、無ければ offset 51-53 を読む。
  //      ※ meta は type byte 非依存に parse するので rx_dl(0x01)/hb(0x03)/rx_dlb(0x04 batch)
  //        いずれも透過。rx_dlb は air↔socket join が batch フレーム単位 (上り leg は batch 粒度)。
  e.cycle_count = 0xFFFFFFFFu;
  e.robot_id = 0xFF;
  {
    uint16_t hdrlen = (frame[0] & 0x80) ? 26 : 24;   // QoS data は QoS control +2
    if (frame[1] & 0x80) hdrlen += 4;                 // HT Control (order bit) +4
    uint32_t slen = rx->sig_len;
    if (slen >= (uint32_t)hdrlen + 8 + 28) {
      const uint8_t *llc = frame + hdrlen;
      if (llc[6] == 0x08 && llc[7] == 0x00) {         // SNAP ethertype = IPv4
        const uint8_t *ip = llc + 8;
        uint16_t ihl = (uint16_t)(ip[0] & 0x0F) * 4;
        const uint8_t *up = ip + ihl + 8;             // UDP payload 先頭
        uint32_t up_off = (uint32_t)(up - frame);
        if (up_off + 27 <= slen &&
            up[0]=='{'&&up[1]=='"'&&up[2]=='m'&&up[3]=='e'&&up[4]=='t'&&up[5]=='a'&&
            up[6]=='"'&&up[7]==':'&&up[8]=='"'&&
            up[9]=='5'&&up[10]=='2'&&up[11]=='4'&&up[12]=='D') {
          uint32_t v = 0; bool ok = true;             // (b) meta hid_seq (offset 19-26)
          for (int i = 19; i < 27; i++) {
            uint8_t c = up[i], d;
            if (c>='0'&&c<='9') d = c - '0';
            else if (c>='A'&&c<='F') d = c - 'A' + 10;
            else if (c>='a'&&c<='f') d = c - 'a' + 10;
            else { ok = false; break; }
            v = (v << 4) | d;
          }
          if (ok) e.cycle_count = v;
          // meta byte4 = robot_id (ASCII hex up[17],up[18]、radio_metrics.md §3.0)
          {
            uint8_t hi = up[17], lo = up[18], dh, dl; bool rok = true;
            if (hi>='0'&&hi<='9') dh = hi-'0'; else if (hi>='A'&&hi<='F') dh = hi-'A'+10;
            else if (hi>='a'&&hi<='f') dh = hi-'a'+10; else rok = false;
            if (lo>='0'&&lo<='9') dl = lo-'0'; else if (lo>='A'&&lo<='F') dl = lo-'A'+10;
            else if (lo>='a'&&lo<='f') dl = lo-'a'+10; else rok = false;
            if (rok) e.robot_id = (dh << 4) | dl;
          }
        } else if (up_off + 54 <= slen) {             // (a) DL offset 51-53 binary
          e.cycle_count = (uint32_t)up[51]
                        | ((uint32_t)up[52] << 8)
                        | ((uint32_t)up[53] << 16);
          e.robot_id = up[2] & 0x0F;                   // DL command robot_id (payload offset 02 low nibble)
        }
      }
    }
  }
  __sync_synchronize();
  g_head = next;
  g_captured_total++;
}

// ============================================================
// Output helpers
// ============================================================
static inline void write_frame_record(const Entry &e) {
  // total = 4 (SYNC_LO, SYNC_HI, TYPE_FRAME, LEN_FRAME=49) + Entry(47B) + dropped_lo(2B) = 53 bytes (v3)
  uint8_t hdr[4] = { SYNC_LO, SYNC_HI, TYPE_FRAME, LEN_FRAME };
  Serial.write(hdr, sizeof(hdr));
  Serial.write((const uint8_t*)&e, sizeof(e));
  uint16_t dropped_lo = (uint16_t)(g_dropped_total & 0xFFFFu);
  Serial.write((const uint8_t*)&dropped_lo, sizeof(dropped_lo));
  // flush は per-record では行わない (issue #3)。record 毎の Serial.flush() は TX FIFO
  // が空くまでブロックし、その間 promisc cb が ring を埋めて高 pps 時に drop する。
  // drain ループを抜けた後に loop() で 1 回だけ flush する。
  // (bridge_offset は GPIO PPS の unix_assert 基準になり UART 到達時刻に非依存になったため、
  //  per-record flush による UART 到達遅延最小化は不要 — issue #1 修正と整合)
}

static inline void write_pps_record(uint64_t tsf_us, uint64_t esp_us) {
  // 20 bytes total: [SYNC, SYNC, TYPE_PPS, LEN_PPS=16] + 8B tsf + 8B esp_timer
  uint8_t hdr[4] = { SYNC_LO, SYNC_HI, TYPE_PPS, LEN_PPS };
  Serial.write(hdr, sizeof(hdr));
  Serial.write((const uint8_t*)&tsf_us, sizeof(tsf_us));
  Serial.write((const uint8_t*)&esp_us, sizeof(esp_us));
  // flush は loop() の drain 後 1 回 (issue #3)。tsf 値は payload 内なので到達遅延に非依存。
}

static inline void write_cal_record(uint64_t t_recv_local_us, double cal_send_rt, uint32_t cal_seq, uint8_t type_flag) {
  // 28 bytes total: [SYNC, SYNC, TYPE_CAL, LEN_CAL=24] + 24-byte payload
  uint8_t hdr[4] = { SYNC_LO, SYNC_HI, TYPE_CAL, LEN_CAL };
  Serial.write(hdr, sizeof(hdr));
  Serial.write((const uint8_t*)&t_recv_local_us, sizeof(t_recv_local_us));  // 8
  Serial.write((const uint8_t*)&cal_send_rt, sizeof(cal_send_rt));           // 8
  Serial.write((const uint8_t*)&cal_seq, sizeof(cal_seq));                    // 4
  Serial.write(&type_flag, 1);                                                // 1
  uint8_t pad[3] = {0,0,0};
  Serial.write(pad, 3);                                                       // 3
}

static inline void write_hb_record() {
  uint8_t hdr[4] = { SYNC_LO, SYNC_HI, TYPE_HB, LEN_HB };
  Serial.write(hdr, sizeof(hdr));
  uint32_t captured = g_captured_total;
  uint32_t dropped  = g_dropped_total;
  uint64_t t_now    = (uint64_t)esp_timer_get_time();
  uint32_t t_now_lo = (uint32_t)(t_now & 0xFFFFFFFFu);
  int32_t  rssi_now = (int32_t)WiFi.RSSI();
  Serial.write((const uint8_t*)&captured, sizeof(captured));
  Serial.write((const uint8_t*)&dropped, sizeof(dropped));
  Serial.write((const uint8_t*)&t_now_lo, sizeof(t_now_lo));
  Serial.write((const uint8_t*)&rssi_now, sizeof(rssi_now));
}

// ============================================================
// 対象 AP 設定 (NVS 永続 + runtime 再 associate)
// ============================================================
static Preferences g_prefs;

// NVS から対象 SSID/pass を読む。未設定なら既定 (WIFI_SSID/WIFI_PASS)。
static void cfg_load() {
  g_prefs.begin("snif", true);   // read-only
  String s = g_prefs.getString("ssid", WIFI_SSID);
  String p = g_prefs.getString("pass", WIFI_PASS);
  g_prefs.end();
  strncpy(g_ssid, s.c_str(), sizeof(g_ssid) - 1); g_ssid[sizeof(g_ssid) - 1] = 0;
  strncpy(g_pass, p.c_str(), sizeof(g_pass) - 1); g_pass[sizeof(g_pass) - 1] = 0;
}

static void cfg_save() {
  g_prefs.begin("snif", false);  // read-write
  g_prefs.putString("ssid", g_ssid);
  g_prefs.putString("pass", g_pass);
  g_prefs.end();
}

// g_ssid/g_pass の AP へ (再) associate し、channel/BSSID 学習 + promiscuous/cal を張り直す。
// setup() からも runtime コマンドからも呼ぶ。戻り値: 接続成功可否。
static bool cfg_apply() {
  esp_wifi_set_promiscuous(false);   // 再設定中は捕捉停止 (未 ON でも無害)
  g_bssid_known = false; __sync_synchronize();
  // 再associate で TSF は新 AP 値へジャンプする。旧 AP の calib を無効化し、
  // 再接続後の最初の 100ms 較正が入るまで cb は tsf=0 を返す (stale calib 使用を防止、issue #4.2)
  g_calib_seq++; __sync_synchronize();
  g_calib_valid = false;
  __sync_synchronize(); g_calib_seq++;
  WiFi.disconnect(true); delay(100);
  WiFi.mode(WIFI_STA);
  Serial.printf("# Connecting to SSID=%s\n", g_ssid);
  WiFi.begin(g_ssid, g_pass);
  unsigned long t0 = millis();
  while (WiFi.status() != WL_CONNECTED && (millis() - t0) < 30000) delay(200);
  if (WiFi.status() != WL_CONNECTED) {
    Serial.println("# ERR: WiFi connect timeout");
    rgbLedWrite(RGB_BUILTIN, 16, 0, 0);  // 赤 = 接続失敗 (低輝度)
    return false;
  }
  rgbLedWrite(RGB_BUILTIN, 0, 0, 16);    // 青 = sniffer (接続済、低輝度)
  memcpy(g_bssid, WiFi.BSSID(), 6);
  __sync_synchronize();
  g_bssid_known = true;
  Serial.printf("# CONNECTED ch=%d rssi=%d bssid=%02X:%02X:%02X:%02X:%02X:%02X\n",
                WiFi.channel(), (int)WiFi.RSSI(),
                g_bssid[0], g_bssid[1], g_bssid[2], g_bssid[3], g_bssid[4], g_bssid[5]);
  if (ENABLE_PROMISCUOUS) {
    wifi_promiscuous_filter_t filt = { .filter_mask = WIFI_PROMIS_FILTER_MASK_DATA };
    esp_wifi_set_promiscuous_filter(&filt);
    esp_wifi_set_promiscuous_rx_cb(&promisc_cb);
    esp_wifi_set_promiscuous(true);
    Serial.println("# PROMISC_ON filter=DATA");
  } else {
    Serial.println("# PROMISC_OFF (pure STA mode, cal target only)");
  }
  g_udp_cal.stop();
  if (g_udp_cal.begin(CAL_PORT)) {
    Serial.printf("# CAL_UDP_OPEN port=%u ip=%s\n",
                  (unsigned)CAL_PORT, WiFi.localIP().toString().c_str());
  } else {
    Serial.println("# CAL_UDP_OPEN_FAILED");
  }
  return true;
}

// host→sniffer UART コマンド処理。"~CFG <ssid>\t<pass>\n" を受けて再 associate + NVS 保存。
static void poll_uart_cmd() {
  static char buf[128];
  static size_t len = 0;
  while (Serial.available()) {
    char c = (char)Serial.read();
    if (c == '\n' || c == '\r') {
      buf[len] = 0;
      if (len >= 5 && strncmp(buf, "~CFG ", 5) == 0) {
        char *rest = buf + 5;
        char *tab = strchr(rest, '\t');
        if (tab) { *tab = 0; strncpy(g_pass, tab + 1, sizeof(g_pass) - 1); g_pass[sizeof(g_pass)-1]=0; }
        else { g_pass[0] = 0; }
        strncpy(g_ssid, rest, sizeof(g_ssid) - 1); g_ssid[sizeof(g_ssid)-1]=0;
        Serial.printf("# CFG_RECV ssid=%s pass=%s\n", g_ssid, g_pass[0] ? "****" : "(open)");
        cfg_save();
        bool ok = cfg_apply();
        Serial.printf("# CFG_APPLIED ok=%d ssid=%s\n", (int)ok, g_ssid);
      }
      len = 0;
    } else if (len < sizeof(buf) - 1) {
      buf[len++] = c;
    } else {
      len = 0;  // overflow → drop line
    }
  }
}

// ============================================================
// Setup / loop
// ============================================================
void setup() {
  Serial.begin(SERIAL_BAUD);
  delay(200);
  Serial.println();
  Serial.printf("# sniffer v0 baud=%u ring=%u filter_he_only=%d\n",
                (unsigned)SERIAL_BAUD, (unsigned)RING_N, (int)FILTER_HE_ONLY);

  // RGB LED で役割を視認 (devkit 区別用): sniffer = 青、起動直後は黄
  rgbLedWrite(RGB_BUILTIN, 8, 8, 0);   // 黄 = boot/未接続 (低輝度)

  // PPS 出力 GPIO + esp_timer 初期化 (実 schedule は calib 確立後に loop で)
  gpio_reset_pin((gpio_num_t)PPS_GPIO);
  gpio_set_direction((gpio_num_t)PPS_GPIO, GPIO_MODE_OUTPUT);
  gpio_set_level((gpio_num_t)PPS_GPIO, 0);
  gptimer_config_t pps_tc = {};
  pps_tc.clk_src = GPTIMER_CLK_SRC_DEFAULT;
  pps_tc.direction = GPTIMER_COUNT_UP;
  pps_tc.resolution_hz = 1000000;   // 1 MHz = 1us tick
  gptimer_new_timer(&pps_tc, &g_pps_gptimer);
  gptimer_alarm_config_t pps_al = {};
  pps_al.alarm_count = 1000000;     // 1e6 tick = 1s (auto-reload で 1Hz 自走)
  pps_al.reload_count = 0;
  pps_al.flags.auto_reload_on_alarm = true;
  gptimer_set_alarm_action(g_pps_gptimer, &pps_al);
  gptimer_event_callbacks_t pps_cbs = {};
  pps_cbs.on_alarm = pps_on_alarm;
  gptimer_register_event_callbacks(g_pps_gptimer, &pps_cbs, nullptr);
  gptimer_enable(g_pps_gptimer);
  gptimer_start(g_pps_gptimer);   // 即 1Hz 自走開始 (位相は calib 確立後 loop で再同期)

  WiFi.mode(WIFI_STA);
  uint8_t mac[6];
  esp_wifi_get_mac(WIFI_IF_STA, mac);
  Serial.printf("# MAC=%02X:%02X:%02X:%02X:%02X:%02X\n",
                mac[0], mac[1], mac[2], mac[3], mac[4], mac[5]);

  cfg_load();   // NVS から対象 SSID/pass (未設定なら既定)
  Serial.printf("# TARGET ssid=%s pass=%s (host UART '~CFG <ssid>\\t<pass>' で変更可)\n",
                g_ssid, g_pass[0] ? "****" : "(open)");
  cfg_apply();  // 対象 AP へ associate + promiscuous/cal を張る (失敗時も後で再設定可)

  Serial.println("# BEGIN_BINARY  (sync=C5 C5  type=01 frame / 02 hb / 03 cal)");
}

void loop() {
  poll_uart_cmd();   // host→sniffer "~CFG <ssid>\t<pass>" を受けて対象 AP 切替

  // TSF calibration (esp_timer ↔ AP TSF 中点フィット、100ms 周期、
  // Phase 0 R4 と同手法)。WiFi 接続後のみ実行。
  uint32_t now_ms = millis();
  if (g_bssid_known && (now_ms - g_last_calib_ms) >= CALIB_PERIOD_MS) {
    g_last_calib_ms = now_ms;
    uint64_t t_before = (uint64_t)esp_timer_get_time();
    uint64_t tsf      = esp_wifi_get_tsf_time(WIFI_IF_STA);
    uint64_t t_after  = (uint64_t)esp_timer_get_time();
    if (tsf > 0) {
      // seqlock writer: ++ (奇数=更新中) → 書込 → ++ (偶数=確定)。cb の calib_snapshot と対 (issue #4.1)
      g_calib_seq++;
      __sync_synchronize();
      g_calib_tsf_us = tsf;
      g_calib_esp_us = (t_before + t_after) / 2;
      g_calib_valid  = true;
      __sync_synchronize();
      g_calib_seq++;
    }
  }

  // PPS: calib 確立後に初回位相合わせ。ISR (auto-reload) 発火毎に marker 出力 + 再同期。
  if (g_calib_valid && !g_pps_started) {
    g_pps_started = true;
    pps_rephase();
  }
  if (g_pps_pending) {
    g_pps_pending = false;
    uint64_t pe = g_pps_pulse_esp;
    uint64_t tsf = g_calib_tsf_us + (pe - g_calib_esp_us);   // 発火時刻の実 TSF
    write_pps_record(tsf, pe);
    pps_rephase();   // loop が回った時のみ drift 補正 (starve 時は自走継続)
  }

  // Drain ring → Serial
  bool wrote_any = (g_tail != g_head) || g_pps_pending;
  while (g_tail != g_head) {
    write_frame_record(g_ring[g_tail]);
    g_tail = (g_tail + 1) & (RING_N - 1);
  }
  // flush は drain 後 1 回だけ (issue #3)。per-record flush の高 pps drop を回避しつつ
  // UART driver buffer を 1 周期分まとめて掃き出す。
  if (wrote_any) Serial.flush();

  // Cal UDP receive — non-blocking. parsePacket() returns 0 if nothing.
  int n = g_udp_cal.parsePacket();
  if (n > 0) {
    uint64_t t_recv_local = (uint64_t)esp_timer_get_time();  // capture asap
    int len = g_udp_cal.read(g_cal_buf, sizeof(g_cal_buf));
    if (len >= 13) {
      // Expected layout (cal_sender.py):
      //   offset 0-7: cal_send_RT (double LE, unix time)
      //   offset 8-11: cal_seq (uint32 LE)
      //   offset 12: type_flag (0=bc, 1=uc)
      double cal_send_rt;
      uint32_t cal_seq;
      uint8_t  type_flag;
      memcpy(&cal_send_rt, g_cal_buf + 0, sizeof(double));
      memcpy(&cal_seq, g_cal_buf + 8, sizeof(uint32_t));
      type_flag = g_cal_buf[12];
      write_cal_record(t_recv_local, cal_send_rt, cal_seq, type_flag);
    }
  }

  // Heartbeat 1Hz with text-line diagnostic
  static uint32_t last_hb = 0;
  uint32_t now = millis();
  if (now - last_hb >= 1000) {
    last_hb = now;
    Serial.printf("\n# DIAG cb_total=%u cb_data=%u captured=%u dropped=%u\n",
                  (unsigned)g_cb_total, (unsigned)g_cb_data_only,
                  (unsigned)g_captured_total, (unsigned)g_dropped_total);
    write_hb_record();
  }

  delay(1);
}
