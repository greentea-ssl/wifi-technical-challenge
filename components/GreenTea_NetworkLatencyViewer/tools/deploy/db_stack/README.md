# Phase2 解析 DB スタック (TimescaleDB + Grafana)

別マシン (DB マシン) で起動する任意の解析基盤。**必須ではない** —
NAS 上の Parquet を `tools/owd_analyzer/duckdb_analyze.py` や DuckDB/Grafana で
直接読むだけでも横断解析・可視化はできる。live push や本格 SQL 運用をしたい場合に使う。

## 起動 (DB マシン)
```bash
cd tools/deploy/db_stack
docker compose up -d
# Grafana   http://<DBマシンIP>:3000  (admin/admin → 変更)
# TimescaleDB <DBマシンIP>:5432  (gtnlv/gtnlv、init.sql で hypertable 作成済)
```

## 使い方の選択肢
1. **NAS Parquet を DuckDB で直読 (DB 不要・推奨の出発点)**
   Grafana に DuckDB datasource、または `duckdb_analyze.py` を DB マシンで実行。
2. **TimescaleDB に live push (PgSink、実装は要望時)**
   daemon に `PgSink` を追加し `--record-format` と併せて INSERT。Grafana の
   PostgreSQL datasource で live ダッシュボード。`init.sql` の hypertable に入る。
3. **Parquet を一括ロード**
   `duckdb` で Parquet → `COPY` で TimescaleDB に投入し横断 SQL。

## 注意
- sniffer_frame は高レート。圧縮ポリシ (init.sql、7日) で容量抑制。
- live push する場合は batch INSERT + 失敗時 drop/retry で daemon hot path を守ること
  (LiveSink/RecordSink と同思想)。
