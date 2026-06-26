# ESP32-C5 + WiFi 6 環境における片方向遅延 (OWD) 計測手法

**RoboCup SSL 2026 Radio Communications Challenge — 研究知見の公開**
Team GreenTea / SanRei

> 本書は、競技提出のために構築した「Host PC ↔ ロボット間 WiFi 区間の上下別片方向遅延 (One-Way Delay) を per-packet で計測する手法」を、外部の方が再現・参考にできる形でまとめた公開ドキュメントです。提出 7 項目の数値そのものではなく、**どう測ったか**に焦点を当てます。確定数値の詳細は `docs/phase3_findings.md`、設計詳細は `docs/measurement_architecture.md` を参照。

---

## 1. 背景と課題

SSL では Host PC 上の AI が各ロボットへ制御コマンドを WiFi で送り、ロボットはテレメトリを返す。制御周期 (~10 ms) に対して**無線区間の遅延とその裾 (tail)・損失**が性能を律速する。評価したいのは往復 (RTT) ではなく、**上り・下りそれぞれの片方向遅延 (OWD) を 1 パケット単位**で、である。

これには 3 つの本質的な困難がある。

1. **送受信端に共通の時計が無い**。Host PC の unix 時刻とロボット (ESP32) のクロックは独立しており、単純な「受信時刻 − 送信時刻」は両端の時計オフセットを含んでしまう。
2. **会場の AP に触れない**。競技会場では主催者提供の AP を使うため、AP 内部にプローブを仕込めない。任意の AP に追随できる手法が要る。
3. **production の通信を汚さない**。AI やロボットの本番ファームに計測コードを混ぜると、計測自体が遅延要因になり、また責任分界 (制御基板は WiFi を知らない) を壊す。

---

## 2. 設計方針: 計測を 1 台に集約し、両端は黒箱のまま

- **計測 Host (Raspberry Pi 5) に集約**。Host PC (AIPC) は production の AI をそのまま動かし、**計測コードを一切持たない (black-box)**。AP 内部にも触れない。これにより会場の任意 AP 構成に追随できる。
- AIPC のトラフィックは **スイッチの SPAN (ポートミラー)** で計測 RasPi に複製し、有線上で観測する。
- 空中のフレームは **ESP32-C5 を promiscuous (sniffer) として AP 隣に設置**して観測する。
- ロボット端末 (HID = XIAO ESP32-C5) は、自身の WiFi 送受信イベントを**独立した broadcast チャネル**で「自己申告」する (後述の radio_metrics)。

結果として、1 つのフレームを **最大 3 つの観測点 — 有線 (wire / SPAN)、空中 (air / sniffer)、端末 (HID) —** で捉え、区間ごとの遅延に分解できる。

```
[AIPC] ─有線→ [SPAN] ─→ [AP] ─空中→ [HID]
          wire観測       sniffer観測(air)   HID自己申告(TSF)
```

---

## 3. 時刻ドメインと、その橋渡し (bridge)

観測点ごとに時計が違う。これを 1 本の時間軸に揃えるのが本手法の核心である。

| クロック | 持ち主 | 性質 |
|---|---|---|
| **unix 時刻 (CLOCK_REALTIME)** | 計測 RasPi / Host PC | NTP 同期。wire 観測 (`SO_TIMESTAMPING`) と HID 申告の受信時刻はこれ |
| **AP TSF** | 802.11 (AP が基準) | 全 STA が beacon で同期する μs カウンタ。air / HID の「無線上の時刻」 |
| **esp_timer** | 各 ESP32 (sniffer / HID) | 各チップのローカル μs クロック (水晶) |

### 3.1 チップ内: esp_timer ↔ AP TSF の線形換算

ESP32 の `esp_wifi_get_tsf_time()` は呼び出しに数十〜数百 μs かかり ISR では使えない。そこで **hot-path では `esp_timer_get_time()` のみ記録**し、別タスクが 100 ms 周期で `(esp_timer, TSF)` ペアを取り、線形回帰で換算する。較正ペアは **中点フィット** `t_mid = (t_before + t_after)/2` を使う (`t_before` 単独だと残差 p99 が 3.5 倍悪化する実機事例)。

### 3.2 AP TSF ↔ unix 時刻: PPS bridge

最後に、AP TSF を unix 時刻に橋渡しする。sniffer は **TSF の 1 秒境界 (TSF が 10⁶ の倍数) で GPIO に PPS パルス**を出し、これを RasPi の `/dev/pps0` (`pps-gpio` overlay) が unix 時刻で打刻する。`(unix_assert, TSF境界)` のペアから

```
bridge_offset = unix_assert − TSF_境界 / 1e6
```

を得る。drift (約 9.5 ppm) は per-second で線形除去する。**この bridge の精度 (残差 sd) は 58 μs** (実機、drift 線形除去後)。

> PPS パルスの安定性は ADALM2000 で連続観測して特性化した。当初は `esp_timer` task でパルスを出しており、その **dispatch jitter** が下限だった (idle sd 14.2 μs/bimodal、1000 Hz 負荷で sd 46 μs)。これを **GPTimer (ハードウェアタイマ) の auto-reload 1 Hz 自走 + アラーム ISR でパルス生成**に置換し、位相は loop が回った時だけ TSF 境界へ再同期する方式にした結果、**idle sd ~5 μs (bimodal 解消)**、100 Hz で取りこぼし 0、**高負荷でも PPS が途切れない**。残課題は、1000 Hz の極端負荷時に HID の単一 loop で WiFi rx と TSF 較正を両立できず位相が ~150 μs ドリフトする構造的限界 (PPS タイマの範囲外。本番制御レート 100 Hz では非問題)。

---

## 4. radio_metrics: 端末の自己申告チャネルと `meta`

HID は自身の WiFi イベントを **port `52000 + robot_id` に broadcast JSON** で出す独立チャネルを持つ (`robot_comm_spec/radio_metrics.md` v2.0.0)。production の上り (port 50000、制御基板起源) には計測情報を混ぜない (責任分界) ため、計測専用チャネルを分けている。

| 種別 | タイミング | 主な内容 |
|---|---|---|
| `rx_dl` | 下りコマンド受信時 | 受信 TSF、相関キー (送信ペイロードから抜いた unix 時刻 / cycle_count) |
| `tx_ul` | 上り送信直前 | 送信 TSF、ul_seq |
| `hb` | 1 Hz | 生存確認 + 送信 TSF (制御基板が無くても流れる) |

### off-board 多点 join 用の `meta` フィールド

各メッセージの **JSON 先頭に固定長 HEX の `meta`** を置く (`"RM"` + version + type + robot_id + hid_seq、payload 先頭から**オフセット 9 に固定**)。

これにより、**JSON パーサを持たない軽量な観測者 (空中 sniffer / 有線 SPAN) が、フレームをオフセット 9 で特定し `hid_seq` で多点突合**できる。送信時刻のような「送信前に未知の値」は payload に入れない — on-air の送出時刻は sniffer が**観測**する。これが計測チャネルを分離した狙いそのものである。

---

## 5. 区間分解

### 5.1 下り (Host → HID)

```
t_tx(AIPC) ─→ t_wire(SPAN) ─→ t_air(sniffer) ─→ t_hid(HID rx)
```

| 区間 | 式 | 意味 |
|---|---|---|
| ① host+有線 | `t_wire − t_tx` | AI 送出 → AP 有線到達 (AIPC↔RasPi クロック整合内、sub-ms) |
| ② AP 滞留 | `t_air − t_wire` | 有線到達 → 空中送出 = AP キュー内滞留 |
| ③ air→HID | `t_hid − t_air` | 空中送出 → HID 受信記録 |
| **total** | `t_hid − t_wire` (= ②+③) | sniffer 非依存 (t_air が相殺) で最も確実。①は除外 |

### 5.2 上り (HID → Host)

```
t_hid(gen) ─→ t_air(sniffer) ─→ t_wire(SPAN)
```

| 区間 | 式 | 意味 |
|---|---|---|
| ① HID→air | `t_air − t_hid` | HID 生成 → 空中送出 (HID 内部 TX 処理) |
| ② air→wire | `t_wire − t_air` | 空中送出 → AP 有線到達 (AP 受信処理 + 転送) |
| **total** | `t_wire − t_hid` | = ①+② |

join key は `hid_seq` (meta)。アンカー時刻は `tx_ul`/`hb` の送信 TSF。

### 5.3 重要: 下りと上りで air/wire の意味が違う

- **下り**は air (AP→端末の送出) と wire (AP への入力) が **AP を挟む** → `air − wire` = **AP 通過 (滞留)**。
- **上り**は air と wire がどちらも **AP の出口**側になりうる。ここで **sniffer は端末の ToDS 原送信 (802.11 addr2 = HID) を観測する必要がある**。AP の FromDS 再送 (addr2 = BSSID) を観測すると、それは AP egress で有線転送より後なので `air→wire` が**負値**になり、区間の意味を失う。ToDS 原送信を使えば air→AP→wire の**直列**となり `air→wire` = AP 受信処理 + 有線転送 (正値) として解釈できる。

これは「sniffer が捉えている air とは何か」を取り違えると符号すら合わなくなる、本手法で最もはまりやすい落とし穴である。

---

## 6. 主要な結果 (確定値)

環境: AP = NEC LN6001-JP (5 GHz ch112 DFS, 11ax open)、スイッチ = Xikestor SKS3200M (SPAN)、計測 = RasPi 5 (RPi OS)。

| 項目 | 値 | 条件 |
|---|---|---|
| **下り OWD median** | **0.80 ms** | PPS bridge, 6h overnight idle |
| 下り OWD p95 / p99 | 1.90 / 5.12 ms | 同上 |
| 下り OWD worst | 803 ms | DFS/queue freeze イベント時 |
| **下り損失率** | **0.0072 %** | 6h, 2,160,001 送信 / 156 欠落 |
| **計測精度 (PPS bridge sd)** | **58 μs** | drift 9.52 ppm 線形除去後 |
| 上り区間 (idle) | ① 0.53 / ② +0.35 / total 0.86 ms | hb 経由、meta join |
| AP queue freeze | 65 events / 6h | DFS radar 検査周期と整合 |
| DL broadcast DTIM 遅延 | median 19.6 ms / max 108 ms | beacon ~100ms × DTIM=1 |
| AIPC↔RasPi 時計一致度 | ~200 μs | NTP master 化後 |

**混雑下の挙動** (WiFi ch112 を iperf で飽和):

- 崩壊は線形でなく**「崖」**: 容量 (~72 Mbps) 手前まで損失 0 %・tail 一桁 ms だが、容量到達で tail が p99 ~250 ms、損失数 % に急崩壊。
- **上り負荷の方が下り制御に有害** (同帯域で下り損失 11.6 % vs 4.6 %)。外部 STA の上り送信が AP の下り送信機会 (TXOP) を奪うため。

---

## 7. 限界と今後

- **PPS jitter**: 当初 esp_timer task の dispatch jitter が下限 (idle sd 14 μs、高負荷 sd 46 μs) だったが、**GPTimer 自走 PPS** に置換して idle sd ~5 μs・高負荷でも継続に改善済 (上記 §3.2)。残: 1000 Hz 極端負荷での HID 較正 starve による位相ドリフト (本番 100 Hz では非問題)。
- **USB-Eth の hwtstamp 非対応**: wire 側は SW timestamp (`SO_TIMESTAMPING` softirq)。内蔵 NIC の PHC ハードタイムスタンプ併用で更に精度向上の余地。
- **上り production フレームの直接計測**: 50000 (制御基板起源) は payload を触れないため、同時送出の `tx_ul`/`hb` (meta 付き) をプロキシとして観測している。

---

## 8. 再現用の構成とツール

| 役割 | 機材 |
|---|---|
| 計測 Host | Raspberry Pi 5 (RPi OS)、内蔵 eth0 = SPAN 受信 (promisc + `SO_TIMESTAMPING`)、USB-Eth = 計測 LAN |
| 空中 sniffer | ESP32-C5 devkit (promiscuous、各フレームに TSF を付与、GPIO PPS 出力) |
| 端末 HID | XIAO ESP32-C5 (radio_metrics 送出、GPIO PPS 出力) |
| スイッチ | ポートミラー (SPAN) 対応機 (AP ポートを mirror) |
| PPS 観測 (任意) | ADALM2000 (PPS Δt 連続特性化) |

主要ソフト (本リポジトリ `tools/`): 収集デーモン `rpi_daemon/gtnlv_rpid.py` (sniffer UART + SPAN AF_PACKET + `/dev/pps0` + radio_metrics socket)、OWD 解析 `owd_analyzer/`、ライブ WebUI `dashboard/` (FastAPI + SSE + uPlot、区間分解をリアルタイム表示)。

---

## 関連ドキュメント (本リポジトリ内、詳細)

- `docs/measurement_architecture.md` — 機器役割・3 軸時刻同期・誤差予算
- `docs/phase3_findings.md` — 確定数値と実測ログ (6h overnight, 混雑試験ほか)
- `docs/pps_sync_design.md` — PPS 高精度同期の設計と ADALM2000 解析
- `docs/live_dashboard.md` — ライブ計測 + WebUI の運用リファレンス
- `robot_comm_spec/radio_metrics.md` — radio_metrics チャネル仕様 (`meta` §3.0、多点観測 §4.3)
