# Phase 1 計測結果と実装知見

> `docs/architecture.md` v2 (RasPi 中心 / AP 非依存) に従って組んだ計測パイプラインで実測した結果と、その過程で判明した制約・落とし穴を集約。チャレンジ提出 (2026-06-26) の本文起草時にここから数値と methodology を引いてくる。

> **後続**: `docs/phase2_findings.md` に Phase 2 (RasPi5 中心 + 6h 連続運用) の確定数字と新しい発見 (AP queue freeze イベント、broadcast 二重受信、analyzer floor 脆弱性等) を集約済。Phase 1 の TSF-bridge 相対値ベース 9.7 ms median は Phase 2 で raw NTP-bound 1.64 ms median + max 1.67 s として再評価された。

## 1. 計測スタックの構成 (Phase 1 確定版)

```
[AIPC] ─────UDP unicast 40000+id─────→ [GS308E] ───→ [AP] ──── HE air ────→ [HID C5]
   payload offset 38-45 = unix_time           │                                  │
   payload offset 52-55 = aipc_seq            │                                  │
                                              └ SPAN → [RasPi 4]                │
                                                       eth0 (gtnlv-rpid)        │
                                                       │                         │
                                                eth1 ←─ 52000+id rx_dl ─────────┘
                                                              JSON broadcast
                                              │                                  │
                                              └─ USB ─── [sniffer C5] (AP 隣) ─┘
                                                         broadcast 補助観測
```

### 主要コンポーネント (本リポ `tools/` 配下)

| 役割 | 場所 | 言語 |
|---|---|---|
| AI 模擬下り送出 | `tools/pc_emulator/pc_emulator.py` | Python |
| HID metrics_radio ライブラリ | `tools/esp_firmware/metrics_radio/` | C++ (Arduino) |
| HID 模擬 reflector | `tools/esp_firmware/metrics_radio_reflector/` | Arduino .ino |
| 本番 sniffer | `tools/esp_firmware/sniffer/` | Arduino .ino |
| sniffer host runner | `tools/sniffer_runner/run.py` | Python |
| 計測 RasPi デーモン | `tools/rpi_daemon/gtnlv_rpid.py` | Python |
| OWD 統計解析 | `tools/owd_analyzer/analyze.py` | Python |
| cal sender / analyzer | `tools/cal_sender/{cal_sender,cal_analyzer}.py` | Python |

## 2. 確定数値

### 2.1 100Hz × 30 分連続運用 (本走)

`pc_emulator → devkit-B reflector` (192.168.1.191) への unicast、30 分間 = 180,000 パケット送出。

```
ラン環境: TEAM_SSID_OPEN (open auth 11ax)、5GHz ch44、AP BSSID 76:7F:F0:3B:74:26
sniffer = devkit-A (/dev/ttyUSB0) で broadcast 較正源として並走
```

#### 下り OWD (相対、観測 floor からの上乗せ μs)

| 指標 | 値 |
|---|---|
| N | 179,952 |
| median | 9,717 μs |
| mean | 10,234 μs |
| stdev | 6,687 μs |
| p95 | 18,777 μs |
| p99 | 28,494 μs |
| p99.9 | 62,662 μs |
| max | 154,865 μs |
| 受信レート | 100.0 Hz over 1799.9 s |

#### 上り OWD (同、reflector の fake uplink @10Hz)

| 指標 | 値 |
|---|---|
| N | 17,984 paired (18,095 中 99.4% paired) |
| median | 9,921 μs |
| mean | 10,159 μs |
| stdev | 10,717 μs |
| p95 | 18,349 μs |
| p99 | 19,348 μs |
| p99.9 | 24,070 μs |
| max | 631,329 μs (1 件外れ値) |

#### パケットロス率 (5 分 × 100Hz、別途取得)

| 指標 | 値 |
|---|---|
| **下り (aipc_seq ベース)** | **0.003%** (1 missing / 29,991 expected) |
| rx_dl 監視 (HID→host broadcast) | 0.000% |
| tx_ul 監視 (同上) | 0.000% |
| **production uplink ↔ tx_ul pair 成功率** | **99.07%** (29 unpaired) ≈ 上り 1% 損失 |

### 2.2 AP unicast vs broadcast 処理時間差 (cal test)

`tools/cal_sender/cal_sender.py` で RasPi (= AIPC マシン) から sniffer に対して broadcast (`192.168.x.255:43000`) と unicast (`<sniffer_ip>:43000`) を 1 ペア/s で 5 分間送出。

```
ラン設定: 2 pairs/sec × 300s = 1,200 cal パケット送出
受信側: sniffer (PROMIS OFF、CAL UDP listener on port 43000)
```

| 指標 | broadcast | unicast |
|---|---|---|
| N 受信 | 599 / 600 (99.8%) | 600 / 600 (100%) |
| median−min (group internal) | 63.3 ms | 65.9 ms |
| p95−min | 109 ms | 112 ms |
| max−min | 143 ms | 202 ms |
| stdev | 29.6 ms | 31.0 ms |

#### 絶対差 (offset 打消し済み)

- **mean(uc) − mean(bc): +2.26 ms** (unicast が遅い)
- **median(uc) − median(bc): +2.32 ms**
- min(uc) − min(bc): −0.28 ms (ベストケースはほぼ同等)

cal の絶対遅延 60-65 ms は AP が「アイドル → 起動」する低 pps での挙動。100Hz 連続 (本走) では AP が warm のため median 10 ms 程度に下がる。

## 3. 重要な発見・制約

### 3.1 ESP32-C5 PROMIS が UDP RX path を starve させる

**発見経緯**: cal test を sniffer 自身を target にして実施 (PROMIS ON 状態) → unicast の 64% がロス + 残った 35% も平均 6.8 秒の遅延。同じテストを sniffer の PROMIS を OFF にして再実施 → unicast 100% 受信 + 正常な stdev 31ms。

**仮説**: 促進モードで cb 起動が多発 (~2,670/s) すると、チップ内部 RX バッファが promiscuous-delivered フレームで埋まり、自分宛て unicast の通常 RX path が starve する。

**含意**:
- sniffer は PROMIS 専任 (UDP listen を併設しない)
- cal 試験は別系統 (sniffer の PROMIS を一時 OFF にして実施、または別 C5 用意)
- HID は PROMIS 非使用なので影響なし
- 過去観測の "Phase 0 R12 で unicast 取れた" は ping target = sniffer 自身なので**通常 RX 経路**で受けていただけ、PROMIS 経路ではない

### 3.2 ESP32-C5 PROMIS は他 STA 宛て unicast を chip filter で reject

**発見経緯**: Phase 1 stress test (pc_emulator 6kHz 送信 to XIAO reflector、devkit sniffer は PROMIS ON で BSSID マッチフィルタ通過)。**cb_total ~22/s で頭打ち** = chip がほとんど cb を起動していない。broadcast 系のみ 1,500+ fps 取れる。

**含意**:
- C5 sniffer で本番 production unicast の per-frame air-side timing は取れない
- per-packet OWD は **HID の rx_dl (TSF) + RasPi SPAN の wire_rx (RT)** だけで成立するため、実用上の問題にはならない
- AP 内滞留の分解は cal test (uc/bc 差) + 連続運用時の OWD 分布で間接的に評価

### 3.3 OWD の絶対値とジッタ源

- 観測される OWD 分布は (1) wire 区間 (~μs)、(2) AP queue (常時 ~10ms 程度)、(3) air 伝搬 (ns)、(4) HID 内部処理 (~ms オーダー) の合計
- AP queue が支配的。cal で見た 60-65ms (低 pps) と 100Hz 本走の 10ms median の差は AP の warm-up 状態の違い
- HID 側の Arduino loop() ジッタは 1-2ms オーダー (reflector で確認)。**本番 HID (SanRei_HID with CAN/UART/Wio) はさらに大きくなる懸念**あり、別途特性化が必要

### 3.4 metrics_radio の loss tracking 機能

`tools/esp_firmware/metrics_radio/` に v2.0.0 提案 + 拡張で:

- `rx_dl` JSON に `aipc_seq` (payload 抽出) + `dl_seq` (HID 内部単調) を追加
- `tx_ul` JSON に `ul_seq` (HID 内部単調) を追加
- ホスト側 `tools/owd_analyzer/analyze.py` が seq ギャップで損失を計算

**proposed spec 差分** (まだ robot_comm_spec に未反映、本リポ内で先行運用):
- downlink_command.md offset 52-55 を `aipc_seq` (uint32 LE) として AI 側に要請
- rx_dl JSON に `dl_seq`, `aipc_seq` 追加
- tx_ul JSON に `ul_seq` 追加

## 4. ハードウェア構成の確定

### 4.1 機材一覧 (本リポでの開発・試験ベンチ)

| デバイス | パス | 役割 |
|---|---|---|
| devkit C5 #1 | `/dev/ttyUSB0` (CP2102N, SN 46adfa...) | sniffer、IP 192.168.1.170 |
| devkit C5 #2 | `/dev/ttyUSB1` (CP2102N, SN 38ca64...) | reflector (HID 模擬)、IP 192.168.1.191 |
| ホスト PC | wired 192.168.1.202 (br0) | AIPC 兼 RasPi (本来 RasPi の役割を兼用中) |

XIAO ESP32-C5 は Phase 0 で R4/R11/R12 通したが、HWCDC が反復試験で固まる持病が出たため、Phase 1+ では bench 機としては不使用 (本番 HID では USB 不接続なので問題なし)。

### 4.2 本番想定の機材リスト

| 機材 | 価格目処 | 用途 |
|---|---|---|
| RasPi 4 (4GB 想定) | ~¥9,000 | 計測 PC (gtnlv-rpid) |
| USB-Ethernet (RTL8153 系で OK) | ~¥1,000 | RasPi eth1 (52000+id 受信用) |
| NETGEAR GS308E | ~¥3,000 | SPAN ミラーリング switch |
| devkit ESP32-C5 (CP2102N) | ~¥2,000 | sniffer |
| (本番 HID は既存 XIAO ロボット内) | - | - |
| **計** (sniffer 機材) | **~¥15,000** | チャレンジ Cost 報告に使用 |

電力 (連続運用):
- RasPi 4: ~5 W
- sniffer C5: ~0.5 W
- GS308E: ~3 W
- USB-Eth: ~1 W
- 計 ~10 W

## 5. 次の作業 (Phase 2 / チャレンジ提出に向けて)

優先度高い順:

1. **クイック AP 切替** (R10、チャレンジ要件): reflector に複数 SSID + 切替ロジック、TSF 飛びと再アソシエート時間を計測
2. **干渉検出** (チャレンジ要件 #4): sniffer 拡張で周辺 AP・retry-bit カウント・RSSI 分布の dump
3. **起動時間** (チャレンジ要件 #5): reflector / HID のコールドブート → 最初の rx_dl 受信までを自動計測
4. **長時間ラン × 混雑シミュレーション**: iperf3 並走で AP 混雑を再現、p99/max 悪化を観測
5. **MIN_OWD_FLOOR_US の経験的較正**: 静止無干渉環境で ground truth と比較し絶対 OWD floor を確定
6. **提出文書起草**: 7 項目それぞれに methodology + numbers + artifacts (firmware / scripts / hardware BOM) を整理
7. **`SanRei_HID` への metrics_radio 組込 PR**: v2.0.0 タグ後

## 6. 既知の改善余地

- gtnlv-rpid v0 は OWD 算出に min-filter bridge を使うが、長時間ランで floor が外れ値で押し下げられる。**rolling window min-filter** に置き換えるべき (v1 改善)
- cal test の絶対遅延 60-65ms (低 pps 時) は AP のアイドル挙動。**チャレンジ提出では「連続運用時 100Hz の本走数字」を主軸**、cal は uc/bc 差のみ採用
- 上り OWD は時間近接ペアリング (5ms 窓) で 99.4% match。残り 0.6% は外れ要因不明、追って調査
- XIAO HWCDC 不安定の根本原因 (ESP-IDF 側? Arduino core 側?) は未調査。本番では USB 不使用なので保留

## 7. 関連ドキュメント参照

- `docs/architecture.md` v2 — 設計の根拠
- `docs/phase0_runbook.md` — Phase 0 R4/R11/R12 結果
- `docs/proposals/radio_metrics_v2x.md` — robot_comm_spec への提案 (v2.0.0-dev branch にローカル commit 済み、未 push)
- `../robot_comm_spec/radio_metrics.md` (submodule 化予定) — v2.0.0 仕様本体
- `../SanRei_HID/` — 本番 HID ファーム (metrics_radio 組込 PR 予定)
