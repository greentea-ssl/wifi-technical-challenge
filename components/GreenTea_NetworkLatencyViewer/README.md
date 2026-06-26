# GreenTea_NetworkLatencyViewer

RoboCup SSL における Wi-Fi 6 (802.11ax) 環境下の通信遅延を、TSF 同期を用いて
マイクロ秒単位で計測・可視化・分析する統合ツールセット。

**主目的**: [RoboCup SSL 2026 Radio Communications Challenge](https://robocup-ssl.github.io/technical-challenge-rules/2026-radio-communications-challenge.html) への提出 (締切 2026-06-26、プレゼン 2026-07-03)。

## 確定数字

6h overnight、LN6001-JP + Xikestor SKS3200M + DFS ch112 (詳細 [`docs/phase3_findings.md`](docs/phase3_findings.md)):

- 下り OWD **wifi_leg median 2.31 ms** (RasPi 内部基準、clock 非依存)、p99 **3.54 ms**、worst-case **2.52 sec**
- DL 損失率 **0.134 %**、UL pair rate **99.96 %**
- AP queue freeze **65 events / 6h** (event 間隔 median 245s ≈ DFS radar check 周期)
- AP unicast vs broadcast: median(uc) − median(bc) = **+7.85 ms**
- DL broadcast の DTIM 遅延: median 19.6 ms / max 108 ms (beacon ~100ms × DTIM=1)

## アーキテクチャ概要

```
[host PC AIPC .4.160] ─→ [Xikestor SKS3200M] ─→ [LN6001-JP .4.1] ──HE ch112 DFS──→ [reflector C5 .4.111]
                              │ AP port mirror         │
                              ↓                        │
                       [RasPi5 eth0 promisc]           │
                       (macb PHC /dev/ptp0、ns ts)     │
                                                       ↓
                       [RasPi5 USB-Eth .4.103] ←── :50001/:52001 broadcast ─┘
                              ├─ /dev/ttyUSB0 sniffer C5 (PROMIS, UART 2Mbps)
                              ├─ /dev/ttyUSB1 reflector C5 (USB 電源のみ)
                              └─ wlP1p1s0 AX210 PCIe (補助 monitor)
```

- **AIPC は計測コード不要** (production AI そのまま、black-box 化)
- **計測は RasPi5 に集約** — AP 内部にも touch しない (会場提供 AP に対応)
- **片方向遅延 (上下別) を直接計測** — RTT 換算ではなく per-packet OWD

## ドキュメント

| doc | 内容 |
|---|---|
| **[docs/architecture.md](docs/architecture.md)** | 設計書本体 (RasPi 中心 / AP 非依存) |
| **[docs/measurement_architecture.md](docs/measurement_architecture.md)** | 機器役割・時刻同期フロー (NTP/PHC/AP-TSF 3 軸)・時刻ペア別誤差予算 |
| **[docs/phase3_findings.md](docs/phase3_findings.md)** | **現行確定数字** (6h overnight、LN6001 + Xikestor + DFS ch112 + PHC wire bridge) |
| [docs/lessons_learned.md](docs/lessons_learned.md) | 不採用にした選択肢と試行錯誤集約 (rejected alternatives + in-project failures) |
| [docs/sync_alternatives.md](docs/sync_alternatives.md) | Wi-Fi 非依存の時刻同期手段比較 |
| [docs/proposals/radio_metrics_v2x.md](docs/proposals/radio_metrics_v2x.md) | `robot_comm_spec` への `52000+id` チャネル提案 |
| (archive) `docs/phase0_runbook.md` / `phase1_findings.md` / `phase2_findings.md` | 過去 Phase の記録 (参照用) |

`CLAUDE.md` は Claude Code 用のリポジトリ guide。

## 実装コンポーネント

```
tools/
├── pc_emulator/                  # AI 模擬下り送出
├── esp_firmware/
│   ├── metrics_radio/            # HID 用 .h/.cpp ライブラリ
│   ├── metrics_radio_reflector/  # HID 模擬 sketch
│   └── sniffer/                  # 本番 sniffer (binary + cal listener)
├── sniffer_runner/               # sniffer binary decoder (t_rpid_recv_unix 含む)
├── cal_sender/                   # uc/bc 較正パケット送出 + 解析
├── rpi_daemon/
│   ├── gtnlv_rpid.py             # 計測 RasPi デーモン
│   └── wire_capture.py           # eth0 SPAN + PHC hwtstamp (AF_PACKET+SO_TIMESTAMPING)
└── owd_analyzer/
    ├── analyze.py                # OWD 統計 + loss
    └── air_wire_diff.py          # scapy で air × wire pcap join (DTIM 計測)
```

## 動作確認 (loopback)

AI マシン無しで end-to-end 試験できる構成:

```bash
# 1. devkit ESP32-C5 ×2 を /dev/ttyUSB0 (sniffer) / /dev/ttyUSB1 (reflector) に接続
./tools/.bin/arduino-cli upload -p /dev/ttyUSB1 --fqbn esp32:esp32:esp32c5 tools/esp_firmware/metrics_radio_reflector
./tools/.bin/arduino-cli upload -p /dev/ttyUSB0 --fqbn esp32:esp32:esp32c5 tools/esp_firmware/sniffer

# 2. gtnlv-rpid 起動 (RasPi5 上)
python3 tools/rpi_daemon/gtnlv_rpid.py --robot-ids 1 --duration 60 \
    --sniffer-port /dev/ttyUSB0 --sniffer-baud 2000000 \
    --out-dir overnight_results

# 3. pc_emulator (host PC 上、reflector の IP は ARP table から取得)
python3 tools/pc_emulator/pc_emulator.py --robot-id 1 \
    --target 192.168.4.111 --port 40001 --rate 100 --duration 60

# 4. 解析
python3 tools/owd_analyzer/analyze.py --in-dir overnight_results --out-dir analysis
```

## 規約

- 設計書は日本語、`[要検証]` / `[要裏取り]` 注釈を使う
- コミットメッセージは Conventional Commits 風 + 日本語 (`docs(architecture): ...`, `feat: ...`)
- 過去の経緯記述は `docs/lessons_learned.md` に集約、現行仕様 doc には残さない
