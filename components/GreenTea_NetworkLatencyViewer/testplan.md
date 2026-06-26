# 実機テストプラン

> 目的: (1) 解決済み Issue #1–#7 の**実機回帰検証**、(2) 提出に必要な**報告データの収集**、
> (3) 規則要件の**ネットワーク切替実証**。
> 対象ブランチ: 本リポジトリ `claude/repo-purpose-explanation-Ni0gb`（master 未マージ）、SanRei_HID `dev`。
> 提出 **2026-06-26** から逆算してスケジュールする（§7）。
>
> 本書中の「§n」は提出ランディングページ（[wifi-technical-challenge/overview_jp.md](https://github.com/gochiuma-dev/wifi-technical-challenge/blob/main/overview_jp.md)）の章を指す。
> 計測手法の詳細は [`docs/owd_methodology.md`](docs/owd_methodology.md) / [`docs/measurement_architecture.md`](docs/measurement_architecture.md) /
> [`docs/pps_sync_design.md`](docs/pps_sync_design.md)、確定数字は [`docs/phase3_findings.md`](docs/phase3_findings.md)。

---

## 1. 前提・機材

| 役割 | 機材 | 接続 |
|---|---|---|
| AP | Linksys Velop WRT Pro 7（5 GHz HE ch112 DFS） | 有線 → switch |
| switch (SPAN) | TP-Link TL-SG105E（easy smart、AP ポートを mirror） | mirror → RasPi eth0 |
| 計測 host | Raspberry Pi 5（RPi OS） | eth0=SPAN, USB-Eth=計測LAN |
| sniffer | ESP32-C5 devkit | RasPi `/dev/ttyUSB0`（2 Mbps）、GPIO10→RasPi `/dev/pps0` |
| HID / reflector | ESP32-C5（実機 SanRei_HID / devkit reflector） | `/dev/ttyUSB1` 等 |
| コーチ | host PC（`pc_emulator.py`） | 有線 |
| 切替先 AP（S1 用） | 2 台目 AP / 別 SSID（オープン推奨） | [要設定] |
| 電力計（D5 用） | USB 電力計 or DMM | [要設定] |

**事前条件**
- RasPi を chrony NTP マスター化、コーチを client（一致度 ~200 µs）。セットアップ: [`docs/raspi_setup.md`](docs/raspi_setup.md)。
- `ethtool -T eth0` で hwtstamp/PHC を確認、`/dev/pps0` の 1 Hz event を `ppstest` で確認。
- ファームは検証対象ブランチを書き込み済み（sniffer は [`tools/esp_firmware/sniffer`](tools/esp_firmware/sniffer)、HID は SanRei_HID `dev`）。

---

## 2. テストケース一覧

| ID | 目的 | 紐づく | 主な合否基準 |
|---|---|---|---|
| **R1** | PPS ブリッジの取りこぼし耐性 | #1 | パルス欠落注入後も `bridge_offset` に ±1 s ジャンプが出ない |
| **R2** | 損失集計の reset 耐性 | #2 | HID 再起動を挟んでも loss% が暴騰しない |
| **R3** | sniffer 高 pps ドロップ | #3 | 目標 pps で `g_dropped_total = 0` |
| **R4** | sniffer 較正の外れ値/再associate | #4 | OWD 分布に >1 s の TSF 外れ値が出ない |
| **R5** | 解析統計の定義 | #5 | 既知データで p95/p99・stdev が新定義どおり |
| **R6** | HID data race の外れ値 | #6 | 高下りレート長時間で ~4295 s 級外れ値ゼロ |
| **R7** | HID 再associate 復帰 | #7 | 切替後すぐ較正回復、TSF 異常なし |
| **D1** | 時刻同期の精度評価 | §3 | PPS 残差 1σ ≤ 60 µs、HID↔sniffer Δt 取得 |
| **D2** | 報告7項目（6h overnight） | §0/§6.1 | median/p95/p99/max・loss・Mean/Var 算出 |
| **D3** | チャネル容量 | §6.1 DataRate | iperf3 飽和で実効スループット |
| **D4** | 起動時間 | §0 StartUp | cold boot→初回 rx_dl の自動計測 |
| **D5** | 消費電力 | §0 Power | idle/通信時の mean/variance |
| **D6** | 干渉検出 | §6.1 Interference | RSSI/retry/近隣beacon/DFS freeze 集計 |
| **D7** | 混雑試験 | §6.1/§9 | 負荷時の OWD/損失の劣化を定量化 |
| **S1** | ネットワーク切替実証 | §6.2 | 切替時間・TSF 跳び・再同期・切替中損失 |

---

## 3. 共通手順（ベースライン計測）

```bash
# 計測デーモン（RasPi 上）
python3 tools/rpi_daemon/gtnlv_rpid.py --robot-ids 1 --duration <秒> \
    --sniffer-port /dev/ttyUSB0 --sniffer-baud 2000000 \
    --pps-device /dev/pps0 \
    --out-dir runs/<run名>          # [要確認] --pps-device の正式名

# 下り送出（コーチ PC）
python3 tools/pc_emulator/pc_emulator.py --robot-id 1 \
    --target <HID_IP> --port 40001 --rate 100 --duration <秒>

# 解析
python3 tools/owd_analyzer/analyze.py --in-dir runs/<run名> --out-dir results/<run名>
```
- 生データは `runs/<run名>/`、解析結果（統計・図）は `results/<run名>/` に保存し、run 名で対応付ける。
- 各 run の冒頭で `[pps-bridge] median bridge_offset` と sniffer の dropped 数をログ記録する。

---

## 4. 回帰検証（R1–R7）

### R1 — PPS ブリッジ取りこぼし耐性（#1）
- **手順**: 10 分計測中に PPS を意図的に 1–2 回欠落させる（sniffer GPIO10 配線を瞬断、または sniffer を数秒ビジー化）。
- **確認**: `runs/<run>/post/pps_bridge.csv` の `bridge_offset_s` 列が欠落前後で連続（±1 s の段差が無い）。修正前は段差が出る想定。
- **合否**: 段差なし＝PASS。`bridge_offset` の median が安定。

### R2 — 損失集計の reset 耐性（#2）
- **手順**: 5 分計測の中盤で HID を電源再投入（連番 0 リセット）。
- **確認**: `analyze.py` の DL loss%（dl_seq / aipc_seq）が現実的な値（< ~1%）に留まる。
- **合否**: reset で loss% が 90%超等に暴騰しない＝PASS。

### R3 — sniffer 高 pps ドロップ（#3）
- **手順**: 下り高レート（複数ロボット同時 or `--rate` を上げる）＋ iperf3 で空中を飽和させ、sniffer を高 pps（目標 ≥ 3,000 fps）で 5 分駆動。
- **確認**: フレームレコードの `dropped` カウンタ（`g_dropped_total`）。
- **合否**: 目標 pps で **dropped = 0**（または既知の許容内）。

### R4 — sniffer 較正の外れ値/再associate（#4）
- **手順**: (a) 30 分連続で OWD 分布を取得。(b) 途中で `set_ssid`（S1）を実行。
- **確認**: OWD・`tsf_us` に **>1 s の外れ値が出ない**。切替直後は calib 無効で `tsf=0`（=該当フレーム除外）になり、stale calib による異常 TSF が出ない。
- **合否**: 巨大外れ値ゼロ＝PASS。

### R5 — 解析統計の定義（#5）
- **手順**: 既知の小規模データ（例 N=100 の合成 or 短時間 run）で `analyze.py` を実行。
- **確認**: p95/p99 が nearest-rank `ceil(q*n)-1`、stdev が標本（n-1）で算出されているか手計算と一致。上り突合が 1 対 1（同一 rx の多重マッチ無し）。
- **合否**: 定義どおり＝PASS。

### R6 — HID data race の外れ値（#6）
- **手順**: 下り 100–1000 Hz で 1 時間以上連続（`udp_rx_task` と `metrics_broadcast_task` を競合させる）。
- **確認**: 下り OWD・上り OWD に **~4295 s（2³² µs）級の外れ値がゼロ**。
- **合否**: torn-read 由来外れ値ゼロ＝PASS。

### R7 — HID 再associate 復帰（#7）
- **手順**: S1 の切替を 5 回繰り返す。
- **確認**: 各切替後、較正が短時間（~数百 ms＝較正4ペア相当）で回復し、回復後の TSF 換算が正常。`a` が [0.99,1.01] 内。
- **合否**: 全切替で正常回復＝PASS。

---

## 5. 提出データ収集（D1–D7, S1）

### D1 — 時刻同期の精度評価（§3）
- **PPS 残差**: `pps_bridge.csv` から drift（ppm）を線形回帰除去後の残差 1σ を算出（目標 ≤ 60 µs）。
- **HID↔sniffer Δt**: 両機の GPIO PPS をオシロ（ADALM2000）で同時観測し Δt 分布。
- **NTP offset**: chrony の offset 時系列を記録。
- **記録**: PPS Δt 分布図、NTP offset 時系列、drift 回帰図 → `results/sync/`。

### D2 — 報告7項目（6h overnight、§0/§6.1）
- **手順**: idle で **6 時間連続**（§3 共通手順、HE ch112 DFS）。
- **算出**: 下り/上り OWD の **Mean / Variance / Max ＋ median/p95/p99**、Average Packet Loss、Detect Interference。
- **重点**: 規定の **Mean / Variance を必ず算出**（現状未確定）。

### D3 — チャネル容量（§6.1 DataRate）
- **手順**: iperf3 で AP–HID/reflector 間を飽和、実効スループットを実測（参考値 ~72 Mbit/s）。
- **記録**: 実効容量、計測条件（方向・並列数）。

### D4 — 起動時間（§0 StartUp、未確定）
- **手順**: HID を cold boot し、**電源投入→最初の rx_dl 受信**までを自動計測（reflector で再現）。複数回で mean/variance。
- **要実装/確認**: 自動計測スクリプトの有無。無ければ簡易計測（電源 GPIO トリガ + 初回 rx_dl タイムスタンプ）。

### D5 — 消費電力（§0 Power、未確定）
- **手順**: USB 電力計で HID の idle / 100 Hz 通信時の電力を測定、mean/variance。
- **記録**: 測定器・条件（電圧・周辺機能 on/off）。

### D6 — 干渉検出（§6.1 Interference）
- **手順**: sniffer ログから RSSI 分布・retry-bit 率・近隣 AP beacon 一覧、DFS radar 周期と相関する AP キュー freeze（参考: 65 events/6h、間隔 median 245 s）を集計。
- **記録**: 検出ロジックと結果（Yes の根拠）。

### D7 — 混雑試験（§6.1/§9）
- **手順**: スマホ動画再生 + RasPi wlan0 経由 iperf3 で AP を圧迫し、HID 配送率・PPS jitter・sniffer drop・OWD/損失の劣化を観測。上り負荷/下り負荷を分けて比較。
- **記録**: 負荷条件ごとの OWD/損失曲線、崩壊点（容量崖）。

### S1 — ネットワーク切替実証（§6.2）
- **手順**: CAN ブリッジ（下り `41000 + robot_id`）に切替指示を送る。
  ```bash
  # 例: robot_id=1 → port 41001 へ JSON UDP 送信
  echo -n '{"type":"set_ssid","ssid":"<切替先SSID>","password":""}' \
      | nc -u -w1 <HID_IP> 41001        # [要確認] SanRei_HID/tools/udp_sender.py でも可
  ```
- **計測**: ①切替時間（指示→再associate→**最初の rx_dl**）、②切替時の TSF 跳び量、③較正再同期に要する時間、④切替中の下りパケット損失。
- **記録**: 切替シーケンスのタイムライン、5 回試行の所要時間統計。R2/R4/R7 と同時に取得すると効率的。

---

## 6. 記録・成果物の置き場

| 種別 | 置き場 |
|---|---|
| 生データ（run ごと） | `runs/<run名>/` |
| 解析結果・図 | `results/<run名>/` |
| 同期精度（D1） | `results/sync/` |
| 確定数字の正本 | [`docs/phase3_findings.md`](docs/phase3_findings.md) |
| 提出反映先 | [wifi-technical-challenge/overview_jp.md](https://github.com/gochiuma-dev/wifi-technical-challenge/blob/main/overview_jp.md) §0/§6 |

---

## 7. 実行順序とスケジュール（提出 2026-06-26 逆算）

1. **Day 1**: セットアップ確認（NTP/PPS/SPAN/health）＋ R3/R5（短時間で済む回帰）
2. **Day 1 夜**: D2（6h overnight）＋ R1/R6（長時間で同時取得）
3. **Day 2**: D1（同期精度・オシロ）、S1＋R2/R4/R7（切替系をまとめて）
4. **Day 2–3**: D3/D6/D7（容量・干渉・混雑）
5. **Day 3**: D4/D5（起動時間・電力、計測系の用意が要る）
6. **Day 4**: 結果を提出 overview に反映、英語版 overview 着手、不足分の再計測予備日

**優先度**: D2（報告本体）＞ S1（規則要件）＞ R1/R6（信頼性の要）＞ D1 ＞ 残り。
時間が逼迫したら D4/D5（起動時間・電力）は「方法を明記し計測中」で提出も可（採点はデータ品質・再現性重視）。

---

## 8. リスク・注意

- **DFS（ch112）**: radar 検出で AP が channel 変更/休止する。worst-case（Max 803 ms）の要因。計測中の DFS イベントはログに残す。
- **ブランチ未マージ**: 検証は feature ブランチ。提出前に master へマージし、提出側のローカル取り込み（self-contained 化）と整合させる。
- **切替先 AP**: S1/R7 には 2 台目 AP か別 SSID が必要（オープン推奨）。hidden AP なら `set_ssid` の directed connection を活用。
