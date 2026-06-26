# 解析

[`../data/`](../data/) の生データから報告メトリクス（片方向 OWD・損失・PPS bridge 精度）を算出する。

## 報告値の再現（ワンショット）

README_JP §0/§6 の上下 OWD 統計（**全データ Mean/Var/SD/median/p99/Max** と **`<p99` 外れ値除去**）と分布図を再生成：

```bash
python analysis/plot_owd.py        # data/・results/ への相対パスで自動解決（要 duckdb/numpy/matplotlib）
# 別 run を解析する場合: python analysis/plot_owd.py --data <run_dir> --out <png>
```

標準出力に下り/上りの `全データ` と `<p99 除去` 行（Var 11.54/4.20・5.97/4.59 等）を表示し、`results/owd_updown.png` を出力する。

## 解析コード（vendor 取り込み済み）
- OWD 統計 + 損失: [components/GreenTea_NetworkLatencyViewer/tools/owd_analyzer](../components/GreenTea_NetworkLatencyViewer/tools/owd_analyzer)（`pps_bridge_owd.py` / `longagg.py` 等）
- 実機手順・コマンド例: [components/GreenTea_NetworkLatencyViewer/testplan.md](../components/GreenTea_NetworkLatencyViewer/testplan.md)

## 流れ
1. `pps_gpio` × `pps_uart` を時刻ペアリング → **窓 120s 分割の線形 bridge**（AP TSF ↔ RasPi UNIX、残差 sd ≈ 3.7 µs）。
2. **下り**: `wire` × `rx_dl` を `(robot_id, cycle_count)` で join → `t_hid_rx_tsf/1e6 + bridge_off(t_wire_phc) − t_wire_phc`。
3. **上り**: `metrics_raw` の `rx_dlb`（`hid_seq`,`tx`）× `wire`(52000) を `(robot_id, hid_seq)` で join → `t_wire_phc − (tx/1e6 + bridge_off)`。
4. 損失: 下り=`cycle_count` 欠番（wrap 補正）、上り=`bseq` 欠番。

duckdb で `read_parquet('data/batch_6r_060hz_6h_w52ch36/<table>.parquet')` として読める。算出方法の詳細は [README_JP §6](../README_JP.md)。
