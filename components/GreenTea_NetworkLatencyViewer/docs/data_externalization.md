# データ外部化 設計・方式比較

> RasPi5 SD は過去 I/O エラーで交換歴あり。6 robot の高レート sniffer ログ
> (~5-10k fps、6h で CSV 換算 ~17GB/run) を SD 直書きは**書込寿命リスク**。
> データを NAS / DB に外部化し、SD 書込を避け、別マシンで後解析できるようにする。

## 0. 前提: 何を外部化するか

| データ | 性質 | 外部化 |
|---|---|---|
| live SQLite (`/dev/shm/gtnlv_live.db`) | tmpfs 5min ring、WebUI 用、小 | **ローカル据置** (RAM上、SD非消費、外部化不要) |
| RecordSink CSV (提出録画) | 永続・大容量 (sniffer 主) | **これを外部化** |

> 既存アーキテクチャ: `store.py` の sink は pluggable (`LiveSink`/`RecordSink`/`Recorder` facade)。
> 外部化は **RecordSink の出力先変更 or 新 sink 追加** で実現でき、収集 hot-path は無改造。

## 1. 方式比較

### A. NFS/SMB マウント + 既存 CSV 直書き
RecordSink の出力 dir を NAS マウント (`/mnt/nas/gtnlv/runs/`) に向けるだけ。
- ✅ 最小変更 (path 変更のみ)、`analyze.py` 互換維持、SD 書込ゼロ
- ✅ 別マシンは NAS を mount して直読
- ⚠ NAS 書込遅延/瞬断が recorder thread を stall させ得る → **ローカル tmpfs バッファ + 非同期 flush** を噛ませる (WAL 修正と同思想)
- ⚠ CSV は嵩む (~17GB/run)。gzip ローテーション併用推奨
- **適**: NAS が NFS/SMB で常時可用、まず動かしたい

### B. ローカル一時記録 → ラン後 rsync/rclone で NAS 同期
USB SSD をローカルバッファにし、ラン後に NAS へ同期。
- ✅ ラン中ネットワーク非依存 (最も堅牢)、SD 非消費 (SSD 使用)
- ⚠ **USB SSD が要る (現状なし)**、二段階運用、live 不可
- **適**: ネットワーク不安定、live 監視不要、確実性最優先

### C. PostgreSQL / TimescaleDB (別マシン or NAS) へ INSERT
daemon に DB sink 追加、batch INSERT。
- ✅ 任意マシンから **SQL 解析**、複数ラン横断クエリ、リテンション、live 可能
- ✅ TimescaleDB は時系列 hypertable で高レート挿入・圧縮に強い
- ⚠ DB サーバ構築、daemon に sink 実装、高レート batch 最適化、スキーマ移行管理
- **適**: 本格運用・複数ラン蓄積・SQL 解析者がいる

### D. InfluxDB + Grafana (時系列 DB)
line protocol で push、Grafana で live ダッシュボード。
- ✅ 時系列ネイティブ、**live Grafana**、自動ダウンサンプリング/リテンション、軽量 push
- ⚠ per-frame の高カーディナリティ (hid_seq 等タグ) は Influx 不得手 → **集計メトリクス向き**、
  生 per-frame (sniffer 全フレーム) 保存には不向き
- **適**: live 監視 + 集計値。生値は別途 (A/B/E と併用)

### E. Parquet on NAS + DuckDB 解析 ★推奨
RecordSink を Parquet 出力化 (pyarrow、stream は row-group 単位 flush)、NAS に置く。
- ✅ **Parquet は CSV の ~1/10** (sniffer 17GB→~2GB)、列指向で解析高速
- ✅ **DuckDB / polars / pandas が任意マシンで直読** (DB サーバ不要)、NAS 上 Parquet を
  DuckDB が `SELECT ... FROM 'nas/*.parquet'` で直接クエリ
- ✅ SD 非消費、stream/timestamp partition で後解析しやすい
- ⚠ RecordSink の Parquet 化実装 (pyarrow 依存追加)、`analyze.py` は Parquet 読み対応 or DuckDB 経由に
- **適**: 大容量 + 後解析 + サーバレスで完結したい (本プロジェクトに最適)

## 2. 推奨構成

> **段階導入**: まず **A (NFS+CSV、tmpfs バッファ付)** で即外部化し SD を保護 →
> 容量・解析効率が課題化したら **E (Parquet+DuckDB)** に移行。
> live 遠隔監視が欲しければ **D (Influx+Grafana)** を集計メトリクスのみ併設。

| 局面 | 構成 |
|---|---|
| 即・最小変更で SD 保護 | **A**: NAS を NFS mount、RecordSink 出力先を NAS に、tmpfs ローカルバッファ + 非同期 flush |
| 大容量+後解析を本格化 | **E**: RecordSink Parquet 化、NAS 保存、別マシンで DuckDB |
| live 遠隔監視も | **D**: 集計メトリクスを InfluxDB へ push、Grafana |
| 複数ラン横断 SQL | **C**: TimescaleDB |

## 3. 実装メモ (採用方式決定後)

- **A**: `gtnlv_rpid.py --out-dir /mnt/nas/...`。NAS 瞬断対策に RecordSink へ
  「ローカル tmpfs に書き → 別 thread が NAS へ rotate-move」を追加 (WAL 修正と同じく書込 thread を死なせない)。
- **E**: `store.py` に `ParquetRecordSink` 追加 (pyarrow `ParquetWriter`、stream は
  N 行ごと / T 秒ごとに row-group flush)。`analyze.py` / `sniffer_bridge.py` を Parquet 読みに
  (polars/pyarrow) or DuckDB ビューに。
- **C/D**: 新 sink (`PgSink`/`InfluxSink`) を `Recorder` に fan-out 追加。batch + 失敗時 drop/再試行で
  hot-path を守る。

## 5. 採用構成と実装 (2026-06-22 決定: SMB NAS あり + DB マシンあり)

**採用: E (Parquet on SMB NAS) を主軸に実装済。DB マシンは Phase2 (live/SQL) で活用。**

### 実装済 (`store.py` / `gtnlv_rpid.py`)
- `ParquetRecordSink`: stream 毎に `segment_s` (既定60s) 毎 Parquet rotate、
  **tmpfs spool → target(NAS) へ move**。zstd 圧縮。NAS 不達時は spool 残置で hot path 維持。
  tsf_us 等は int64 明示スキーマで精度保持。**実測 CSV比 ~4.6x 圧縮** (sniffer 20.6B/row)。
- daemon フラグ: `--record-format parquet --record <NAS上のrun dir> --record-spool /dev/shm/gtnlv_spool`
- 解析: `tools/owd_analyzer/duckdb_analyze.py <run dir>` (DuckDB、**別マシンで DB サーバ不要**)。
  polars/pandas からも `read_parquet` で直読可。

### SMB NAS マウント (構築済 2026-06-22、検証済)
- NAS: **QNAP `//192.168.1.100/latencylog`** (1015G 空)、認証 `/etc/gtnlv-nas.cred` (root 600、git管理外)。
- **eth1(有線)固定**: `.100` を /32 で eth1 経由に (計測 WiFi 汚染回避)。SMB 接続元 = `192.168.1.231` 確認済、**113 MB/s** (GbE フル)。
- **systemd `gtnlv-nas-mount.service`** (`tools/deploy/`、enable 済): route + mount を再起動後も自動。NAS 不達でも boot を止めない (`nofail`)。
```bash
# 導入 (実施済): cifs-utils、cred、unit
sudo cp tools/deploy/gtnlv-nas-mount.service /etc/systemd/system/ && sudo systemctl daemon-reload
sudo systemctl enable --now gtnlv-nas-mount.service   # /mnt/nas にマウント
```
> **録画起動 (Parquet を NAS へ)**:
> ```bash
> sudo python3 ~/gtnlv/gtnlv_rpid.py --robot-ids 0,1,2,3,4,5 --duration 0 \
>   --live --live-db /dev/shm/gtnlv_live.db --ctrl-sock /tmp/gtnlv.sock \
>   --sniffer-port /dev/ttyUSB0 --sniffer-baud 2000000 --pps-device /dev/pps0 --wire eth0 \
>   --record /mnt/nas/gtnlv/runs/$(date +%Y%m%d_%H%M) --record-format parquet --record-spool /dev/shm/gtnlv_spool \
>   --run-note "home 4hub, AP=自宅 ch40, 6robot idle, SPAN有"
> ```
> spool は tmpfs(RAM)。NAS への move が追いつく限り RAM 使用は数セグメント分。
> **測定条件**: 各 run dir に `run_meta.json` を自動併置 (git版数/chrony同期/IF/IP/sniffer SSID/
> daemon条件/`--run-note` の自由記述)。データと条件が同じ dir に揃う。
> 解析(別マシン): `tools/owd_analyzer/duckdb_analyze.py /mnt/nas/gtnlv/runs/<run>` (DuckDB 直読)。
> **end-to-end 検証済**: 30000行→NAS Parquet→spool全move→DuckDB 直読 OK。

### Phase2: DB マシンで live 監視 + 横断 SQL (任意)
別マシンに **TimescaleDB + Grafana** を docker で立てる構成を `tools/deploy/db_stack/` に用意 (docker-compose + 初期スキーマ)。
daemon 側に `PgSink` を追加すれば集計メトリクスを live push できる (実装は要望時)。
当面は **NAS 上 Parquet を DB マシンの DuckDB/Grafana で読む**だけでも横断解析・可視化は可能 (PgSink 不要)。

## 5.5 会場/WWAN 越しの Tailscale NAS アクセス (保留 2026-06-22)

> 家環境では NAS 直アクセス可なので**保留**。会場 (RasPi5 の internet が WWAN のみになり得る) で
> 自宅 NAS に Tailscale 越しでアクセスする必要が出たら、以下の調査結果から再開する。

実測で確定した事項:
- **transport は問題なし**: WWAN(EC25J irumo) でインターネット到達可、Tailscale は public IP
  (CGNAT でない)・DERP Tokyo で直結/relay とも通る。
- **subnet**: 自宅(NAS)= `192.168.1.x`、**会場 = `192.168.4.x`** で**重複しない**。
- 既存 subnet router: **gochiuma-ryzen9** (tailscale `100.92.12.120` / LAN `192.168.1.134`、
  同一自宅 LAN) が `192.168.1.0/24` を advertise・承認済・online。
- **accept-routes の罠**: RasPi5 で `accept-routes=true` にすると、**自宅 (192.168.1.x) では
  自分のローカル subnet を hijack** し .100 が tailscale0 経由に化ける (実測確認・即 revert 済)。
  会場 (192.168.4.x) なら重複しないので OK。Tailscale は特定 route のみ除外不可 (all-or-nothing)。

再開時の選択肢:
- **A (堅牢・推奨)**: QNAP に Tailscale 導入 → NAS が 100.x。accept-routes 不要・場所非依存・
  ryzen9 非依存。自宅は 192.168.1.100 (eth1 直結) / 会場は NAS の 100.x でマウント。
- **B (既存インフラ)**: location 自動判定で accept-routes トグル (自宅=OFF / 会場 192.168.4.x=ON)。
  ryzen9 online 必須。RasPi5 側だけで完結。
- **共通前提**: 会場の高レート録画は LTE で stream せず**ローカル録画→後で Tailscale 同期**。

## 6. 要決定事項 (ユーザ確認)

1. **NAS の有無とプロトコル**: 既存 NAS? NFS / SMB(CIFS) どちら? IP/共有名?
2. **DB サーバの有無**: PostgreSQL/Influx を立てられる別マシンはあるか?
3. **live 遠隔監視の要否**: ラン中に別マシンからリアルタイム監視したいか (→ D/C)、
   後解析だけで良いか (→ A/E)。
4. **USB SSD の用意可否** (B 案やローカルバッファ用)。
```
