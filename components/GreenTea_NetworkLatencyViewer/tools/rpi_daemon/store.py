#!/usr/bin/env python3
# gtnlv 記録部 (recorder) — sink 分離レイヤ。
#
# 収集部 (gtnlv_rpid の各 reader thread) が emit する生 event を、
# pluggable な sink に振り分けて記録する。docs/measurement_pipeline_v2.md §5.2。
#
#   LiveSink   : SQLite (tmpfs、/dev/shm/gtnlv_live.db)。直近 keep_s 秒の ring。
#                WebUI/analyzer が SELECT で window 取得 (tail 廃止)。SD 非消費。
#   RecordSink : append-only CSV (永続)。提出データ。既存 analyze.py 互換の
#                ファイル名/スキーマを維持 (cycle_count 等の列が増えるのみ)。
#
# 設計方針:
#   - 生値記録。DL/UL の cycle_count join は計測部 (analyzer) の責務でここでは
#     しない。各 source の event をそのまま raw table に落とす (§5.1)。
#   - hot path を軽く: put() は deque append のみ。flush thread が batch 書込。
#   - record 側は per-event の有効フラグ (Stage 4 で UI トグル) を見て tee する。

from __future__ import annotations

import collections
import csv
import shutil
import sqlite3
import threading
import time
from pathlib import Path

# ----------------------------------------------------------------------
# スキーマ: 各 source stream を 1 table に。列順は CSV header もこの順。
# ingest_unix は ring 削除/部分窓 SELECT 用 (全 table 共通、末尾付与)。
# cycle_count は spec v2.0.0 で追加 (rx_dl / sniffer_frame / wire)。
# ----------------------------------------------------------------------
SCHEMAS: dict[str, list[str]] = {
    "rx_dl": [
        "robot_id", "hid_ip", "hid_seq", "dl_seq", "aipc_seq", "cycle_count",
        "corr_unix_time", "t_rpid_recv_unix", "t_hid_rx_tsf_us", "t_hid_rx_esp_us",
        # spec v2.1.0: rx_dl 自身の上り送信アンカー (上り OWD の HID→air leg 用)
        "t_hid_tx_tsf_us", "t_hid_tx_esp_us",
        # spec v2.1.0: HID が下り受信時に読んだ WiFi.RSSI (dBm、link 品質)
        "rssi",
        "frame_size", "owd_dl_approx_us",
        # spec v2.1.0 §3.1.1: rx_dlb (batch) 展開行に付与。per-frame rx_dl では null。
        # batch_seq=batch 連番 (UDP 損失検出キー)、batch_rxc=累積 rx_dl 受信数 (冗長チェック)。
        "batch_seq", "batch_rxc",
    ],
    "sniffer_frame": [
        "t_rpid_recv_unix", "rx_seq", "t_local_us_lo", "rx_timestamp_us", "tsf_us",
        "bb_format", "rate", "channel", "rssi", "sig_len", "hdr_seq",
        "src", "dst", "fc_lo", "fc_hi", "dropped_lo", "cycle_count",
        "robot_id",  # v3: 計測対象 robot_id (DL=payload off02、radio_metrics=meta byte4)。多 robot join 用
    ],
    "sniffer_hb": [
        "t_rpid_recv_unix", "captured_total", "dropped_total", "t_now_us_lo", "rssi_now",
    ],
    "pps_uart": ["t_rpid_recv_unix", "tsf_us", "esp_us"],
    "pps_gpio": ["unix_assert", "sequence"],
    "uplink": ["robot_id", "hid_ip", "t_rpid_recv_unix", "size_bytes"],
    "tx_ul": [
        "robot_id", "hid_ip", "hid_seq", "ul_seq", "tx_port",
        "t_hid_tx_tsf_us", "t_rpid_recv_unix", "frame_size",
    ],
    "metrics_raw": ["robot_id", "hid_ip", "t_rpid_recv_unix", "json"],
    "wire": [
        "cycle_count", "robot_id", "t_tx_unix", "t_wire_phc", "src", "dst", "frame_size",
    ],
}

# RecordSink の CSV ファイル名 (既存 analyze.py / sniffer_bridge.py 互換)
CSV_FILENAMES: dict[str, str] = {
    "rx_dl": "owd_dl.csv",
    "sniffer_frame": "sniffer.csv",
    "sniffer_hb": "sniffer_hb.csv",
    "pps_uart": "pps_uart.csv",
    "pps_gpio": "pps_gpio.csv",
    "uplink": "uplink_arrivals.csv",
    "tx_ul": "tx_ul.csv",
    "metrics_raw": "metrics_raw.csv",
    "wire": "wire_capture.csv",
}

# ring 削除を保留しない (短い) 周期で回す table の保持窓は keep_s 共通。


class LiveSink:
    """SQLite (tmpfs) ring buffer。put() は deque append のみ、flush thread が
    executemany で batch insert + 周期 ring 削除。"""

    def __init__(self, db_path: str | Path, keep_s: float = 300.0,
                 flush_interval_s: float = 0.5):
        self.db_path = str(db_path)
        self.keep_s = keep_s
        self.flush_interval_s = flush_interval_s
        self._buffers: dict[str, collections.deque] = {
            t: collections.deque() for t in SCHEMAS
        }
        self._lock = threading.Lock()
        self._stop = threading.Event()
        # tmpfs 上の ephemeral DB。耐障害性より速度優先。
        # WAL: 読取り (WebUI/analyzer/手動 SELECT) が writer を block しないようにする。
        # MEMORY journal だと reader が write lock を奪い writer flush が
        # "database is locked" で例外死 → 永続化が静かに停止する (実測で発生)。
        # busy_timeout: ロック時に即 fail せず待機させ、flush thread を死なせない。
        self._conn = sqlite3.connect(self.db_path, check_same_thread=False,
                                     timeout=30.0)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.execute("PRAGMA synchronous=OFF")
        self._conn.execute("PRAGMA temp_store=MEMORY")
        self._conn.execute("PRAGMA busy_timeout=30000")
        self._conn.execute("PRAGMA wal_autocheckpoint=1000")
        for table, cols in SCHEMAS.items():
            coldef = ", ".join(f"{c}" for c in cols)
            self._conn.execute(
                f"CREATE TABLE IF NOT EXISTS {table} ({coldef}, ingest_unix REAL)"
            )
            self._conn.execute(
                f"CREATE INDEX IF NOT EXISTS idx_{table}_ingest ON {table}(ingest_unix)"
            )
        self._conn.commit()
        self._last_ring = time.time()
        self._thread = threading.Thread(target=self._flush_loop, daemon=True)
        self._thread.start()

    def put(self, table: str, row: dict):
        # ingest_unix を付与して deque へ (hot path)
        with self._lock:
            self._buffers[table].append((row, time.time()))

    def _flush_loop(self):
        # flush thread は絶対に死なせない: 一時的な locked/IO 例外で抜けると
        # 以降データが SQLite に書かれなくなる (収集 thread は生きたままなので
        # 一見正常に見え、永続化だけ静かに停止する)。全例外を握って継続。
        while not self._stop.is_set():
            time.sleep(self.flush_interval_s)
            try:
                self._flush_once()
                now = time.time()
                if now - self._last_ring >= 1.0 and self.keep_s > 0:
                    self._ring(now - self.keep_s)
                    self._last_ring = now
            except Exception:
                # 次サイクルで再試行 (busy_timeout で通常は待って成功する)
                continue

    def _flush_once(self):
        with self._lock:
            pending = {t: list(b) for t, b in self._buffers.items() if b}
            for t in pending:
                self._buffers[t].clear()
        if not pending:
            return
        for table, items in pending.items():
            cols = SCHEMAS[table]
            placeholders = ", ".join(["?"] * (len(cols) + 1))
            sql = f"INSERT INTO {table} ({', '.join(cols)}, ingest_unix) VALUES ({placeholders})"
            data = [tuple(row.get(c) for c in cols) + (ts,) for row, ts in items]
            try:
                self._conn.executemany(sql, data)
            except sqlite3.Error:
                pass
        try:
            self._conn.commit()
        except sqlite3.Error:
            # busy_timeout 超過等。次サイクルで再 commit される (この batch は
            # ephemeral live ring なので欠落しても提出 CSV (RecordSink) には影響なし)。
            pass

    def _ring(self, cutoff: float):
        try:
            for table in SCHEMAS:
                self._conn.execute(f"DELETE FROM {table} WHERE ingest_unix < ?", (cutoff,))
            self._conn.commit()
        except sqlite3.Error:
            pass

    def snapshot(self, since: float | None = None) -> dict[str, list[dict]]:
        """ring に入っている行を table 別に返す (pre-roll 用)。since 指定時は
        ingest_unix >= since の行のみ (録画開始の pre-roll を直近数秒に絞り、
        長時間稼働で ring 全体 (~百万行) を毎回コピーして start がブロックするのを防ぐ)。
        未 flush の buffer を先に書き出してから、別 read 接続で取得する。"""
        self._flush_once()
        out: dict[str, list[dict]] = {}
        where = "WHERE ingest_unix >= ?" if since is not None else ""
        params = (since,) if since is not None else ()
        try:
            rconn = sqlite3.connect(self.db_path)
            rconn.row_factory = sqlite3.Row
            for table, cols in SCHEMAS.items():
                rows = rconn.execute(
                    f"SELECT {', '.join(cols)} FROM {table} {where} ORDER BY ingest_unix",
                    params
                ).fetchall()
                out[table] = [dict(r) for r in rows]
            rconn.close()
        except sqlite3.Error:
            pass
        return out

    def close(self):
        self._stop.set()
        self._thread.join(timeout=2.0)
        self._flush_once()
        try:
            self._conn.close()
        except sqlite3.Error:
            pass


class RecordSink:
    """append-only CSV (永続)。提出データ。各 table を個別ファイルに。
    Stage 4 で `enabled` を runtime トグルし pre-roll を実現する。"""

    def __init__(self, out_dir: str | Path, enabled: bool = True):
        self.out_dir = Path(out_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.enabled = enabled
        self._lock = threading.Lock()
        self._writers: dict[str, csv.DictWriter] = {}
        self._files = {}
        for table, cols in SCHEMAS.items():
            f = open(self.out_dir / CSV_FILENAMES[table], "w", newline="", buffering=1)
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            self._files[table] = f
            self._writers[table] = w

    def put(self, table: str, row: dict):
        if not self.enabled:
            return
        self.put_force(table, row)

    def put_force(self, table: str, row: dict):
        """enabled に関わらず書く (pre-roll backfill 用)。"""
        cols = SCHEMAS[table]
        with self._lock:
            self._writers[table].writerow({c: row.get(c) for c in cols})

    def close(self):
        with self._lock:
            for f in self._files.values():
                try:
                    f.close()
                except OSError:
                    pass


def _pa_type(pa, col: str):
    """列名から pyarrow 型を決める (セグメント間スキーマ安定化用)。
    tsf/us 系は値が大きい (≳5e11) ので int64、unix/time/owd は float64。"""
    if col in ("hid_ip", "src", "dst", "json"):
        return pa.string()
    # t_wire_phc は実体が RasPi5 sw RX unix (sub-ms 精度) なので float64 必須。
    # 旧実装は "phc" を拾えず int64 に落ち、秒に切り捨てて SPAN→Air が計算不能だった。
    if "unix" in col or "time" in col or "phc" in col or col.startswith("owd"):
        return pa.float64()
    return pa.int64()


class ParquetRecordSink:
    """Parquet セグメント録画 (外部化用)。各 stream を segment_s 毎に Parquet へ
    rotate し、tmpfs spool → target_dir (NAS マウント等) へ move する。

    - CSV の ~1/10 サイズ (zstd)、列指向で DuckDB/polars が任意マシンから直読可
    - SD 非消費 (spool=tmpfs)。NAS 不達時は spool に残し hot path は止めない
    - put() は in-mem buffer append のみ (hot path 軽量)、別 thread が rotate
    インターフェースは RecordSink 互換 (put/put_force/close/enabled/out_dir)。"""

    def __init__(self, target_dir: str | Path, spool_dir: str | Path | None = None,
                 enabled: bool = True, segment_s: float = 60.0):
        import pyarrow as pa            # 遅延 import (未 install 環境では生成時のみ失敗)
        import pyarrow.parquet as pq
        self._pa = pa
        self._pq = pq
        self.out_dir = Path(target_dir)
        self.out_dir.mkdir(parents=True, exist_ok=True)
        self.spool = Path(spool_dir) if spool_dir else Path("/dev/shm/gtnlv_spool")
        self.spool.mkdir(parents=True, exist_ok=True)
        self.enabled = enabled
        self.segment_s = segment_s
        self._schemas = {t: pa.schema([(c, _pa_type(pa, c)) for c in cols])
                         for t, cols in SCHEMAS.items()}
        self._lock = threading.Lock()
        self._buffers: dict[str, list] = {t: [] for t in SCHEMAS}
        self._seq = {t: 0 for t in SCHEMAS}
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._rotate_loop, daemon=True)
        self._thread.start()

    def put(self, table: str, row: dict):
        if not self.enabled:
            return
        self.put_force(table, row)

    def put_force(self, table: str, row: dict):
        cols = SCHEMAS[table]
        with self._lock:
            self._buffers[table].append({c: row.get(c) for c in cols})

    def _rotate_loop(self):
        # rotate thread は例外で死なせない (NAS 瞬断等でも収集継続)
        while not self._stop.is_set():
            self._stop.wait(self.segment_s)
            try:
                self._flush_segments()
            except Exception:
                continue

    def _flush_segments(self):
        with self._lock:
            pending = {t: b for t, b in self._buffers.items() if b}
            for t in pending:
                self._buffers[t] = []
        for table, rows in pending.items():
            try:
                self._write_segment(table, rows)
            except Exception:
                pass

    def _write_segment(self, table: str, rows: list):
        seq = self._seq[table]
        self._seq[table] += 1
        fname = f"{table}_{seq:06d}.parquet"
        tmp = self.spool / fname
        try:
            tbl = self._pa.Table.from_pylist(rows, schema=self._schemas[table])
        except (self._pa.ArrowInvalid, self._pa.ArrowTypeError, TypeError):
            tbl = self._pa.Table.from_pylist(rows)   # 型不一致は推論にフォールバック
        self._pq.write_table(tbl, tmp, compression="zstd")
        dst_dir = self.out_dir / table
        dst_dir.mkdir(parents=True, exist_ok=True)
        try:
            shutil.move(str(tmp), str(dst_dir / fname))   # NAS へ move
        except OSError:
            pass   # NAS 不達: spool に残置 (次回 daemon 起動時/手動で回収)。hot path 維持

    def close(self):
        self._stop.set()
        self._thread.join(timeout=3.0)
        self._flush_segments()


class Recorder:
    """収集部から呼ばれる facade。有効な sink に fan-out する。
    `put(table, row)` を各 reader thread が呼ぶ (現 write_* helper の置換先)。

    Stage 4: UI 起点の録画 on/off を制御チャネル (unix socket) から受ける。
    record sink が無ければ start_record 時に record_base/<tag> へ on-demand 生成し、
    live ring の現バッファを pre-roll として書き出してから継続 append する。"""

    def __init__(self, live: LiveSink | None = None, record=None,
                 record_base: str | Path = "runs",
                 record_format: str = "csv", record_spool: str | Path | None = None):
        self.live = live
        self.record = record
        self.record_base = Path(record_base)
        self.record_format = record_format   # "csv" | "parquet"
        self.record_spool = record_spool
        self.meta_writer = None   # callable(record_dir) → run_meta.json を書く (daemon が注入)
        # pre-roll は直近 preroll_s 秒だけ (ring 全体だと長時間稼働で start がブロックする)
        self.preroll_s = 5.0
        self._lock = threading.Lock()
        self._recording = record is not None and record.enabled
        self._tag = None

    def make_record_sink(self, out_dir, enabled: bool):
        """record_format に応じて CSV / Parquet sink を生成 (外部化用)。"""
        if self.record_format == "parquet":
            return ParquetRecordSink(out_dir, spool_dir=self.record_spool, enabled=enabled)
        return RecordSink(out_dir, enabled=enabled)

    def put(self, table: str, row: dict):
        if table not in SCHEMAS:
            raise KeyError(f"unknown table: {table}")
        if self.live is not None:
            self.live.put(table, row)
        if self.record is not None:
            self.record.put(table, row)

    def start_record(self, tag: str | None = None) -> dict:
        """録画開始。record sink を必要なら生成し、pre-roll を書いて enable。"""
        with self._lock:
            tag = tag or time.strftime("rec_%Y%m%d_%H%M%S")
            self._tag = tag
            # 毎回の録画開始で新しい run dir (tag) を作る。既存 sink があれば閉じてから
            # 作り直す (旧実装は再利用し record_dir が初回 tag に固定される問題があった)。
            if self.record is not None:
                try:
                    self.record.close()
                except Exception:
                    pass
            out_dir = self.record_base / tag
            self.record = self.make_record_sink(out_dir, enabled=False)
            created = True
            # pre-roll: live ring の直近 preroll_s 秒を record に流す
            preroll = 0
            if self.live is not None:
                since = time.time() - self.preroll_s if self.preroll_s and self.preroll_s > 0 else None
                snap = self.live.snapshot(since=since)
                for table, rows in snap.items():
                    for r in rows:
                        self.record.put_force(table, r)
                        preroll += 1
            self.record.enabled = True
            self._recording = True
            # 測定条件マニフェスト (run_meta.json) を併置 (best-effort)
            if self.meta_writer is not None:
                try:
                    self.meta_writer(str(self.record.out_dir))
                except Exception:
                    pass
            return {"recording": True, "tag": tag, "record_dir": str(self.record.out_dir),
                    "preroll_rows": preroll, "created_sink": created}

    def stop_record(self) -> dict:
        with self._lock:
            if self.record is not None:
                self.record.enabled = False
            self._recording = False
            return {"recording": False, "tag": self._tag}

    def status(self) -> dict:
        return {
            "recording": self._recording,
            "tag": self._tag,
            "record_dir": str(self.record.out_dir) if self.record is not None else None,
            "live": self.live is not None,
        }

    def set_recording(self, on: bool):
        if self.record is not None:
            self.record.enabled = on
        self._recording = on

    def close(self):
        if self.live is not None:
            self.live.close()
        if self.record is not None:
            self.record.close()
