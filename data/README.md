# 生データ

報告値（[../README_JP.md](../README_JP.md) §0/§6）の元になった**計測 run の生データ**。

## run: `batch_6r_060hz_6h_w52ch36`

- 条件: ロボット **6 台 @ 60 Hz**、5 GHz **W52 ch36（非DFS）**、`rx_dlb` batch firmware、**6 時間連続**。
- 取得: Raspberry Pi 5（eth0 SPAN + `/dev/pps0` + sniffer C5）。詳細は `run_meta.json`。
- 形式: NAS の per-chunk parquet を**テーブル毎に 1 ファイルへ統合（zstd 圧縮）**。計 150 MB。

| ファイル | 内容 | 主な列 | 用途（報告値との対応） |
|---|---|---|---|
| `rx_dl.parquet` | HID の下り受信記録（rx_dlb 展開、n=7.78M） | `robot_id, cycle_count, t_hid_rx_tsf_us, rssi, batch_seq` 等 | **下り OWD**（P3）、**下り損失**（cycle_count 欠番） |
| `wire.parquet` | eth0 SPAN の有線フレーム（n=8.16M） | `robot_id, cycle_count, t_wire_phc, t_tx_unix, src, dst` | **下り/上り OWD**（P1 wire 基準） |
| `metrics_raw.parquet` | radio_metrics 生 JSON（rx_dlb/hb、n=513k） | `robot_id, t_rpid_recv_unix, json` | **上り OWD**（rx_dlb の `tx`/`hid_seq`）、**上り損失**（`bseq` 欠番） |
| `pps_gpio.parquet` | PPS の GPIO assert（UNIX 打刻、n=21.6k） | `unix_assert, sequence` | **PPS bridge**（TSF↔UNIX） |
| `pps_uart.parquet` | PPS の UART マーカー（TSF、n=21.4k） | `t_rpid_recv_unix, tsf_us, esp_us` | **PPS bridge** |
| `sniffer_frame.parquet` | sniffer の air 捕捉（n=939k） | `robot_id, cycle_count, tsf_us, rssi, src, dst` 等 | air leg（多台数下りは A-MPDU で欠落、報告値には未使用） |
| `sniffer_hb.parquet` | sniffer hb（n=21.4k） | — | 補助 |
| `run_meta.json` | run メタ（機材・同期・ネットワーク） | — | 計測条件の出典 |

## 解析（再現）
**§0 報告値の正本は [`../analysis/plot_owd.py`](../analysis/plot_owd.py)**（`python analysis/plot_owd.py` で本ディレクトリを読み、Mean/Var/SD/median/p99/Max・`<p99` を再現）。
vendor の `owd_analyzer` は同じ手法のフル実装（チーム内運用版）で、`plot_owd.py` は提出用の自己完結スクリプト。
PPS bridge（窓120s）を構築し、下り `SPAN→HID`・上り `HID→wire` の OWD と損失を算出する。
解析コードは [../components/GreenTea_NetworkLatencyViewer/tools/owd_analyzer](../components/GreenTea_NetworkLatencyViewer/tools/owd_analyzer)、
手順は [../components/GreenTea_NetworkLatencyViewer/testplan.md](../components/GreenTea_NetworkLatencyViewer/testplan.md)。
duckdb で `read_parquet('data/batch_6r_060hz_6h_w52ch36/<table>.parquet')` として読める。
