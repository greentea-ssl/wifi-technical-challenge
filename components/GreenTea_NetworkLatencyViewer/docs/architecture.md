# GreenTea NetworkLatencyViewer — アーキテクチャ設計書

> 本書は v2 改訂版 (2026-05-23)。Phase 0 実機検証 (R4/R11/R12 PASS) と RoboCup SSL 2026 Radio Communications Challenge のルール公開を踏まえて、**AP 非依存・両方向 OWD 計測**に再設計した。`[要検証]` は実機確認待ちの記述。

## 1. 目的とスコープ

RoboCup SSL の試合・練習環境において、ホスト PC (AIPC) ↔ ロボットの**無線通信における片道遅延 (One-Way Delay; OWD) を上下方向それぞれ独立に計測・可視化・分析**する。

### 1.1 主目的: RoboCup 2026 Radio Communications Challenge への提出

- 提出締切: 2026-06-26、プレゼン: 2026-07-03
- ルール: <https://robocup-ssl.github.io/technical-challenge-rules/2026-radio-communications-challenge.html>
- 報告項目: **One-Way Latency (mean/variance/max)**、Average Packet Loss、Data Rate、Interference Detection、Startup Time、Power Consumption、Cost
- 「Year 1 は計測手法を規定しない」「片方向通信の無線方式は片方向遅延を報告すべし」と明記されており、**OWD 報告でルール適合**
- **AP は会場提供** (大会 TC が共有 Wi-Fi を提供) もしくはチーム持参。**AP 内部にアクセスできない前提**で設計する
- 「異なるフィールドの異なる Wi-Fi ネットワーク間でのクイック切替」を実証する要件あり → AP に依存した実装は不可

### 1.2 運用環境

- 屋内競技会場・研究室
- 多数の AP・端末が密集、パケットロス・キャリアセンスによる送信抑制が常時起きる
- GPS は使えない (屋内)
- AIPC ↔ AP は **有線 Ethernet 直結**
- 計測機材 (RasPi + sniffer C5) は AP の隣 (数十 cm 以内) に置く

### 1.3 通信パターン (本チームの実態, `robot_comm_spec` v2.0.0)

ロボット側 WiFi 終端は **HID Controller (ESP32-C5)** で、内部で UART (115200 bps) 経由で Compute Unit (STM32H743) と中継する構造。WiFi 区間のチャネルは:

| 論理チャネル | ポート | 送信方式 | 起源 | 計測対象 |
|---|---|---|---|:---:|
| 下り 通常コマンド | `40000 + robot_id` | **unicast** (mDNS で `robot<id>.local` 解決) | AIPC → HID → (UART) → CU | ✅ |
| 下り EMS / OTA | `40999` | broadcast | **AI 系とは別の安全系 PC** → HID | ❌ 計測対象外 |
| 下り CAN ブリッジ | `41000 + robot_id` | unicast | AIPC → HID (CU 経由せず) | △ (将来) |
| 上り 通常テレメトリ | `50000 + robot_id` | broadcast (`192.168.x.255`) | CU → (UART) → HID → Host PC | ✅ |
| 上り CAN テレメトリ | `51000 + robot_id` | broadcast | HID → Host PC (HID 起源) | △ (将来) |

本プロジェクトの計測対象は **AIPC ↔ HID 間の WiFi 区間**のみ。HID↔CU の UART は Radio Comms Challenge のスコープ外。**40999 (EMS) は AI 系と独立した安全系から送られるため、本計測の対象外**。

双方向 echo を前提とした RTT 計測は適用不可。上下それぞれ独立に OWD を測る。

#### 1.3.1 既存の相関キー

- **下り通常コマンド (64 bytes)** の offset 38-45 に `unix time(double LE)` フィールドが既存。Host PC の `clock_gettime(REALTIME)` 値を埋める運用 (dev branch §7.1 で実装実績)
- **上り通常テレメトリ (JSON)** には現状、HID の送信タイムスタンプも seq 番号も無い → そのままでは上り OWD 計測不能

#### 1.3.2 v2.x.0 への追加提案 (本リポ → `robot_comm_spec`)

HID 起源の **WiFi メトリクスチャネル (port `52000 + robot_id`、broadcast JSON)** を新設する。詳細ドラフトは `external/robot_comm_spec/radio_metrics.md` (v2.0.0 で導入、現状は `docs/proposals/radio_metrics_v2x.md` も同内容)。要点のみ:

- HID は 40000/40999 で UDP 受信したら `rx_dl` JSON を 52000+id にブロードキャスト (t_rx_tsf + 受信ペイロードの unix_time コピー)
- HID は 50000/51000 で UDP 送信する直前に `tx_ul` JSON を 52000+id にブロードキャスト (t_tx_tsf)
- 既存ポートには一切手を加えない (互換性維持)
- 採用されれば本リポでは submodule 経由で `robot_comm_spec` を参照

### 1.4 計測対象

- **両方向 OWD** (下り `owd_dl`、上り `owd_ul`) — mean / variance / max を上下別に報告
- 各方向の遅延内訳: 有線 / **AP 内滞留** / 空中 / ESP 内処理
- ジッタ (IPDV, RFC 3393)
- 損失率・再送率
- 無線品質と遅延の相関 (MCS / RSSI / retry)
- 同期不確かさ (sync_uncertainty)

### 1.5 (副次) 研究目的

「ESP32-C5 二台 + 会場 AP におけるビーコン TSF + RasPi タップによる片道遅延計測手法」を**公開可能な知見**として残す。LN6001-JP を持ち込めた場合は AP 直接 TSF 取得 (R1 経路) との独立クロスチェック結果を加える。

## 2. ハードウェア構成

| 役割 | 機材 | 備考 |
|---|---|---|
| ホスト (**AIPC**) | 任意の Linux PC | **本プロジェクトでは black-box** (production AI そのまま動かす)。計測のための変更は一切加えない。Ethernet で GS308E に直結 |
| **タップ** | **NETGEAR GS308E** (SPAN ミラーリング設定) | AIPC↔AP 間に挟み、両ポート双方向のトラフィックを RasPi 観測ポートにミラー |
| **計測 PC (RasPi)** | **Raspberry Pi 4** (4GB 想定)、**NIC 2 本** | **計測の主役**。`eth0` (onboard GbE) を SPAN destination に、`eth1` (USB-Ethernet) を通常 LAN に接続。`gtnlv-rpid` がここで動く |
| RasPi 観測 NIC | onboard GbE (BCM54213PE) | `eth0`。**hwtstamp 対応 (RX 時刻取得用)**。GS308E の SPAN destination ポートにつなぐ |
| RasPi LAN NIC | USB-Ethernet (任意、Realtek RTL8153 系で十分) | `eth1`。GS308E の通常ポートにつなぎ、HID の `52000+id` broadcast を受信。hwtstamp 不要 |
| AP | **任意の 11ax 対応機**。会場では venue 提供、検証ではチーム保有機 | **AP 内部にアクセスしない設計**。SSH 不要、設定変更不要 |
| ロボット側 WiFi 終端 (= **HID Controller**) | **XIAO ESP32-C5** (本番ロボット搭載) | Wi-Fi 6 STA。`robot_comm_spec` 上の名称は **HID**。CU (STM32H743) と UART 115200 bps で接続、本プロジェクトの計測スコープは WiFi 区間のみ。本番運用では USB 不接続、WiFi のみ |
| (開発時の HID 模擬) | ESP32-C5 devkit (CP2102N) | XIAO の native USB CDC は反復 flash 試験で固まる持病あり (Phase 0 §1.2.2)。本リポでの再現開発には devkit を使うのが効率的。同一 SoC なので `metrics_radio` モジュールはそのまま XIAO 本番に移植可能 (WiFi UDP のみ使用、USB CDC 非依存) |
| **TX sniffer** | **ESP32-C5 (追加 1 台、USB-UART ブリッジ系)** | **AP の隣に固定設置**。promiscuous mode で AP 送受信フレームを観測。RasPi に USB 接続。CP2102N 系 devkit 推奨 (sniffer は計測用機材、本番ロボットには搭載されない) |

### 2.1 補助・比較対象機材 (任意)

| 物 | 用途 |
|---|---|
| Linksys Velop WRT Pro 7 (LN6001-JP) | **研究比較用**。OpenWrt 系で `iw dev get tsf` 等で AP TSF を直接取得し、RasPi タップ経由のブリッジ精度を独立検証 (R1) |

**もはや AIPC 側 hwtstamp NIC は不要** (計測が RasPi 完結になったため)。

### 2.2 不採用にした選択肢

不採用にした選択肢の根拠は **`docs/lessons_learned.md` §A** (時刻同期 / sniffer 選定 / 計測アーキテクチャ) に集約。実機検証して採用しなかったもの (AX210 monitor の HE PPDU 不可など) は同 §B 参照。

## 3. 時刻同期チェーン

### 3.1 「NTP は混雑 Wi-Fi 下で信用できない」という前提

NTP/SNTP は片方向オフセットを `offset = ((t2−t1)+(t3−t4))/2` で推定するため**往復路の対称性**を仮定する。会場条件では:

| 起こること | NTP/SNTP への影響 |
|---|---|
| キャリアセンスによる送信抑制 | 上りリクエスト送出が数〜数十ms 遅延 |
| パケットロス→再送 | DCF バックオフが片方向だけに乗り、対称仮定が崩れる |
| 干渉での選好レート低下 | 上下でエアタイム非対称 |
| 多 AP での輻輳 | RTT 分布そのものが崩壊 |

実測の感覚値: 静かな LAN < 100μs、オフィス Wi-Fi 1〜10ms、**会場相当の混雑下では 5〜20ms、外れ値 50ms+**。

**結論**: AP↔ESP32 の**無線区間で NTP を主軸に据えると ms 保証は破綻する**。無線区間はビーコン由来 TSF を使う。本構成では計測を RasPi に集約することで、AIPC 側のクロック精度は OWD 計算に影響しない。

### 3.2 採用する同期チェーン (RasPi 中心 / AP 非依存版)

```
[AIPC] ────────────→ [GS308E] ───────────→ [AP] ──── (HE air) ────→ [HID C5]
 black box, no PTP,    │ port1/port2                                  TSF
 計測ロジック無し       │ SPAN to port3
                       ↓
                    [RasPi 4]                       ┌── 52000+id bcast ──┘
                  ┌────┴───┐                        │  (rx_dl / tx_ul JSON)
                eth0     eth1 ─── port4 normal ─────┘
              (SPAN dst, (USB-Eth)
               hwtstamp)
                  │
                  USB ─→ [sniffer C5] (AP 隣設置)
                          TSF + esp_timer
```

三つのクロック領域を以下のブリッジで連結する (AIPC は登場しない):

| ブリッジ | 手段 | 期待精度 |
|---|---|---|
| RasPi clock ↔ AP TSF 軸 | RasPi が観測する (wire_rx, sniffer_rx) ペアを**下位 5-10% min-filter** で抽出 → 線形フィット | 20〜50μs (AP の最小処理遅延がフロア) |
| AP TSF ↔ sniffer C5 内部 | ビーコン TSF (アソシエート STA として自動追従) + **(esp_timer, TSF) 中点線形回帰** | **50μs p99** (Phase 0 R4 で実測) |
| sniffer C5 ↔ HID C5 | 同じ AP TSF を共有 | **約 4μs p99** (Phase 0 R11 で実測、ドリフト除去後) |

**チェイン全体の累積誤差予算: 約 60〜80μs RSS** → 目標 1ms に対し十分なマージン。

**AIPC は計測に関与しない**ため、AIPC 側 PTP も AIPC NIC の hwtstamp 対応も**いずれも不要**になった。AIPC が payload offset 38-45 に埋める unix_time は OWD 計算の基準としては使わず、**フレーム相関キー**としてのみ利用する (RasPi 観測の `t_rpi_wire_rx_dl` と HID の `rx_dl.corr_unix_time` を結ぶための一意キー)。

### 3.3 関わるクロック一覧

| ノード | クロック | 用途 | 同期手段 |
|---|---|---|---|
| AIPC | (任意) | OWD 計算には使わない (payload の unix_time は相関キーのみ) | 不問 |
| RasPi | `CLOCK_REALTIME` (system) | **OWD 計算の時刻基準。両方向の "Host 側" 起点・終点** | 通常 NTP (人間可読化のみ) |
| RasPi `eth0` PHC | hwtstamp RX timestamp | tap 受信フレームの精密タイムスタンプ | onboard NIC 内部 |
| sniffer C5 | TSF | **空中フレームの受信時刻基準** | AP ビーコンで追従 |
| sniffer C5 | `esp_timer_get_time()` | ISR 安全な打刻カウンタ | TSF と線形回帰較正 |
| HID C5 | TSF | 同上 | 同上 |
| HID C5 | `esp_timer_get_time()` | 同上 | 同上 |

### 3.4 各較正の中身

#### 3.4.1 (削除) AIPC↔RasPi 同期は不要

旧 v2 では PTP による AIPC↔RasPi 同期が必要だったが、**計測を RasPi に集約**したことでこの段が消えた。AIPC のクロック精度は OWD 計算に影響しない。

#### 3.4.2 RasPi ↔ AP TSF 軸 (タップによる較正パケット観察)

GS308E の SPAN ポートから RasPi `eth0` (or 別 NIC) でミラーリング受信。`gtnlv-rpid` が:

1. **production の下り unicast** (port 40000+id) を RasPi `eth0` (SPAN destination) で受信し `t_rpi_wire_rx_rt` を記録 (kernel `SO_TIMESTAMPING` HW)。専用較正パケットは不要 — production トラフィックそのものを較正データとして使う
2. USB-UART 経由で sniffer C5 から `(hdr_seq, t_sniffer_rx_tsf, payload bytes 38-45 = unix_time)` を受信
3. 同一 payload `unix_time` でペアにして `delay_aipc_to_air = t_sniffer_rx_tsf − t_rpi_wire_rx_rt` を計算
4. 直近 N 秒の **下位 5〜10%** だけ採用 (AP がアイドル直後のフレーム) して線形フィット → `bridge_rt_to_tsf(t) = a * t + b` を保持

```c
// 簡略疑似コード
struct pair_t { uint64_t t_rt; uint64_t t_tsf; };
ring_buffer<pair_t> recent_pairs(N=4096);  // 過去 ~5 min

// per packet
double delay = t_sniffer_rx_tsf - bridge_rt_to_tsf(t_rpi_wire_rx_rt);
if (delay < percentile_5(recent_delays)) {
    recent_pairs.push({t_rpi_wire_rx_rt, t_sniffer_rx_tsf});
    refit_linear(recent_pairs);
}
```

**前提**: AP の最小処理遅延 (アイドル時) はおよそ定数で、これがブリッジのオフセット成分。残差成分 (per-packet AP queue / MAC backoff) は別に保持して `delay_ap_dl` として報告する。

期待精度: フィット残差 20〜50μs。

#### 3.4.3 AP ↔ sniffer C5 / HID C5 (ビーコン TSF)

sniffer C5 / HID C5 共に AP に associate しておく。STA としてビーコンを受けると HW が自動で AP TSF に追従する。

監視メトリクスとして以下を per-frame で記録:

- 直近ビーコン受信からの経過時刻 (`last_beacon_age_us`)
- 直近 N=64 ビーコンの取り逃し数 (`missed_beacons`)
- 受信ビーコンの TSF と前回の差分

これらを**同期品質スコア**として各パケットに付与する (§5)。

#### 3.4.4 ESP32 内 TSF ↔ esp_timer 較正 (Phase 0 R4 で実証)

**前提制約**:
- `esp_wifi_get_tsf_time()` は呼び出しに 0〜120μs (典型 48μs、Phase 0 実測 290μs 中央値) かかる → **受信 ISR 内では呼べない**
- ESP32-C5 では `wifi_pkt_rx_ctrl_t` は `esp_wifi_rxctrl_t` のエイリアス。`sig_mode` 廃止、**`cur_bb_format`** (HE_SU=4) で PHY 判定
- `esp_timer_get_time()` は 64bit μs、高速かつ ISR 安全
- HE PPDU の `rx_ctrl.timestamp` で **重複値 (esp-idf#2468)** が 0〜2.6% 発生する → ホスト側で必ず dedup

**実装** (Phase 0 R4 検証済み手順):

```c
// 受信コールバック (ISR 安全)
int64_t t_rx_local = esp_timer_get_time();
log_rx(seq, t_rx_local, rx->timestamp, rx->cur_bb_format, ...);

// 較正タスク (vTaskDelay(100ms))
int64_t t_a   = esp_timer_get_time();      // before
int64_t t_tsf = esp_wifi_get_tsf_time(WIFI_IF_STA);
int64_t t_b   = esp_timer_get_time();      // after
int64_t t_mid = (t_a + t_b) / 2;            // ★ 中点を使うこと
push_calibration_pair(t_mid, t_tsf);
// 直近 N 点で線形回帰 → esp_timer→TSF 変換式を更新
```

**Phase 0 実測**: 中点フィット時に **|resid| p99 ≤ 50μs** (XIAO 50us / devkit 53us、static 6 分間、`docs/phase0_runbook.md` §1.1.1 詳細)。`t_a` 単独だと p99 が 3.5 倍に悪化する罠あり。

## 4. 計測実装

### 4.1 端点での打刻

AIPC は計測コードを一切持たない (black box)。すべての打刻は RasPi 側 / 各 C5 側で取得される。

| 端点 | 動作 | 時計 | 出力 |
|---|---|---|---|
| RasPi tap RX 下り | AIPC 発フレームを SPAN port 経由で eth0 が受信、`SO_TIMESTAMPING` (HW) | `CLOCK_REALTIME` (RasPi system, PHC 同期) | `t_rpi_wire_rx_dl` |
| RasPi tap RX 上り | HID 発フレームを SPAN port 経由で eth0 が受信 (AIPC 行きのもの) | 同上 | `t_rpi_wire_rx_ul` |
| RasPi eth1 受信 (HID 52000+id) | 通常 LAN ポートで HID の `rx_dl` / `tx_ul` JSON broadcast を受信 | 同上 | `t_rpi_metric_rx_rt`, `t_hid_rx_tsf`, `t_hid_tx_tsf` |
| sniffer C5 (両方向) | promiscuous で AP↔HID 間フレーム捕捉 → USB-UART で RasPi に送出 | TSF (esp_timer から変換) | `t_sniffer_rx_tsf` |
| HID C5 受信 (下り 40000+id) | UDP 受信時に `esp_timer_get_time()` → TSF 変換 → 52000+id rx_dl で broadcast | TSF (esp_timer から変換) | `t_hid_rx_tsf` |
| HID C5 送信 (上り 50000+id / 51000+id) | 送信直前に `esp_wifi_get_tsf_time()` → 52000+id tx_ul で broadcast | TSF | `t_hid_tx_tsf` |

**最終的な投影軸**: すべての時刻を **RasPi `CLOCK_REALTIME`** に投影。`t_*_tsf` は §3.4.2 のブリッジで RasPi 軸に変換する。**AIPC は登場しない**。

### 4.2 sniffer C5 の配置と動作

- AP との物理距離 < 数十 cm (伝搬非対称を ns 以下に保つ)
- AP に associate して TSF を継続同期 (STA として接続維持)
- promiscuous mode 有効、filter mask = `WIFI_PROMIS_FILTER_MASK_DATA`
- ホストへの送出は **USB-UART (CP2102N) 経由のシリアル** を推奨。XIAO の native USB CDC は MSPI エラー後に TX 不能化する症状あり (Phase 0 §1.2.2 参照)
- USB CDC を採用せざるを得ない場合は **UDP 経由のフォールバック** (Phase 0 で実装済み、`tools/esp_firmware/r12_udp_test/` 参照)

### 4.2.0 ESP32-C5 promiscuous の制約

ESP32-C5 の promiscuous モードは **チップ MAC フィルタが promiscuous でも有効** で、他 STA 宛て unicast を cb に届けない:

- STA + associated + promiscuous: 自身宛て・broadcast・multicast・management のみ
- STA + unassociated + promiscuous (pure promisc, channel 固定): 同じ
- 他 STA 宛て unicast の捕捉率 ~0.8 % (実測、`docs/lessons_learned.md` §C.2)

**含意**:
- 本番 unicast フレームの **air-side timing** は C5 sniffer では取れない
- per-packet OWD は **HID rx_dl (TSF) + RasPi wire-side (PHC)** で成立するため実用上問題なし
- bridge 較正 (RT↔TSF) は **broadcast キャプチャ** で代替

**`delay_ap_dl` 変動成分の取り方**:
- HID の RX 処理時刻 (PHY → `rx_ctrl.timestamp` 経路) を定数 `c_rx_proc` と仮定
- `delay_total = t_hid_rx_tsf − t_rpi_wire_rx_dl` (per-packet 計測可能)
- `delay_ap_dl ≒ delay_total − c_rx_proc` (差が AP 滞留の per-packet 変動)
- `c_rx_proc` は静的環境 broadcast 較正で決定 (~μs オーダー)

per-packet absolute breakdown は失うが、AP 滞留 **変動** は取れる → チャレンジ要件「AP 内滞留評価」に十分対応。

別ルートとして **AX210 monitor mode** を補助的に併用すれば、HE PPDU 以外 (11a/legacy、broadcast/MGMT/制御) の他 STA 宛て air-side timing は取れる (`docs/lessons_learned.md` §B.1)。

### 4.2.0' sniffer の promiscuous と UDP RX path

ESP32-C5 で sniffer を **PROMIS ON 中に UDP listener を併設すると RX path が starve する** (`docs/lessons_learned.md` §C.1)。

**運用ルール**:
- sniffer は **promiscuous 専任** (UDP listen 併設しない)
- cal 試験は sniffer を一時 `ENABLE_PROMISCUOUS=false` に切替、終了後 true に戻す
- HID は promiscuous 非使用なので影響なし、production 運用に問題なし

### 4.2.1 sniffer のキャパシティ設計 (フレーム取りこぼし対策)

dev branch §3.1 で「シリアル出力 ~1000 pps 上限、フィルタ通過率 ~50%」が実機観測された。会場想定の **12 ロボット × 60Hz × 双方向 + 周辺トラフィック = 2000〜5000 pps** に対応するには、層別に対策を重ねる。

#### ドロップが起きる層

| # | 層 | 容量目処 | 観測手段 |
|---|---|---|---|
| 1 | Wi-Fi ドライバ RX queue (`CONFIG_ESP_WIFI_DYNAMIC_RX_BUFFER_NUM`) | default 32 buffer | ドライバ内部統計 `esp_wifi_statis_dump` (任意) |
| 2 | promiscuous callback の処理時間 | cb 数 μs 以下なら問題なし | バースト時の取り逃しを `hdr_seq` 飛びで間接観測 |
| 3 | アプリ ring buffer | 設計次第 | `g_dropped_total` カウンタを出力に必ず含める |
| 4 | UART/CDC 出力帯域 | 921600 bps + ASCII で ~700 pps | host runner で受信 pps を観測 |
| 5 | UDP 出力 (WiFi) | ~10 Mbps だが自己干渉あり | host runner で UDP 受信 pps 観測 |

層 4 が dev branch の主ボトルネックだった (50% drop)。これを潰した上で層 2/3 を強化する。

#### 多層対策 (本番 sniffer に組込)

- **バイナリプロトコル化**: ASCII CSV (~120B/frame) → 固定長バイナリ (~36B/frame)。3.5 倍効率。フレームフォーマットは `tools/esp_firmware/sniffer/proto.h` で定義予定
- **UART 2 Mbps**: CP2102N の実用上限。921600 → 2 Mbps で 2.17 倍。組合せて理論 ~7000 pps sustained
- **cb 段階フィルタ**: `WIFI_PKT_DATA` → `sig_len` → `cur_bb_format` → BSSID マッチ、の早期 reject で cb 処理時間を短縮
- **ring buffer 4096 entries × 36B = 144KB** (C5 SRAM 327KB に余裕で収まる): バースト対策ヘッドルーム
- **drop counter を必ず出力に含める**: 取りこぼし率を per-capture で報告可能に
- **二台 sniffer 並列**: `hdr_seq % 2` 等で分担。Phase 0 で R11 用に既に二台動作させた構成を流用可能。冗長性も得られる
- **(オプション) UART + UDP 二系統出力**: UART を主、UDP を副 / 障害冗長として並列稼働

#### 想定キャパシティと運用方針

| 構成 | 理論 sustained pps | 会場想定 (2-5k pps) 対応 |
|---|---|---|
| 921600 baud + ASCII (Phase 0 現状) | ~700 | NG |
| 2 Mbps + バイナリ (Phase 1 推奨) | ~7000 | OK (margin 30%) |
| 上記 + 二台並列 | ~14000 | OK (margin 150%) |

#### 取りこぼしの統計的扱い

ドロップを完全に 0 にする努力と同時に、**計測結果には常に "捕捉率" を併記**する:

```
Mean OWD_dl = 1.23 ms ± 0.05 ms
  (N=12,580 captured / ~13,360 expected, capture_rate=94.2%)
```

捕捉率は host runner が `g_dropped_total` カウンタと `hdr_seq` の飛びを合成して算出。チャレンジの「Packet Loss」報告とも整合する。捕捉率が 80% を切るランは `sync_uncertainty` の評価対象から除外し、別途報告する。

### 4.3 下り OWD (AIPC unicast → HID)

```
AIPC ──UDP unicast 40000+id (payload offset 38-45: unix_time)── [GS308E] ── AP ── air(HE unicast) ──→ HID
                                                                     │                                  │
                                                                     SPAN port3 → RasPi eth0            │
                                                                                  t_rpi_wire_rx_dl      │
                                                                                                        │
                                                          sniffer C5 (AP 隣) ←─── air ──────────────────┤
                                                          t_sniffer_rx_tsf                              │
                                                                                                        │
                                       RasPi eth1 ←──── 52000+id bcast (rx_dl, t_hid_rx_tsf) ───────────┘
                                          (LAN port)
```

相関キー: AIPC が payload offset 38-45 に埋める `unix_time` を **HID が rx_dl.corr_unix_time にエコー**する。RasPi はこの値で「tap 観測した下りフレーム」と「HID の rx_dl 報告」をペアリング。**AIPC クロックの絶対値は不問** (一意キーとして使うだけ)。

OWD の定義: "Host PC → Robot" = RasPi が tap で観測した送出時刻 → HID が受信した瞬間。AIPC NIC から GS308E SPAN までの cable + switch propagation (数 μs) は無視。

各区間の per-packet 分解:

| 区間 | 計算式 | 解釈 |
|---|---|---|
| **`delay_ap_dl`** | **`t_sniffer_rx_tsf (→RT) − t_rpi_wire_rx_dl`** | **AP 内滞留 (キュー + 処理 + MAC バックオフ)** |
| `delay_air_dl` | `t_hid_rx_tsf − t_sniffer_rx_tsf` | 空中伝搬 + HID/sniffer 処理遅延差 (≈ 4μs 定数, R11) |
| **`owd_dl`** | `t_hid_rx_tsf (→RT) − t_rpi_wire_rx_dl` | **下り片方向遅延 (チャレンジ報告値)** |

旧版にあった `delay_wired_dl` (AIPC→AP の有線遅延) は計測対象外となった (AIPC を black-box 扱いするため、AIPC NIC 内部の処理時刻を見ない)。tap 観測時刻が事実上の起点。

### 4.4 上り OWD (HID broadcast → AIPC)

上り通常テレメトリ (`50000+id`) は CU 起源で HID は透過中継。HID 自身の送信時刻は `52000+id` の `tx_ul` メトリクスから取る:

```
       CU ──UART──→ HID ──UDP bcast 50000+id (production telemetry)── air ──→ AP ── [GS308E] ──→ AIPC (受信、計測には関与しない)
                          │                                                  │       │ port4
                          │                                                  │       └── SPAN port3 → RasPi eth0
                          │                                                  │                       t_rpi_wire_rx_ul
                          └──UDP bcast 52000+id (tx_ul, t_hid_tx_tsf)── air ──┤
                                                                              ├──→ RasPi eth1 (LAN, t_rpi_metric_rx)
                                                                              ↓
                                                                       sniffer C5 (両方とも捕捉、TSF 付き)
                                                                       t_sniffer_rx_tsf
```

ペアリング: 同 HID (= 同 src_ip) の production uplink と `tx_ul` を **時間近接** (`|t_rpi_wire_rx_ul − t_rpi_metric_rx| < 5ms` 窓) で対応付け。詳細は `external/robot_comm_spec/radio_metrics.md` (v2.0.0 で導入、現状は `docs/proposals/radio_metrics_v2x.md` も同内容) §4.2。

OWD の定義: "Robot → Host PC" = HID が WiFi に出した瞬間 (TSF) → RasPi が tap で観測した到着時刻。AIPC が UDP socket で受け取った時刻は使わない。

| 区間 | 計算式 | 解釈 |
|---|---|---|
| `delay_air_ul` | `t_sniffer_rx_tsf − t_hid_tx_tsf` | HID 送信 → AP 入力 (≒ sniffer 受信) |
| **`delay_ap_ul`** | **`t_rpi_wire_rx_ul − t_sniffer_rx_tsf (→RT)`** | **AP 内滞留 (上り、bridging+queueing)** |
| **`owd_ul`** | `t_rpi_wire_rx_ul − t_hid_tx_tsf (→RT)` | **上り片方向遅延 (チャレンジ報告値)** |

旧版にあった `delay_wired_ul` (AP→AIPC の有線遅延) は計測対象外。AIPC を観測しないため、tap までの到着が終点。

### 4.5 ホスト側パケット相関 (dedup)

esp-idf#2468 由来の重複 `rx_timestamp` および sniffer / ロボット双方からの観測の重複を防ぐため:

- **一意キー**: `(src_mac, hdr_seq)` (802.11 ヘッダの 12-bit seq、open AP なので暗号化されない)
- payload 内の application-level seq (AIPC 採番) も別キーとして併用
- Phase 0 §1.2.2 参照 (dedup の必要性は実機検証で確認済み)

## 5. データスキーマ

1 計測サンプル (1 パケット) あたり Parquet で保存。両方向別の行として記録するか、1 行で両方向相関を持つかは解析側の都合で選択 (両方向の同 seq が必ずペアになるとは限らないため別行が現実的)。

### 5.1 共通カラム (両方向の各行)

| カラム | 型 | 取得元 | 備考 |
|---|---|---|---|
| `seq` | uint64 | RasPi (rpid 採番) | 計測ラン内単調増加 |
| `run_id` | string | RasPi | 実験回識別 |
| `direction` | enum {`dl`, `ul`} | RasPi | 下り or 上り |
| `frame_size` | uint16 | RasPi (tap) | ペイロード長 |
| `src_mac` / `dst_mac` | string | sniffer or RasPi | 802.11 / Ethernet 各層 |
| `hdr_seq` | uint16 | sniffer | 802.11 ヘッダ seq |
| `corr_unix_time` | float64 | RasPi (tap → HID 52000+id) | 下りフレーム相関キー (payload offset 38-45) |
| `rssi`, `mcs`, `bb_format`, `bw`, `gi` | 各種 | sniffer / HID | 無線品質 |

### 5.2 下り (direction=dl) の時刻列

| カラム | 型 | 取得元 |
|---|---|---|
| **`t_rpi_wire_rx_dl`** | int64 (ns) | **RasPi eth0 (SPAN)、OWD 計算の起点** |
| `t_sniffer_rx_tsf` | int64 (μs) | sniffer C5 |
| `t_sniffer_rx_rt` | int64 (ns) | 解析時に §3.4.2 ブリッジで投影 |
| `t_hid_rx_tsf` | int64 (μs) | HID C5 (52000+id rx_dl 経由で受領) |
| `t_hid_rx_rt` | int64 (ns) | 同上、ブリッジで RasPi 軸へ |
| `t_hid_app_done` | int64 (μs) | HID C5 (アプリ層、任意) |

### 5.3 上り (direction=ul) の時刻列

| カラム | 型 | 取得元 |
|---|---|---|
| `t_hid_tx_tsf` | int64 (μs) | HID C5 (52000+id tx_ul 経由で受領) |
| `t_hid_tx_rt` | int64 (ns) | 解析時にブリッジで RasPi 軸へ |
| `t_sniffer_rx_tsf` | int64 (μs) | sniffer C5 |
| **`t_rpi_wire_rx_ul`** | int64 (ns) | **RasPi eth0 (SPAN)、OWD 計算の終点** |

### 5.4 同期品質列 (両方向共通)

| カラム | 型 | 用途 |
|---|---|---|
| `rt_to_tsf_residual_us` | float | §3.4.2 ブリッジの残差 |
| `esp_tsf_cal_residual_us` | float | §3.4.4 各 C5 の線形回帰残差 |
| `last_beacon_age_us` | int32 | C5 の直近ビーコン受信からの経過 |
| `missed_beacons` | uint16 | 直近ウィンドウのビーコン取り逃し |

### 5.5 派生指標

| 指標 | 計算 | 用途 |
|---|---|---|
| **`owd_dl`** | `t_hid_rx_rt − t_rpi_wire_rx_dl` | **下り OWD (チャレンジ報告)** |
| **`owd_ul`** | `t_rpi_wire_rx_ul − t_hid_tx_rt` | **上り OWD (チャレンジ報告)** |
| `delay_ap_dl/ul`, `delay_air_dl/ul` | §4.3/§4.4 | 内訳分解 |
| `owd_air_pure_dl` | `t_hid_rx_tsf − t_sniffer_rx_tsf − rx_proc_diff_const` | 純粋空中伝搬 (sniffer で AP TX 消去) |
| **`sync_uncertainty`** | `rt_to_tsf_residual + esp_tsf_cal_residual + f(beacon_age)` | 計測値の不確かさ |

### 5.6 同期品質フラグ

- `sync_ok`: `sync_uncertainty < 500μs`
- `beacon_recent`: `last_beacon_age_us < 200000` (直近 2 ビーコン以内)
- `no_roaming`: 計測中に BSSID が変わっていない

## 6. ソフトウェア構成

```
GreenTea_NetworkLatencyViewer/
├── tools/
│   ├── rpi_daemon/        # RasPi 上の gtnlv-rpid (新規, Python or Rust)
│   │                      #   - eth0 (SPAN dst) で AIPC↔AP のフレーム受信 (PF_PACKET + SO_TIMESTAMPING HW)
│   │                      #   - eth1 (LAN) で HID の 52000+id JSON broadcast 受信
│   │                      #   - sniffer C5 USB-UART 受信
│   │                      #   - RT ↔ TSF 線形ブリッジ計算
│   │                      #   - OWD 計算と CSV/Parquet 保存
│   ├── esp_firmware/      # ESP-IDF / Arduino プロジェクト
│   │   ├── r4_calib_test/        # esp_timer↔TSF 較正単体 (Phase 0)
│   │   ├── r12_promisc_test/     # promiscuous + ring buffer (Phase 0)
│   │   ├── r12_udp_test/         # 同 + UDP 出力 (HWCDC 死亡時のフォールバック)
│   │   ├── sniffer/              # 本番 sniffer ファーム [Phase 1]
│   │   └── hid_metrics/          # HID ファームに組込む 52000+id 発信モジュール [Phase 1]
│   ├── r4_runner/         # ホスト側 R4 ランナー (Phase 0 完成)
│   ├── r12_runner/        # 同 R12 ランナー (Phase 0 完成)
│   ├── r12_udp_runner/    # UDP 版ランナー (Phase 0 完成)
│   └── r11_analyzer/      # 2 台 C5 比較解析 (Phase 0 完成)
├── analyzer/              # オフライン解析 (Python: pandas/polars) [Phase 2]
├── web/
│   ├── backend/           # FastAPI [Phase 3]
│   └── frontend/          # Streamlit (Phase 2) → React+Plotly (Phase 3)
├── docs/
│   ├── architecture.md         # 本書 (v2)
│   ├── phase0_runbook.md       # Phase 0 実機検証手順・結果
│   ├── sync_alternatives.md    # Wi-Fi 非依存同期の比較検討
│   ├── lessons_learned.md      # dev branch 実機知見
│   ├── proposals/
│   │   └── radio_metrics_v2x.md  # robot_comm_spec への 52000+id チャネル追加提案
│   ├── hardware_options.md     # [TODO] hwtstamp 対応機材リスト
│   ├── requirements.md         # [TODO]
│   └── calibration.md          # [TODO] PTP / TSF 較正の運用 SOP
├── external/
│   └── robot_comm_spec/   # git submodule、v2.0.0 タグが出たら追加
├── phase0_results/        # Phase 0 計測 CSV (gitignored)
└── README.md
```

## 7. 可視化要件 (Web ダッシュボード)

最低限のビュー:

1. **両方向 OWD 時系列** (`owd_dl` / `owd_ul` 別軸、mean/p50/p95/p99)
2. **両方向 OWD CDF**
3. ジッタ (IPDV) ヒストグラム (各方向)
4. **遅延内訳積み上げ** (`delay_ap` / `delay_air`、各方向)
5. 無線品質との相関 (MCS / RSSI / retry vs OWD 散布図)
6. 同期品質パネル (PTP offset、TSF 較正残差、ビーコン取りこぼし率)
7. ラン間比較 (試合 A vs B、AP A vs B)

実装順:

- Phase 2: Streamlit で履歴ファイル読み込み描画
- Phase 4: FastAPI + WebSocket + React/Plotly でほぼリアルタイム

## 8. 未解決リスクと検証項目

| # | 項目 | 影響 | 状態 / 検証方法 |
|---|---|---|---|
| **R4** | ESP32-C5 の `esp_timer↔TSF` 較正が線形回帰で μs に収まるか | High | **PASS** (Phase 0): 中点フィットで p99 ≤ 50μs |
| **R11** | sniffer C5 とHID C5 の `rx_ctrl.timestamp` 処理遅延差の対称性 | High | **PASS** (Phase 0): ドリフト除去後 RMS 2μs |
| **R12** | sniffer C5 が HE PPDU を promiscuous 取得可能か | Mid | **PASS** (Phase 0): HE_SU 90%、ただし重複 0〜2.6% 発生 → host dedup 必須 |
| ~~R-PTP~~ | ~~AIPC ↔ RasPi PTP のオフセット精度~~ | **削除** | 計測を RasPi に集約し AIPC を black-box 化したため不要 |
| **R-BRG** | RasPi ↔ AP TSF ブリッジの安定性 | High | min-filter 線形フィットの残差を実測。Phase 1 マイルストーン |
| **R-MIRROR** | GS308E SPAN destination port が hwtstamp HW RX を出すか | Mid | Phase 1 着手時に `ethtool -T eth0` + 実フレーム計測で確認 |
| **R-AP** | AP の最小処理遅延がどの程度安定か (会場 AP・チーム AP それぞれ) | High | Phase 2 で複数 AP 機種で計測 |
| **R9** | 混雑下のビーコン取り逃しと TSF ドリフト | High | 混雑シミュレーション (iperf3 UDP フラッド) で測定 |
| **R10** | ESP32 STA のローミング・**クイック AP 切替時の TSF 飛び** | **High (チャレンジ要件)** | チャレンジが SSID 切替を実証要求。Phase 2 で集中検証 |
| R-CDC | XIAO C5 の native USB CDC が一定条件で TX 不能化 | Mid | Phase 0 §1.2.2 で実例。sniffer 役は CP2102N 系 devkit を使う運用で回避 |
| R8 | ESP32 水晶ドリフト (10-20ppm) が較正窓 100ms 内で問題化するか | Mid | Phase 0 R4 副産物で評価: 6.4s 窓で ±6〜7ppm の揺れあり、許容範囲内 |
| **R1** (元 High) | LN6001-JP の QSDK で TSF が安定取得できるか | **Low** (デモート) | RasPi タップで AP 非依存になったため必須でなくなった。研究比較目的で残置 |
| **R3** (元 Mid) | LN6001-JP の Ethernet hwtstamp 対応 | **Low** (デモート) | 同上 |

## 9. 現状と残課題

### 9.1 達成済 (チャレンジ提出主軸数字)

LN6001-JP + Xikestor SKS3200M + DFS ch112 環境で 6h overnight 走破済 (詳細 `docs/phase3_findings.md`):

- 下り OWD **wifi_leg median 2.31 ms** (RasPi 内部基準、clock 非依存)、p99 3.54 ms、worst-case 2.52 sec
- DL 損失率 **0.134 %**、UL pair rate **99.96 %**
- AP queue freeze 65 events / 6h (event 間隔 median 245 s、DFS radar check 周期と整合)
- AP uc-bc 差 median(uc) − median(bc) = **+7.85 ms**
- DL broadcast の DTIM 遅延: median 19.6 ms / max 108 ms

### 9.2 残課題

- **提出文書ドラフト** (チャレンジ 7 項目を `docs/phase3_findings.md` 数字で起稿) — **2026-06-26 提出締切、2026-07-03 プレゼン**
- 起動時間計測 (要件 #5、reflector cold boot → first rx_dl の自動計測)
- 干渉検出データ集計 (要件 #4、sniffer.csv から retry-bit / RSSI / 近隣 AP beacon)
- クイック AP 切替デモ (R10、2 SSID 切替時の TSF 飛びと再 association 時間)
- 混雑下計測 (R9、iperf3 並走で event 数の悪化を観測)
- sniffer TSF↔unix bridge 評価 (次回 overnight で `t_rpid_recv_unix` 活用、~ms 精度の絶対 TSF 変換)

### 9.3 大会後の研究計画

- UWB DW3000 を ground truth として導入する研究計画 (`docs/sync_alternatives.md` §5)
- TCXO 換装 (SiT5356) によるロボット長時間ドリフト改善
- PTP / chrony 直接同期で host PC ↔ RasPi clock offset ~2 ms → < 100 μs に

## 10. 用語集

- **TSF (Timing Synchronization Function)**: IEEE 802.11 で規定される AP-STA 間 μs 単位時刻共有メカニズム。ビーコンで配布される 64bit μs カウンタ
- **TBTT (Target Beacon Transmission Time)**: ビーコンの予定送出時刻。通常 100ms 周期
- **DCF (Distributed Coordination Function)**: 802.11 の基本的なメディアアクセス制御。CSMA/CA + ランダムバックオフ
- **PTP (IEEE 1588)**: 有線ネットワーク用の高精度時刻同期プロトコル。Linux では `linuxptp` (`ptp4l`, `phc2sys`)
- **PHC (PTP Hardware Clock)**: NIC 内の PTP 用ハードウェアクロック。`/dev/ptp0` 等
- **HW Timestamping**: NIC ハードウェアレベルでパケット送受信時刻を取得する仕組み (`SO_TIMESTAMPING` + `SOF_TIMESTAMPING_*_HARDWARE`)
- **SPAN (Switched Port Analyzer)**: 管理スイッチが特定ポート間のトラフィックを別ポートにミラーリングする機能。「ポートミラー」「Port Mirroring」とも
- **IPDV (Inter-Packet Delay Variation)**: ジッタの計量指標 (RFC 3393)
- **OWD (One-Way Delay)**: 片方向遅延。送信側のクロックで打刻された送信時刻と受信側のクロックで打刻された受信時刻の差。両クロックの同期前提
- **bb_format**: ESP32-C5 の `wifi_pkt_rx_ctrl_t.cur_bb_format`。受信フレームの PHY 形式 (HE_SU=4, 11G/A=1 等)
