#!/usr/bin/env python3
# GreenTea Network Latency Viewer (FastAPI 版) — データ源レイヤ。
#
# 現行 gtnlv-rpid の tmpfs/out CSV を tail で読み、Live/Overview 用の
# 集計値を返す。pandas を使わず stdlib のみ (RasPi の SSE ループを軽く保つ)。
#
# 将来 SQLite live store へ移行する場合は本ファイルの DataSource 実装だけ
# 差し替えれば server.py / フロントは不変 (docs/measurement_pipeline_v2.md §5.5)。

from __future__ import annotations

import csv
import glob
import io
import math
import os
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]

# 現行 record (out/) + Live ビュワー用 tmpfs (/dev/shm/gtnlv_live/)
OUT_GLOBS = [
    str(REPO_ROOT / "out" / "*"),
    "/dev/shm/gtnlv_live/*",
]

CSV_NAMES = {
    "owd_dl":     "owd_dl.csv",
    "sniffer":    "sniffer.csv",
    "sniffer_hb": "sniffer_hb.csv",
    "pps_bridge": "pps_bridge.csv",
    "m2k_dt":     "dt.csv",
    "wire":       "wire_capture.csv",
}

LIVE_WINDOW_S = 10.0          # 移動平均 window 秒
LIVE_TAIL_BYTES = 4_000_000   # tail 読込 size
ACTIVE_THRESH_S = 5.0         # この秒数以内に更新があれば「計測中」

HID_OUI = "D0:CF:13"          # Espressif (XIAO C5 / devkit 共通)

NAN = float("nan")


# ----------------------------------------------------------------------
# 低レベル: run 探索 + CSV tail
# ----------------------------------------------------------------------
def list_runs() -> list[dict]:
    """全 OUT_GLOBS 配下の run を mtime 降順で。symlink (latest) は除外。"""
    now = time.time()
    out = []
    seen = set()
    for pattern in OUT_GLOBS:
        for p in glob.glob(pattern):
            path = Path(p)
            if path.is_symlink() or not path.is_dir() or path.name in seen:
                continue
            csvs = list(path.glob("*.csv"))
            if not csvs:
                continue
            mt = max(c.stat().st_mtime for c in csvs)
            seen.add(path.name)
            out.append({
                "name": path.name,
                "path": str(path),
                "mtime": mt,
                "is_tmpfs": str(path).startswith("/dev/shm/"),
                "active": (now - mt) <= ACTIVE_THRESH_S,
            })
    out.sort(key=lambda r: r["mtime"], reverse=True)
    return out


def run_dir_for(name: str) -> Path | None:
    for pattern in OUT_GLOBS:
        for p in glob.glob(pattern):
            if Path(p).name == name and Path(p).is_dir():
                return Path(p)
    return None


def find_active_run() -> str | None:
    best = (None, 0.0)
    for r in list_runs():
        if r["active"] and r["mtime"] > best[1]:
            best = (r["name"], r["mtime"])
    return best[0]


def tail_rows(path: Path, max_bytes: int = LIVE_TAIL_BYTES) -> list[dict]:
    """CSV 末尾だけを dict 行で返す。header は別読みして結合。"""
    if not path.exists():
        return []
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            header = f.readline().decode("utf-8", errors="replace")
            if size > max_bytes:
                f.seek(-max_bytes, os.SEEK_END)
                f.readline()  # 部分行を捨てる
            body = f.read().decode("utf-8", errors="replace")
        return list(csv.DictReader(io.StringIO(header + body)))
    except Exception:
        return []


# ----------------------------------------------------------------------
# 統計ヘルパ (stdlib)
# ----------------------------------------------------------------------
def _floats(rows: list[dict], col: str) -> list[float]:
    out = []
    for r in rows:
        v = r.get(col)
        if v is None or v == "":
            continue
        try:
            f = float(v)
        except ValueError:
            continue
        if f == f:  # not nan
            out.append(f)
    return out


def _ints(rows: list[dict], col: str) -> list[int]:
    out = []
    for r in rows:
        v = r.get(col)
        if v is None or v == "":
            continue
        try:
            out.append(int(float(v)))
        except ValueError:
            continue
    return out


def quantile(sorted_vals: list[float], q: float) -> float:
    n = len(sorted_vals)
    if n == 0:
        return NAN
    if n == 1:
        return sorted_vals[0]
    idx = q * (n - 1)
    lo = math.floor(idx)
    hi = math.ceil(idx)
    if lo == hi:
        return sorted_vals[lo]
    return sorted_vals[lo] * (hi - idx) + sorted_vals[hi] * (idx - lo)


def _stats(vals: list[float]) -> dict | None:
    if not vals:
        return None
    s = sorted(vals)
    n = len(s)
    mean = sum(s) / n
    return {
        "n": n,
        "median": quantile(s, 0.5),
        "mean": mean,
        "p95": quantile(s, 0.95),
        "p99": quantile(s, 0.99),
        "min": s[0],
        "max": s[-1],
    }


def _seq_col(rows: list[dict]) -> str | None:
    """損失検出キー列名。cycle_count (v2) を優先、無ければ aipc_seq (現行)。"""
    if not rows:
        return None
    head = rows[0]
    if "cycle_count" in head:
        return "cycle_count"
    if "aipc_seq" in head:
        return "aipc_seq"
    return None


# ----------------------------------------------------------------------
# Live 集計 (SSE で push)
# ----------------------------------------------------------------------
def compute_live(run: str, window_s: float = LIVE_WINDOW_S) -> dict:
    """直近 window の Total OWD 時系列 + leg + packet traffic。
    現状 total (AIPC tx → RasPi 着 broadcast) のみ。wire/air leg は
    wire_capture/sniffer 並走 join が揃ったら追加 (現行 app.py と同じ制約)。
    """
    out = {
        "ok": False, "now_text": "—", "t_max": None,
        "total": None, "series": [],
        "legs": {"aipc_wire": None, "wire_air": None, "air_hid": None},
        "traffic": None,
    }
    rd = run_dir_for(run)
    if rd is None:
        return out
    rows = tail_rows(rd / CSV_NAMES["owd_dl"])
    if not rows or "t_rpid_recv_unix" not in rows[0] or "corr_unix_time" not in rows[0]:
        return out

    # window 抽出 (t_rpid_recv_unix 基準)
    recv = _floats(rows, "t_rpid_recv_unix")
    if not recv:
        return out
    t_max = max(recv)
    out["t_max"] = t_max
    out["now_text"] = time.strftime("%H:%M:%S", time.localtime(t_max))

    win = []
    for r in rows:
        try:
            tr = float(r["t_rpid_recv_unix"])
            ts = float(r["corr_unix_time"])
        except (ValueError, KeyError, TypeError):
            continue
        if tr != tr or ts != ts:
            continue
        if t_max - window_s <= tr <= t_max + 1.0:
            win.append((tr, (tr - ts) * 1e6))  # owd μs (raw, broadcast 戻り含む)
    if not win:
        return out

    win.sort()
    out["series"] = [{"t": t, "owd_us": o} for t, o in win]
    st = _stats([o for _, o in win])
    out["total"] = st
    out["ok"] = True

    out["traffic"] = _compute_traffic(rd, rows, t_max, window_s)
    return out


def _compute_traffic(rd: Path, owd_rows: list[dict], t_max: float, window_s: float) -> dict | None:
    seq_col = _seq_col(owd_rows)
    if seq_col is None:
        return None
    win = []
    for r in owd_rows:
        try:
            tr = float(r["t_rpid_recv_unix"])
            sq = int(float(r[seq_col]))
        except (ValueError, KeyError, TypeError):
            continue
        if t_max - window_s <= tr <= t_max + 1.0:
            win.append(sq)
    if not win:
        return None
    n_recv = len(win)
    lo, hi = min(win), max(win)
    span = hi - lo + 1
    delivered = len(set(win))
    loss = (span - delivered) / span * 100 if span > 0 else 0.0
    traffic = {
        "seq_col": seq_col,
        "tx_rate_hz": n_recv / window_s,
        "cumulative_tx": hi + 1,
        "loss_pct": loss,
        "delivered": delivered,
        "expected": span,
        "missing": span - delivered,
        "air_to_hid_rate_hz": None,
        "air_hid_loss_pct": None,
    }
    # sniffer air → HID frame rate
    sn = tail_rows(rd / CSV_NAMES["sniffer"])
    if sn and "dst" in sn[0] and "t_rpid_recv_unix" in sn[0]:
        hid_frames = 0
        for r in sn:
            try:
                tr = float(r["t_rpid_recv_unix"])
            except (ValueError, KeyError, TypeError):
                continue
            if t_max - window_s <= tr <= t_max + 1.0:
                if str(r.get("dst", "")).upper().startswith(HID_OUI):
                    hid_frames += 1
        traffic["air_to_hid_rate_hz"] = hid_frames / window_s
        if hid_frames > 0:
            traffic["air_hid_loss_pct"] = max(0.0, (hid_frames - n_recv) / hid_frames * 100)
    return traffic


# ----------------------------------------------------------------------
# Overview 集計 (一発取得)
# ----------------------------------------------------------------------
def compute_summary(run: str) -> dict:
    rd = run_dir_for(run)
    out = {"run": run, "owd": None, "loss": None, "data_rate_kbps": None,
           "pps_bridge": None, "m2k_dt": None, "sniffer": None}
    if rd is None:
        return out

    owd_rows = tail_rows(rd / CSV_NAMES["owd_dl"])
    if owd_rows:
        approx = _floats(owd_rows, "owd_dl_approx_us")
        st = _stats(approx)
        if st:
            out["owd"] = {k: (v / 1000.0 if k in ("median", "mean", "p95", "p99", "min", "max") else v)
                          for k, v in st.items()}  # μs → ms
        seq_col = _seq_col(owd_rows)
        if seq_col:
            seqs = _ints(owd_rows, seq_col)
            if seqs:
                lo, hi = min(seqs), max(seqs)
                span = hi - lo + 1
                delivered = len(set(seqs))
                out["loss"] = {
                    "seq_col": seq_col,
                    "delivered": delivered, "expected": span,
                    "loss_pct": (span - delivered) / span * 100 if span > 0 else 0.0,
                }
                out["data_rate_kbps"] = delivered * 64 * 8 / 1000.0

    ppsb = tail_rows(rd / CSV_NAMES["pps_bridge"])
    if ppsb:
        off = _floats(ppsb, "bridge_offset_s")
        delays = _floats(ppsb, "uart_delay_ms")
        if len(off) >= 2:
            diffs = [(off[i] - off[i - 1]) * 1e9 for i in range(1, len(off))]  # ns
            sd = _stats(diffs)
            out["pps_bridge"] = {
                "n": len(off),
                "range_ms": (max(off) - min(off)) * 1000,
                "adj_diff_sd_us": (_std(diffs) / 1000.0) if diffs else NAN,
                "uart_delay_median_ms": quantile(sorted(delays), 0.5) if delays else NAN,
                "uart_delay_max_ms": max(delays) if delays else NAN,
            }

    m2k = tail_rows(rd / CSV_NAMES["m2k_dt"])
    if m2k:
        dt = _floats(m2k, "dt_us")
        st = _stats(dt)
        if st:
            out["m2k_dt"] = st

    sn = tail_rows(rd / CSV_NAMES["sniffer"])
    if sn:
        rssi = _floats(sn, "rssi")
        n_bcast = sum(1 for r in sn if r.get("dst") == "FF:FF:FF:FF:FF:FF")
        hb = tail_rows(rd / CSV_NAMES["sniffer_hb"])
        dropped = 0
        if hb:
            d = _ints(hb, "dropped_total")
            dropped = d[-1] if d else 0
        out["sniffer"] = {
            "n_frame": len(sn),
            "n_bcast": n_bcast,
            "n_ucast": len(sn) - n_bcast,
            "rssi_median_dbm": quantile(sorted(rssi), 0.5) if rssi else None,
            "dropped_total": dropped,
        }
    return out


def _std(vals: list[float]) -> float:
    n = len(vals)
    if n < 2:
        return NAN
    m = sum(vals) / n
    return math.sqrt(sum((v - m) ** 2 for v in vals) / (n - 1))


# ======================================================================
# SQLite live store (Stage 5 analyzer + Stage 6 GUI)
#   recorder の LiveSink (/dev/shm/gtnlv_live.db) を直読みし、PPS bridge で
#   TSF→unix 変換した真の DL OWD と cycle_count 損失を live で算出する。
# ======================================================================
import os as _os
import sqlite3 as _sqlite3

LIVE_DB = _os.environ.get("GTNLV_LIVE_DB", "/dev/shm/gtnlv_live.db")


def live_db_active(thresh_s: float = ACTIVE_THRESH_S) -> bool:
    # WAL モードでは書込は -wal に行き、本体 db の mtime は checkpoint 時しか
    # 更新されない。-wal/-shm は毎書込で更新されるので、3 ファイルの最新 mtime
    # を見る (本体だけ見ると WAL 運用で常時 inactive 誤判定になる)。
    newest = 0.0
    for suffix in ("", "-wal", "-shm"):
        try:
            mt = Path(LIVE_DB + suffix).stat().st_mtime
            if mt > newest:
                newest = mt
        except OSError:
            continue
    if newest == 0.0:
        return False
    return (time.time() - newest) <= thresh_s


def _live_conn():
    # read-only で開く (writer = daemon と競合しない)
    return _sqlite3.connect(f"file:{LIVE_DB}?mode=ro", uri=True, timeout=1.0)


def _pps_bridge_offset(conn, window_s: float = 60.0) -> float | None:
    """直近の pps_gpio(unix_assert) と pps_uart(tsf_us) を順序対応でペアにし、
    bridge_offset = unix_assert - tsf_us/1e6 の中央値を返す。drift は ppm 級なので
    live window では定数オフセット近似で十分 (tsf_unix = tsf_us/1e6 + offset)。"""
    try:
        g = [r[0] for r in conn.execute(
            "SELECT unix_assert FROM pps_gpio ORDER BY ingest_unix DESC LIMIT 30").fetchall()]
        u = [r[0] for r in conn.execute(
            "SELECT tsf_us FROM pps_uart ORDER BY ingest_unix DESC LIMIT 30").fetchall()]
    except _sqlite3.Error:
        return None
    n = min(len(g), len(u))
    if n < 1:
        return None
    offs = sorted(g[i] - u[i] / 1e6 for i in range(n))
    return offs[len(offs) // 2]


def compute_live_sqlite(window_s: float = LIVE_WINDOW_S) -> dict:
    """SQLite live store から直近 window の DL OWD (PPS bridge 換算) + traffic。"""
    out = {
        "ok": False, "now_text": "—", "t_max": None,
        "total": None, "total_bridge": None, "series": [],
        "legs": {"aipc_wire": None, "wire_air": None, "air_hid": None},
        "uplink": None,
        "traffic": None, "bridge_offset": None,
    }
    if not live_db_active():
        return out
    try:
        conn = _live_conn()
    except _sqlite3.Error:
        return out
    try:
        row = conn.execute("SELECT max(ingest_unix) FROM rx_dl").fetchone()
        if not row or row[0] is None:
            return out
        t_max = float(row[0])
        out["t_max"] = t_max
        out["now_text"] = time.strftime("%H:%M:%S", time.localtime(t_max))
        offset = _pps_bridge_offset(conn)
        out["bridge_offset"] = offset
        lo = t_max - window_s
        rows = conn.execute(
            "SELECT corr_unix_time, t_hid_rx_tsf_us, owd_dl_approx_us, cycle_count, ingest_unix "
            "FROM rx_dl WHERE ingest_unix >= ? ORDER BY ingest_unix", (lo,)).fetchall()
        if not rows:
            return out
        raw_us, bridge_us, series, cycles = [], [], [], []
        for corr, tsf, approx, cyc, ing in rows:
            if approx is not None:
                try:
                    raw_us.append(float(approx))
                except (TypeError, ValueError):
                    pass
            # PPS bridge OWD = bridge(t_hid_rx_tsf) - corr_unix_time
            if offset is not None and tsf and corr:
                try:
                    b = (float(tsf) / 1e6 + offset - float(corr)) * 1e6
                    bridge_us.append(b)
                    series.append({"t": float(ing), "owd_us": b})
                except (TypeError, ValueError):
                    pass
            if cyc is not None:
                cycles.append(int(cyc))
        out["total"] = _stats(raw_us)
        out["total_bridge"] = _stats(bridge_us)
        if not series and raw_us:
            # bridge 不可時は raw を時系列に
            series = [{"t": float(r[4]), "owd_us": float(r[2])}
                      for r in rows if r[2] is not None]
        # payload 軽量化: chart 用に最大 ~300 点へ間引き
        if len(series) > 300:
            stride = len(series) // 300 + 1
            series = series[::stride]
        out["series"] = series
        out["traffic"] = _live_traffic(conn, t_max, window_s, cycles)
        out["legs"] = _compute_legs(conn, lo, offset)
        out["uplink"] = _compute_uplink(conn, lo, offset)
        # wire SPAN 並走時は total/時系列を ①(host+有線) 除外版 (②+③ = t_hid−t_wire) に差替。
        # ① は AIPC↔RasPi クロック比較で符号がぶれるため total から除く。
        lt = out["legs"].get("total")
        if lt and lt.get("series"):
            out["total_bridge"] = {"n": lt["n"], "median": lt["median"], "p95": lt["p95"]}
            out["series"] = lt["series"]
            out["total_excl_host"] = True
        out["ok"] = True
    except _sqlite3.Error:
        pass
    finally:
        conn.close()
    return out


def _compute_legs(conn, lo: float, offset) -> dict:
    """cycle_count で sniffer_frame(DL air) と rx_dl を join し、PPS bridge で
    区間別遅延を算出: send_air (AI送出→AP air、sniffer観測) / air_hid (air→HID rx)
    / total (AI送出→HID rx)。各 leg は {median,p95,n,series} (μs)。
    offset(=PPS bridge) が無い / sniffer cycle が無い場合は None。"""
    legs = {"send_air": None, "air_hid": None, "total": None,
            "host_wire": None, "wire_air": None}
    if offset is None:
        return legs
    # 多 robot join: 下りは AI が全 robot へ同一 cycle_count を送るため cycle 単独では
    # 衝突する。sniffer_frame.robot_id (v3) と rx_dl.robot_id で (robot_id, cycle) 複合キー化。
    # robot_id 不明の旧データは cycle 単独で fallback (_ckey)。
    air_rc, air_c = {}, {}  # (rid,cycle)->tsf / cycle->tsf(fallback)
    try:
        for cyc, rid_s, tsf in conn.execute(
            "SELECT cycle_count, robot_id, tsf_us FROM sniffer_frame WHERE ingest_unix >= ? "
            "AND cycle_count IS NOT NULL AND tsf_us > 0 AND dst LIKE ? "
            "ORDER BY ingest_unix", (lo, HID_OUI + "%")).fetchall():
            c = int(cyc)
            if rid_s is not None:
                air_rc.setdefault((int(rid_s), c), float(tsf))
            else:
                air_c.setdefault(c, float(tsf))
    except _sqlite3.Error:
        return legs
    if not air_rc and not air_c:
        return legs
    # wire 到達時刻 ((robot_id,cycle) -> unix)。SPAN 並走 (--wire) 時のみ。3 区間化に使う。
    wire_rc, wire_c = {}, {}
    try:
        for cyc, rid_w, tw in conn.execute(
            "SELECT cycle_count, robot_id, t_wire_phc FROM wire WHERE ingest_unix >= ? "
            "AND cycle_count IS NOT NULL AND t_wire_phc IS NOT NULL "
            "ORDER BY ingest_unix", (lo,)).fetchall():
            c = int(cyc)
            if rid_w is not None:
                wire_rc.setdefault((int(rid_w), c), float(tw))
            else:
                wire_c.setdefault(c, float(tw))
    except _sqlite3.Error:
        pass
    sa, ah, tot, sa_s, ah_s = [], [], [], [], []
    hw, wa, hw_s, wa_s = [], [], [], []
    te, te_s = [], []   # total から ①(host+有線) を除いた値 = t_hid − t_wire (②+③)
    ov = []             # total/②/③ を同一時間軸で重ねる overlay 系列
    try:
        rows = conn.execute(
            "SELECT cycle_count, robot_id, corr_unix_time, t_hid_rx_tsf_us, ingest_unix FROM rx_dl "
            "WHERE ingest_unix >= ? AND cycle_count IS NOT NULL AND t_hid_rx_tsf_us > 0 "
            "AND corr_unix_time IS NOT NULL ORDER BY ingest_unix", (lo,)).fetchall()
    except _sqlite3.Error:
        return legs
    for cyc, rid, corr, hid_tsf, ing in rows:
        c = int(cyc)
        rid = int(rid) if rid is not None else None
        ta = (air_rc.get((rid, c)) if rid is not None else None)
        if ta is None:
            ta = air_c.get(c)   # robot_id 不明 air の fallback
        if ta is None:
            continue
        t_tx = float(corr)
        t_air = ta / 1e6 + offset
        t_hid = float(hid_tsf) / 1e6 + offset
        sa.append((t_air - t_tx) * 1e6)
        ah.append((t_hid - t_air) * 1e6)
        tot.append((t_hid - t_tx) * 1e6)
        sa_s.append({"t": float(ing), "owd_us": (t_air - t_tx) * 1e6})
        ah_s.append({"t": float(ing), "owd_us": (t_hid - t_air) * 1e6})
        tw_v = (wire_rc.get((rid, c)) if rid is not None else None)
        if tw_v is None:
            tw_v = wire_c.get(c)
        if tw_v is not None:
            t_w = tw_v
            hw.append((t_w - t_tx) * 1e6)
            wa.append((t_air - t_w) * 1e6)
            te.append((t_hid - t_w) * 1e6)   # total − ① = ②+③ = t_hid − t_wire
            hw_s.append({"t": float(ing), "owd_us": (t_w - t_tx) * 1e6})
            wa_s.append({"t": float(ing), "owd_us": (t_air - t_w) * 1e6})
            te_s.append({"t": float(ing), "owd_us": (t_hid - t_w) * 1e6})
            ov.append({"t": float(ing), "total": (t_hid - t_w) * 1e6,
                       "wa": (t_air - t_w) * 1e6, "ah": (t_hid - t_air) * 1e6,
                       "cyc": c, "rid": int(rid) if rid is not None else None})

    def pack(vals, series):
        st = _stats(vals)
        if not st:
            return None
        if len(series) > 300:
            stride = len(series) // 300 + 1
            series = series[::stride]
        return {"median": st["median"], "p95": st["p95"], "n": st["n"], "series": series}

    legs["send_air"] = pack(sa, sa_s)
    legs["air_hid"] = pack(ah, ah_s)
    legs["host_wire"] = pack(hw, hw_s)   # host+有線 (AI送出→AP有線到達)
    legs["wire_air"] = pack(wa, wa_s)    # 有線→air = AP queue/滞留
    legs["total_full"] = pack(tot, [])   # AI送出→HID (①含む、AIPCクロック依存)
    # total は ①(host+有線=AIPCクロック比較で不確実) を除いた ②+③ = t_hid−t_wire を採用。
    # wire SPAN 無時のみ ①含む total にフォールバック。
    legs["total"] = pack(te, te_s) if te else pack(tot, [])
    if len(ov) > 300:
        stride = len(ov) // 300 + 1
        ov = ov[::stride]
    legs["overlay"] = ov   # [{t, total, wa, ah}] 同一時間軸 (total/②/③ 重ね描画用)
    return legs


def _compute_uplink(conn, lo: float, offset) -> dict:
    """tx_ul (HID の上り送信報告) から上り OWD と損失を算出。
    UL OWD = t_rpid_recv_unix (52000 metrics 到達, RasPi CLOCK_REALTIME)
             − bridge(t_hid_tx_tsf_us)。HID tx → RasPi app の end-to-end 上り遅延。
    区間分解 (HID→air→wire) は meta/hid_seq join で別途算出 (_compute_uplink_legs)。
    tx_ul が無く (CU 未接続等) ても hb から legs は計算できるので先に求める。"""
    res = {"owd": None, "series": [], "loss_pct": None, "n": 0,
           "missing": 0, "rate_hz": None, "legs": None}
    res["legs"] = _compute_uplink_legs(conn, lo, offset)
    try:
        rows = conn.execute(
            "SELECT t_rpid_recv_unix, t_hid_tx_tsf_us, ul_seq, tx_port, ingest_unix "
            "FROM tx_ul WHERE ingest_unix >= ? AND t_hid_tx_tsf_us > 0 "
            "AND t_rpid_recv_unix IS NOT NULL ORDER BY ingest_unix", (lo,)).fetchall()
    except _sqlite3.Error:
        return res
    if not rows:
        return res
    owd_us, series, per_port = [], [], {}
    t_first = t_last = None
    for trecv, ttx, useq, port, ing in rows:
        if offset is not None:
            try:
                o = (float(trecv) - (float(ttx) / 1e6 + offset)) * 1e6
                if -50_000 < o < 5_000_000:   # clock 未同期等の異常値を除外
                    owd_us.append(o)
                    series.append({"t": float(ing), "owd_us": o})
            except (TypeError, ValueError):
                pass
        if useq is not None and port is not None:
            per_port.setdefault(int(port), []).append(int(useq))
        if t_first is None:
            t_first = float(ing)
        t_last = float(ing)
    res["owd"] = _stats(owd_us)
    sent = got = 0
    for seqs in per_port.values():    # tx_port ごとに ul_seq 連続性で損失算出
        seqs.sort()
        sent += seqs[-1] - seqs[0] + 1
        got += len(seqs)
    if sent > 0:
        res["n"] = got
        res["missing"] = sent - got
        res["loss_pct"] = 100.0 * (sent - got) / sent
    if t_first is not None and t_last and t_last > t_first:
        res["rate_hz"] = got / (t_last - t_first)
    if len(series) > 300:
        stride = len(series) // 300 + 1
        series = series[::stride]
    res["series"] = series
    return res


def _compute_uplink_legs(conn, lo: float, offset) -> dict:
    """上り/hb OWD を HID gen → air(sniffer) → wire(SPAN) の区間に分解。
    join key は hid_seq (radio_metrics.md §3.0 の meta 由来)。HID は全 radio_metrics
    フレーム (rx_dl / tx_ul / hb) 先頭の meta に hid_seq を載せ、sniffer/WireReader が
    payload offset 9 から取り出して cycle_count 列に格納する。送信アンカー (HID がフレームを
    空中送出する直前の TSF→unix) は次の優先で集める:
      - rx_dl.t_hid_tx_tsf_us  (spec v2.1.0、下り 100Hz に追従した高密度。主)
      - hb の t_now_tsf_us      (metrics_raw JSON、1Hz、アイドル/CU 無し時)
      - tx_ul.t_hid_tx_tsf_us   (任意。production 上りプロキシ)
    hid_seq は機体ごと独立カウンタ (各 0 起点) のため多 robot では衝突する。よって全結合を
    (robot_id, hid_seq) 複合キーで行う。robot_id 不明な air 行のみ hid_seq 単独で fallback。
      ① HID→air  = t_air − t_hid   (HID生成→空中送出、sniffer が ToDS 原送信を観測)
      ② air→wire = t_wire − t_air  (空中送出→AP 有線到達)
      total      = t_wire − t_hid"""
    out = {"hid_air": None, "air_wire": None, "total": None, "n": 0, "series": []}
    if offset is None:
        return out
    # アンカー: (robot_id, hid_seq) → t_hid (unix)。rx_dl (v2.1.0 主) / hb / tx_ul (任意)。
    txu = {}
    try:
        for rid, hseq, ttx in conn.execute(
            "SELECT robot_id, hid_seq, t_hid_tx_tsf_us FROM rx_dl WHERE ingest_unix >= ? "
            "AND hid_seq IS NOT NULL AND t_hid_tx_tsf_us > 0 ORDER BY ingest_unix", (lo,)):
            txu.setdefault((int(rid) if rid is not None else None, int(hseq)),
                           float(ttx) / 1e6 + offset)
    except _sqlite3.Error:
        pass
    try:
        for rid, hseq, ttx in conn.execute(
            "SELECT robot_id, hid_seq, t_hid_tx_tsf_us FROM tx_ul WHERE ingest_unix >= ? "
            "AND hid_seq IS NOT NULL AND t_hid_tx_tsf_us > 0 ORDER BY ingest_unix", (lo,)):
            txu.setdefault((int(rid) if rid is not None else None, int(hseq)),
                           float(ttx) / 1e6 + offset)
    except _sqlite3.Error:
        pass
    try:
        import json as _json
        for rid, js in conn.execute(
            "SELECT robot_id, json FROM metrics_raw WHERE ingest_unix >= ? "
            "AND json LIKE '%\"hb\"%' ORDER BY ingest_unix", (lo,)):
            try:
                m = _json.loads(js)
            except (ValueError, TypeError):
                continue
            if (m.get("type") == "hb" and m.get("hid_seq") is not None
                    and m.get("t_now_tsf_us")):
                txu.setdefault((int(rid) if rid is not None else None, int(m["hid_seq"])),
                               float(m["t_now_tsf_us"]) / 1e6 + offset)
    except _sqlite3.Error:
        pass
    if not txu:
        return out
    # air = HID 自身の ToDS 原送信 (src=HID)。(robot_id, hid_seq) 複合キー、robot_id 不明は fallback。
    air_rc, air_c = {}, {}
    try:
        for cyc, rid_s, tsf in conn.execute(
            "SELECT cycle_count, robot_id, tsf_us FROM sniffer_frame WHERE ingest_unix >= ? "
            "AND src LIKE ? AND tsf_us > 0 AND cycle_count IS NOT NULL "
            "ORDER BY ingest_unix", (lo, HID_OUI + "%")):
            s = int(cyc)
            if rid_s is not None:
                air_rc.setdefault((int(rid_s), s), float(tsf) / 1e6 + offset)
            else:
                air_c.setdefault(s, float(tsf) / 1e6 + offset)
    except _sqlite3.Error:
        pass
    wire_rc, wire_c = {}, {}
    try:
        for cyc, rid_w, tw in conn.execute(
            "SELECT cycle_count, robot_id, t_wire_phc FROM wire WHERE ingest_unix >= ? "
            "AND dst LIKE '%.255' AND t_wire_phc IS NOT NULL AND cycle_count IS NOT NULL "
            "ORDER BY ingest_unix", (lo,)):
            s = int(cyc)
            if rid_w is not None:
                wire_rc.setdefault((int(rid_w), s), float(tw))
            else:
                wire_c.setdefault(s, float(tw))
    except _sqlite3.Error:
        pass

    def _air(rid, hseq):
        v = air_rc.get((rid, hseq)) if rid is not None else None
        return v if v is not None else air_c.get(hseq)

    def _wire(rid, hseq):
        v = wire_rc.get((rid, hseq)) if rid is not None else None
        return v if v is not None else wire_c.get(hseq)

    ha, aw, tot, ov = [], [], [], []
    for key in sorted(txu, key=lambda k: (k[0] if k[0] is not None else -1, k[1])):
        rid, hseq = key
        th = txu[key]
        ta = _air(rid, hseq)
        tw = _wire(rid, hseq)
        if ta is not None:
            ha.append((ta - th) * 1e6)
        if ta is not None and tw is not None:
            aw.append((tw - ta) * 1e6)
        if tw is not None:
            tot.append((tw - th) * 1e6)
            ov.append({"t": tw, "cyc": hseq, "rid": rid, "total": (tw - th) * 1e6,
                       "hid_air": (ta - th) * 1e6 if ta is not None else None,
                       "air_wire": (tw - ta) * 1e6 if ta is not None else None})
    out["hid_air"] = _stats(ha)
    out["air_wire"] = _stats(aw)
    out["total"] = _stats(tot)
    out["n"] = len(tot)
    if len(ov) > 300:
        st = len(ov) // 300 + 1
        ov = ov[::st]
    out["series"] = ov
    return out


def _live_traffic(conn, t_max, window_s, cycles):
    if not cycles:
        return None
    n_recv = len(cycles)
    lo, hi = min(cycles), max(cycles)
    span = hi - lo + 1
    delivered = len(set(cycles))
    loss = (span - delivered) / span * 100 if span > 0 else 0.0
    out = {
        "tx_rate_hz": n_recv / window_s,
        "cumulative_tx": hi + 1,
        "loss_pct": loss,
        "delivered": delivered, "expected": span, "missing": span - delivered,
        "air_to_hid_rate_hz": None, "air_hid_loss_pct": None,
    }
    try:
        n_air = conn.execute(
            "SELECT count(*) FROM sniffer_frame WHERE ingest_unix >= ? AND dst LIKE ?",
            (t_max - window_s, HID_OUI + "%")).fetchone()[0]
        out["air_to_hid_rate_hz"] = n_air / window_s
        if n_air > 0:
            out["air_hid_loss_pct"] = max(0.0, (n_air - n_recv) / n_air * 100)
    except _sqlite3.Error:
        pass
    return out


def live_per_robot_sqlite(window_s: float = 60.0) -> dict:
    """Live: robot_id 別の下り OWD (PPS bridge / approx) と損失を直近 window_s で。
    集約 (全 robot 混在) でなく 1 台毎に分けて表示するため。"""
    out = {"active": live_db_active(), "window_s": window_s, "robots": [], "bridge_offset_ok": False}
    if not out["active"]:
        return out
    try:
        conn = _live_conn()
    except _sqlite3.Error:
        return out
    try:
        lo = time.time() - window_s
        offset = _pps_bridge_offset(conn)
        out["bridge_offset_ok"] = offset is not None
        rows = conn.execute(
            "SELECT robot_id, corr_unix_time, t_hid_rx_tsf_us, cycle_count, owd_dl_approx_us, ingest_unix "
            "FROM rx_dl WHERE ingest_unix >= ? AND robot_id IS NOT NULL", (lo,)).fetchall()
        by = {}
        for rid, corr, tsf, cyc, approx, ingest in rows:
            d = by.setdefault(int(rid), {"br": [], "ap": [], "cyc": [], "pts": []})
            v = None
            if offset is not None and tsf is not None and corr is not None:
                v = (float(tsf) / 1e6 + offset - float(corr)) * 1e6
                if abs(v) < 1e6:
                    d["br"].append(v)
                else:
                    v = None
            if approx is not None:
                d["ap"].append(float(approx))
            if cyc is not None:
                d["cyc"].append(int(cyc))
            # 時系列点: x = 観測時刻 (corr 優先、無ければ ingest)、y = bridge 優先 approx fallback
            y = v if v is not None else (float(approx) if approx is not None else None)
            t = float(corr) if corr is not None else (float(ingest) if ingest is not None else None)
            if y is not None and t is not None:
                d["pts"].append((t, y))
        for rid in sorted(by):
            d = by[rid]
            br = sorted(d["br"]); ap = sorted(d["ap"])
            ent = {"robot_id": rid, "n": len(ap)}
            if br:
                ent["bridge_median_ms"] = round(br[len(br) // 2] / 1000, 3)
                ent["bridge_p95_ms"] = round(br[min(len(br) - 1, int(len(br) * 0.95))] / 1000, 3)
                ent["bridge_p99_ms"] = round(br[min(len(br) - 1, int(len(br) * 0.99))] / 1000, 3)
                ent["bridge_max_ms"] = round(br[-1] / 1000, 3)
            if ap:
                ent["approx_median_ms"] = round(ap[len(ap) // 2] / 1000, 3)
            if d["cyc"]:
                loc, hic = min(d["cyc"]), max(d["cyc"]); span = hic - loc + 1; dd = len(set(d["cyc"]))
                ent["loss_pct"] = round((span - dd) / span * 100, 3) if span > 0 else 0.0
            # 時系列 (x昇順 + 最大 ~200 点に間引き)。frontend は owd_us → ms 換算
            pts = sorted(d["pts"]); stride = max(1, len(pts) // 200)
            ent["series"] = [{"t": round(t, 3), "owd_us": round(y, 1)}
                             for t, y in pts[::stride]]
            out["robots"].append(ent)
    except _sqlite3.Error:
        pass
    finally:
        conn.close()
    return out


def live_summary_sqlite() -> dict:
    """Overview 用: live store から 7 項目相当 + PPS bridge 精度。"""
    out = {"owd": None, "owd_bridge": None, "loss": None, "data_rate_kbps": None,
           "bridge_offset": None, "sniffer": None, "active": live_db_active()}
    if not out["active"]:
        return out
    try:
        conn = _live_conn()
    except _sqlite3.Error:
        return out
    try:
        approx = [float(r[0]) for r in conn.execute(
            "SELECT owd_dl_approx_us FROM rx_dl WHERE owd_dl_approx_us IS NOT NULL").fetchall()]
        st = _stats(approx)
        if st:
            out["owd"] = {k: (v / 1000.0 if k in ("median", "mean", "p95", "p99", "min", "max") else v)
                          for k, v in st.items()}
        offset = _pps_bridge_offset(conn)
        out["bridge_offset"] = offset
        if offset is not None:
            br = [(float(t) / 1e6 + offset - float(c)) * 1e6 for c, t in conn.execute(
                "SELECT corr_unix_time, t_hid_rx_tsf_us FROM rx_dl "
                "WHERE t_hid_rx_tsf_us IS NOT NULL AND corr_unix_time IS NOT NULL").fetchall()]
            stb = _stats(br)
            if stb:
                out["owd_bridge"] = {k: (v / 1000.0 if k in ("median", "mean", "p95", "p99", "min", "max") else v)
                                     for k, v in stb.items()}
        cyc = [int(r[0]) for r in conn.execute(
            "SELECT cycle_count FROM rx_dl WHERE cycle_count IS NOT NULL").fetchall()]
        if cyc:
            lo, hi = min(cyc), max(cyc)
            span = hi - lo + 1
            d = len(set(cyc))
            out["loss"] = {"seq_col": "cycle_count", "delivered": d, "expected": span,
                           "loss_pct": (span - d) / span * 100 if span > 0 else 0.0}
            out["data_rate_kbps"] = d * 64 * 8 / 1000.0
    except _sqlite3.Error:
        pass
    finally:
        conn.close()
    return out


def list_csv_files(run: str) -> list[dict]:
    rd = run_dir_for(run)
    if rd is None:
        return []
    rows = []
    for p in sorted(rd.glob("*.csv")):
        try:
            with open(p, "rb") as f:
                lines = sum(1 for _ in f)
        except OSError:
            lines = 0
        rows.append({"name": p.name, "size_kb": round(p.stat().st_size / 1024, 1), "lines": lines})
    return rows
