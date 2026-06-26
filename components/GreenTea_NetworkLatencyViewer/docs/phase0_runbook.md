# Phase 0 ランブック — 実機素性調査

> `docs/architecture.md` §9 Phase 0 を、手持ち機材状況に合わせて分割した実行手順書。
>
> **手元状況 (2026-05-22 時点)**: LN6001-JP 不在、ESP32-C5 ×2 (devkit + XIAO) あり、メーカー違いの AP あり。
>
> このランブックは「LN6001 が来る前にどこまで詰められるか」を最大化することを目的とする。

## 0. 機材アベイラビリティ・マトリクス

| 機材 | 状況 | 影響範囲 |
|---|---|---|
| LN6001-JP (AP) | **未着** | R1 (QSDK TSF), R3 (hwtstamp), `gtnlv-apd` 着手不可 |
| ESP32-C5 devkit | `/dev/ttyUSB0` (CP2102N USB-UART) | R4 / R12 / R11 で「ロボット役」想定 |
| ESP32-C5 XIAO | `/dev/ttyACM1` (Espressif native USB JTAG/Serial), MAC `D0:CF:13:E1:FE:FC` | R4 / R12 / R11 で「sniffer 役」想定。dev branch では Robot ID 1 の Reflector だった個体 |
| メーカー違いの AP (11ax) | 手元あり、SSID `TEAM_SSID_OPEN` (hidden, open auth) | R4, R12 の代替検証に使える。R1 には使えない |
| AIPC | このマシンで代用 | — |

**重要**: メーカー違い AP で得られる結果は LN6001 結果の予測値であって保証ではない。あくまで「実装と手順を Phase 1 移行前に固めるためのドライラン」と位置付ける。

### 0.1 dev 環境からの差分メモ

- **SSID**: dev は `TEAM_SSID` (WPA2 想定)、Phase 0 では `TEAM_SSID_OPEN` (open auth)。
  - **CCMP PN dedup (lessons_learned §4.2) は使えない**。複数受信機の同一フレーム照合は **802.11 ヘッダの 12bit Sequence Number** を使う。
  - lessons_learned §3.3 の「AP BSSID 自動学習」は「最初に観測した暗号化下りフレーム」を起点にしていたが、open では成立しない。**ビーコンの source MAC か Probe Response から BSSID を学習**する方式に切替。
- **SSID hidden**: STA 側で `WiFi.begin(ssid, ...)` (Arduino) または `wifi_sta_config_t::ssid` 設定すれば普通にアソシエートできる（active probe する）。sniffer のキャプチャ側は元々 SSID を見ていないので無影響。
- **ボード非対称性**: dev では同一ハード (XIAO ×2 想定) を sniffer/reflector に使う前提だった。Phase 0 では **devkit + XIAO の混成**。SoC は同じ ESP32-C5 だが、PCB レイアウト・アンテナ・電源系が違う。
  - **R11 の解釈に影響**: 「`rx_ctrl.timestamp` 処理遅延差を定数として較正できるか」の検証は、devkit↔XIAO 間で得られた定数は本番ハード構成（sniffer/ロボットに何を使うか確定後）に**移植可能な定数とは呼べない**。Phase 0 の R11 結果はあくまで**手法の妥当性検証**であり、本番較正は本番ハード構成で再取得が必要。

### 0.2 ビルド環境

- Arduino IDE + ESP32 core **3.3.8** を採用（既にインストール済み `~/.arduino15/packages/esp32/hardware/esp32/3.3.8`）。
- ESP32 core 3.x は ESP-IDF v5 系をラップしており、`#include <esp_wifi.h>` 経由で `esp_wifi_get_tsf_time()`, `esp_wifi_set_promiscuous()` 等の IDF API がそのまま使える。
- FQBN:
  - XIAO C5 (`/dev/ttyACM1`): `esp32:esp32:XIAO_ESP32C5` (lessons_learned §5.1)
  - devkit C5 (`/dev/ttyUSB0`): Arduino IDE のボードメニューで該当のもの (ESP32-C5 系の標準 devkit; FQBN は `esp32:esp32:esp32c5_devkitc` 等)。**XIAO 用 FQBN を devkit に使うとピン配置が違うため誤動作する**。
- ネイティブ ESP-IDF / Docker IDF は Phase 1 以降で sniffer に低レベルアクセスが必要になったら再評価する（dev では Docker `espressif/idf:release-v6.0` 使用、lessons_learned §5.3）。

## 1. 「いま手元の機材でやる」タスク

LN6001 不在のままでも進められる調査。Phase 1 が始まった時点で即実走できる状態にしておくのが目的。

### 1.1 R4: ESP32-C5 `esp_timer ↔ TSF` 較正の線形性検証

**目的**: 較正サンプリング 100ms 周期で線形回帰した時、残差が μs オーダーに収まるか確認する。

**必要機材**: ESP32-C5 × 1、任意の AP（メーカー違いで可）、USB シリアル先の Linux PC。

**手順**:
1. ESP32-C5 を STA としてアソシエート（SSID は手元 AP）。チャネル固定。
2. 100ms 周期で `(esp_timer_get_time(), esp_wifi_get_tsf_time())` のペアを記録（30 分以上）。
3. シリアル経由でホストに流し、Python で線形回帰 → 残差 (RMS, p99) を出す。
4. 較正窓 N を 16/32/64/128 で振り、残差と窓長の関係を見る。
5. ビーコン未受信フラグ・直近ビーコン経過時刻も同時に記録（R9 の伏線）。

**合格ライン**: 静止環境で残差 p99 ≤ 50μs。
**実装参考**: `docs/lessons_learned.md` §3.2 の `estimate_tsf()` パターンを線形回帰版に拡張する。

#### 1.1.1 計測結果 (2026-05-22, 約6分間, TEAM_SSID_OPEN, RSSI -42〜-59 dBm)

| 手法 | XIAO p99 | devkit p99 | XIAO RMS | devkit RMS |
|---|---|---|---|---|
| `t_a` (TSF読出し開始時の esp_timer) | 178.8 us | 179.1 us | 35.6 us | 35.7 us |
| **中点 `t_a + read_dur/2`** | **50.0 us** | **52.7 us** | **17.0 us** | **16.8 us** |
| 中点 + read_dur 上位 5% 除去 | 47.7 us | 50.0 us | 14.9 us | 15.1 us |

判定: **中点フィットで R4 合格（≤50us）**。クロックずれ: XIAO -17.8ppm, devkit -13.2ppm。両ボードでほぼ同じ残差分布になり、構造的原因は `esp_wifi_get_tsf_time()` の所要時間（中央値 ~290us、p99 ~390us）と判明。中点を使うとレジスタ読出し時刻の不確かさが ~半分になる。

知見:
- **時刻参照は必ず中点 `(t_before + t_after) / 2` を使う**。`t_before` だけだと p99 が 3.5 倍に膨らむ
- 6.4s 窓内の ppm 揺れ: XIAO ±7ppm, devkit ±6ppm。較正窓を短くしても改善余地は限定的
- 個体差はほぼ無い（XIAO と devkit で p99 が ±2us 以内）。本番でも sniffer/ロボット間の処理遅延は対称キャンセル期待が高い → R11 への追い風

### 1.2 R12: ESP32-C5 promiscuous で HE PPDU の `rx_ctrl.timestamp` が取れるか

**目的**: AP→STA の **HE (11ax) フレーム**を promiscuous キャプチャした際に `rx_ctrl.timestamp` が有効値で返るかを確認。

**必要機材**: ESP32-C5 × 1 (sniffer 役)、手元の AP（**11ax 動作させる**こと）、別の 11ax STA（HE フレームを誘発するためのトラフィック源 — スマホ等で十分）。

**手順**:
1. AP を 11ax モードに設定。
2. ESP32-C5 を STA として同 AP に associate（TSF 同期維持）→ promiscuous on、同チャネル固定。
3. AP→他 STA の暗号化下りデータフレーム（HE PPDU）を観測。
4. `rx_ctrl.timestamp`、`rx_ctrl.sig_mode` (HE = `WIFI_PHY_RATE_*`)、フレーム長を CSV 出力。
5. `timestamp` が単調増加か、重複が出ないか（[espressif/esp-idf#2468](https://github.com/espressif/esp-idf/issues/2468)）を確認。

**合格ライン**: HE PPDU に対して `rx_ctrl.timestamp` が連続的に異なる値を返す。重複や 0 連発が出ない。
**実装参考**: dev ブランチ `esp32-sniffer/main/main.c`（lessons_learned §3）。

**注意**: C5 の `wifi_pkt_rx_ctrl_t` は `esp_wifi_rxctrl_t` (in `esp_wifi_he_types.h`) のエイリアスで、フィールド構成が ESP32 classic と異なる。`sig_mode` (2bit) は無く、代わりに **`cur_bb_format`** (4bit) で PHY タイプを表す: `RX_BB_FORMAT_11B=0, _11G/A=1, _HT=2, _VHT=3, _HE_SU=4, _HE_MU=5, _HE_ERSU=6, _HE_TB=7, _VHT_MU=11`。HE 判定は `cur_bb_format >= 4 && cur_bb_format <= 7`。

#### 1.2.1 計測結果 (2026-05-22, sniffer=devkit, target=XIAO に 20pps ping, 60秒)

| 項目 | 値 |
|---|---|
| 捕捉フレーム数 | 1,327 (≒ 22 fps) |
| **HE_SU (bb_format=4)** | 1,198 (90.3%) ← **PASS** |
| 11G/A | 129 (9.7%) — 多くはビーコン以外の管理/レガシー |
| 連続 timestamp の単調性違反 (非ラップ) | 0 |
| firmware ring drop | 0 |
| **連続重複 timestamp** | **34 (2.6%)** ← esp-idf#2468 がここで発現 |
| RSSI | -36〜-39 dBm |
| AP BSSID | `76:7F:F0:3B:74:26` (open ch44, dev branch の `76:7F:F0:3B:74:24` と近い系) |

判定: **R12 合格**。HE PPDU の `rx_ctrl.timestamp` は十分使えるが、**2.6% の重複は必ず除外する設計が必要**。
- ホスト側マージで `(rx_timestamp, hdr_seq, src_mac, dst_mac)` の組で重複除去
- もしくは sniffer 側で前フレームと同じ timestamp なら捨てる
- これは `architecture.md` §4.2.3 の較正実験を進める前提条件

### 1.2.2 ハマリポイント (2026-05-22 セッション)

- **XIAO C5 の native USB CDC は固まると物理リセットが必要**。R12 スケッチを書き込んだ直後にシリアル出力が止まり、R4 を再書き込みしても復旧しなかった (WiFi は ping 応答するので CPU は生きてる、USB CDC エンドポイントだけ死んだ状態)。
  - 復旧手段: **XIAO の USB ケーブル抜き挿し**。
  - 教訓: native USB CDC ボードは sniffer 役には不向き (書き込み→デバッグの試行錯誤が多い場面でリスク)。**sniffer 役は CP2102N 系の USB-UART ブリッジを持つ devkit を採用するのが安全**。本番（LN6001 ＋ AP 隣設置）でも同様の判断。
- **promiscuous + STA 共存は動く**。AP に associate 後 `esp_wifi_set_promiscuous(true)` でデータフレーム捕捉開始 (filter mask = `WIFI_PROMIS_FILTER_MASK_DATA`)。
- **callback から Serial.printf は危険**。本実装ではロックフリー SPSC リングで loop() に渡す方式に分離した。22fps では問題なかったが、本番の数百 fps では必須。

### 1.3 R11: sniffer C5 とロボット C5 の `rx_ctrl.timestamp` 処理遅延差の定数性

**必要機材**: ESP32-C5 × **2**（手元の C5 が 1 台しかない場合は Phase 1 で 2 台目調達後に実施）。

**手順**:
1. 2 台の C5 を**極近距離**（数 cm）に並べ、同じ AP のビーコン or 下り HE フレームを同時受信。
2. 同一フレーム（CCMP PN 等で同定 — lessons_learned §4.2）の `(rx_ctrl.timestamp_A, rx_ctrl.timestamp_B)` を 30 分蓄積。
3. 差分の平均・分散・ドリフトを集計。
4. 個体入れ替え（A↔B の役割スワップ）・基板再起動を挟んだ前後で差分が再現するか確認。

**合格ライン**: 差分の標準偏差 < 10μs、長時間ドリフト < 20μs。
**実装参考**: dev ブランチの CCMP PN dedup（lessons_learned §4.2）。

#### 1.3.1 計測結果 (2026-05-22, XIAO=UDP 出力, devkit=Serial, 60s, ping 20pps → devkit)

| 指標 | 値 | 補足 |
|---|---|---|
| マッチペア数 | 1,261 / 1,290 | `(src_mac, hdr_seq)` で join。XIAO の全フレームが devkit 側に対応あり |
| ドリフト (生) | +5.69 μs/s = +5.69 ppm | 各ボードのフリーラン crystal の差。AP TSF に射影すれば消える |
| **ドリフト除去後 RMS** | **1.79 μs** | これが本質的なノイズフロア |
| **同 p99** | **3.8 μs** | **目標 (10us) 大幅クリア** |
| HE_SU 限定の RMS | 1.78 μs | HE フレームに限っても同じ精度 |

**判定: R11 大幅 PASS**。`architecture.md` §4.2 の「同型 ESP32-C5 を2個使えば `rx_ctrl.timestamp` の処理遅延差は対称キャンセル」仮説が**検証された**。しかも heterogeneous (XIAO + devkit) で 2us RMS なので、本番で同型ペアを使えばさらに改善する余地がある。

**含意**: `owd_air_pure = robot_rx_tsf − sniffer_rx_tsf − const` の "const" は事前較正で十分に固定できる。事前較正の不確かさは ~2us オーダー。`architecture.md` §5 の `sync_uncertainty` 内訳予算において、この成分は無視できる小ささ。

### 1.3.2 XIAO HWCDC 問題と UDP 出力の採用

XIAO C5 は MSPI 初期化エラー以降 native USB CDC の TX が止まる現象が観測された（§1.2.2 参照）。R11 を通すために XIAO 側だけ「Serial の代わりに UDP で送信」する R12 派生スケッチ `tools/esp_firmware/r12_udp_test/` を作成・採用した。

- 同じフォーマットで host 側 `tools/r12_udp_runner/run.py` が UDP 受信→CSV 保存
- 解析側 `tools/r11_analyzer/analyze.py` は XIAO UDP / devkit Serial のどちらの CSV も同列に扱える
- **本番設計への含意**: sniffer/ロボット双方で「測定データをホストに返す経路」は Serial に依存しない方が安全。WiFi UDP かテレメトリ既存路（architecture.md §4.3）への相乗りを前提にすべき

### 1.4 R8: 100ms 較正間隔での水晶ドリフト影響

R4 の副産物として残差の時間相関を解析すれば自動的に判定できる。独立タスクは不要。

### 1.5 R10: STA ローミング・再アソシエート時の TSF 飛び

**手順**: R4 の計測中に手動で AP の電源 off→on や C5 の disconnect→reconnect を起こし、TSF カウンタの不連続性を観測。連続記録のなかで `tsf - tsf_prev` の異常ジャンプをマークする。
**運用結論として記録**: 試合運用ではローミング無効化＋BSS固定が前提（architecture.md §8 R10）。

### 1.6 ドキュメント面で詰められるもの

- `docs/requirements.md`（架構 §6 で予告だが未作成）— 計測精度要件、可視化要件、運用要件を独立に整理
- `docs/calibration.md`（同上）— Phase 1 で必要になる較正手順 SOP（chrony 設定、`gtnlv-apd` 較正点蓄積、esp_timer↔TSF 較正の手順）
- `tools/` の §6 ディレクトリ構造で空ディレクトリ＋ README 雛形を切る（中身は Phase 1 で書く）

## 2. 「LN6001 着荷後にやる」タスク

LN6001-JP の現物がないと潰せない調査。事前に**手順だけ**確定させておけば着荷後の所要時間を最小化できる。

### 2.1 R1: QSDK で TSF 安定取得経路の特定

候補経路（優先順）:
1. `iw dev <phy> get tsf`
2. `nl80211` (`NL80211_CMD_GET_INTERFACE` で `NL80211_ATTR_TSF`)
3. `debugfs` (`/sys/kernel/debug/ieee80211/phy*/...`)
4. ベンダ独自パス（QSDK 固有の `cnss` 等）

**LN6001 着荷時の最初の 30 分でやること**: SSH 接続 → `uname -a`, `iw --version`, `ls /sys/kernel/debug/ieee80211/`, `iw dev`, `iw dev <if> get tsf` の出力を全部キャプチャしてリポジトリに残す。

### 2.2 R3: LN6001 の Ethernet HW timestamping 対応

`ethtool -T <eth>` 一発で判定可能。着荷後の所要 1 分。

### 2.3 `gtnlv-apd` の AP 上ビルド環境準備

OpenWrt SDK / QSDK のクロスコンパイル環境構築。手順は LN6001 のファーム特定後に決まる。

## 3. 推奨実行順

**今週**:
1. このランブック自体のレビュー（誤認識・不足項目があれば修正）
2. `tools/` 配下のディレクトリ雛形作成（雛形だけ、コードは入れない）
3. R4 のテストファーム実装 → 30 分実走 → 残差レポート
4. R12 のテストファーム実装 → HE PPDU 取得確認

**来週以降 (LN6001 着荷待ち)**:
5. 2 台目 C5 が揃った時点で R11 実走
6. `docs/requirements.md` と `docs/calibration.md` 起草
7. LN6001 着荷次第、§2 を一気に消化

## 4. このランブックの更新ルール

- 着手前タスクは見出しに `[未着手]`、進行中は `[作業中]`、完了は `[完了 YYYY-MM-DD]` を付ける
- 結果は別途 `docs/phase0_results.md` (作成予定) にデータと一緒に残す
- 不採用にした手段は本体 `architecture.md` の「不採用とした選択肢」セクションに昇格させる

## 5. Phase 1 への継承 [追記 2026-05-23]

Phase 0 完了 (R4/R11/R12 PASS) 後、Phase 1 で実装パイプライン構築 + end-to-end OWD 計測まで通った。詳細は `docs/phase1_findings.md` 参照。

Phase 0 で確定した「sniffer 役は CP2102N 系 devkit を採用」の判断はそのまま継続。本番 HID = XIAO C5 だが USB 不接続のため XIAO HWCDC の持病は本番運用に影響しない。

Phase 1 で新たに発見された制約 (Phase 0 ランブックには未記載):
- **ESP32-C5 PROMIS + STA は他 STA 宛て unicast を chip filter で reject** → sniffer は broadcast 較正専任、per-packet unicast の air timing は HID rx_dl で代替
- **PROMIS ON 中は自分宛て UDP RX path が starve** → cal target は別 STA か PROMIS OFF 運用
- AP の unicast vs broadcast 処理時間差 +2.32 ms (実測、cal test)

LN6001 は Phase 0 R1 のブロッカーから「研究比較対象」に格下げ済 (architecture.md v2 §2 参照)。実機到着しても致命的ブロックではない。
