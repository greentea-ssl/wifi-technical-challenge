#!/usr/bin/env python3
# GreenTea Network Latency Viewer — Streamlit dashboard
#
# 提出 (Year 1 Radio Communications Challenge) 7 項目 KPI を可視化:
#   1. One-way Latency (DL/UL OWD via PPS bridge)
#   2. Average Packet Loss (DL/UL)
#   3. Data Rate
#   4. Interference Detection (sniffer.csv 集計)
#   5. Startup Time
#   6. Power
#   7. Cost
#
# Usage:
#   streamlit run tools/dashboard/app.py
#
# 入力: out/<run_tag>/*.csv (gtnlv-rpid 出力) を sidebar で選択。
#       AIPC 上 (`./out/`) と RasPi 上 (gochiuma@192.168.4.212:~/out/) の両方を
#       glob 対象にする。RasPi 側は事前に rsync で取得しておく前提。

from __future__ import annotations

import csv
import glob
import math
import os
import time
from pathlib import Path

import pandas as pd
import plotly.express as px
import plotly.graph_objects as go
import streamlit as st

try:
    from streamlit_autorefresh import st_autorefresh
    HAS_AUTOREFRESH = True
except ImportError:
    HAS_AUTOREFRESH = False


# ============================================================
# 設定
# ============================================================
REPO_ROOT = Path(__file__).resolve().parents[2]
# 通常 record (out/) + Live ビュワー用 tmpfs (/dev/shm/gtnlv_live/) の両方を見る
OUT_GLOBS = [
    str(REPO_ROOT / "out" / "*"),
    "/dev/shm/gtnlv_live/*",
]
# Legacy alias (1 つしか使われていない場所向け)
OUT_GLOB = OUT_GLOBS[0]

# CSV 名の規約 (gtnlv-rpid 出力)
CSV_NAMES = {
    "owd_dl":       "owd_dl.csv",
    "sniffer":      "sniffer.csv",
    "sniffer_hb":   "sniffer_hb.csv",
    "pps_uart":     "pps_uart.csv",
    "pps_gpio":     "pps_gpio.csv",
    "pps_bridge":   "pps_bridge.csv",
    "metrics_raw":  "metrics_raw.csv",
    "tx_ul":        "tx_ul.csv",
    "uplink":       "uplink_arrivals.csv",
    "m2k_dt":       "dt.csv",          # ADALM2000 PPS Δt 計測 (m2k_pps_diff)
    "wire":         "wire_capture.csv",  # PHC hwtstamp (per-packet、aipc_seq 付き想定)
}

# Live tab 設定
LIVE_REFRESH_MS = 2000          # autorefresh 周期 (ms)
LIVE_WINDOW_S   = 10            # 移動平均 window 秒数
LIVE_TAIL_BYTES = 4_000_000     # CSV tail 読込 size (4 MB ≈ 100Hz × ~250 s 程度の余裕)
LIVE_ACTIVE_THRESH_S = 5        # この秒数以内に CSV が更新されていれば「アクティブ」と判定

st.set_page_config(
    page_title="GreenTea Network Latency Viewer",
    page_icon="📡",
    layout="wide",
)


# ============================================================
# data loaders (cached)
# ============================================================
@st.cache_data(show_spinner=False, ttl=10)
def list_runs() -> list[tuple[str, str]]:
    """全 OUT_GLOBS 配下の run を一覧化。**(name, full_path)** タプルを返す。
    mtime 降順 (= 最後に更新されたものが先頭)。
    """
    candidates = []
    for pattern in OUT_GLOBS:
        for p in glob.glob(pattern):
            path = Path(p)
            if path.is_symlink() or not path.is_dir():
                continue
            csvs = list(path.glob("*.csv"))
            if csvs:
                mt = max(c.stat().st_mtime for c in csvs)
                candidates.append((mt, path.name, str(path)))
    candidates.sort(reverse=True)
    return [(name, full) for _, name, full in candidates]


def run_dir_for(name: str) -> Path | None:
    """run name から実 path を解決 (out/ or /dev/shm/gtnlv_live/)。"""
    for pattern in OUT_GLOBS:
        for p in glob.glob(pattern):
            if Path(p).name == name and Path(p).is_dir():
                return Path(p)
    return None


@st.cache_data(show_spinner=False)
def load_csv(run: str, kind: str) -> pd.DataFrame | None:
    name = CSV_NAMES.get(kind)
    if not name:
        return None
    run_dir = run_dir_for(run)
    if run_dir is None:
        return None
    path = run_dir / name
    if not path.exists():
        return None
    try:
        return pd.read_csv(path)
    except Exception:
        return None


def tail_csv(path: Path, max_bytes: int = LIVE_TAIL_BYTES) -> pd.DataFrame | None:
    """CSV の末尾だけ読む。大ファイル (>100MB) でも 2秒周期で耐える。
    1 行目 (header) は別途読み、tail と結合。
    """
    if not path.exists():
        return None
    try:
        size = path.stat().st_size
        with open(path, "rb") as f:
            header = f.readline().decode("utf-8", errors="replace")
            if size > max_bytes:
                f.seek(-max_bytes, os.SEEK_END)
                f.readline()  # 部分行を捨てる
                body = f.read().decode("utf-8", errors="replace")
            else:
                body = f.read().decode("utf-8", errors="replace")
        import io
        return pd.read_csv(io.StringIO(header + body))
    except Exception as e:
        return None


def latest_pps_offset(df_pps: pd.DataFrame | None) -> float | None:
    """直近の bridge_offset_s (PPS GPIO 経由) を返す。"""
    if df_pps is None or df_pps.empty:
        return None
    try:
        return float(df_pps["bridge_offset_s"].iloc[-1])
    except Exception:
        return None


def window_filter(df: pd.DataFrame, t_col: str, t_now: float, window_s: float) -> pd.DataFrame:
    if df is None or df.empty or t_col not in df.columns:
        return df
    t = pd.to_numeric(df[t_col], errors="coerce")
    mask = (t >= t_now - window_s) & (t <= t_now + 1.0)
    return df.loc[mask]


def find_active_run(threshold_s: float = LIVE_ACTIVE_THRESH_S) -> str | None:
    """全 OUT_GLOBS で **今書き込まれている** run を探す (tmpfs 含む)。
    symlink は除外 (run_live.sh の `latest` シンボリックリンクを避ける)。"""
    now = time.time()
    best = (None, 0.0)
    for pattern in OUT_GLOBS:
        for p in glob.glob(pattern):
            path = Path(p)
            if path.is_symlink() or not path.is_dir():
                continue
            csvs = list(path.glob("*.csv"))
            if not csvs:
                continue
            mt = max(c.stat().st_mtime for c in csvs)
            if (now - mt) <= threshold_s and mt > best[1]:
                best = (path.name, mt)
    return best[0]


# ============================================================
# KPI 計算
# ============================================================
def compute_dl_owd_summary(df_owd: pd.DataFrame, df_pps_bridge: pd.DataFrame | None):
    """abs_owd を 4 方式 (raw / global / rolling / pps) で簡易計算。

    sniffer_bridge.py のフル機能は別 script。ここは summary 数字のみ。
    """
    if df_owd is None or df_owd.empty:
        return {}
    n = len(df_owd)
    # raw approximation: owd_dl.csv の owd_dl_approx_us 列 (NTP-bound、overstating)
    raw_med = df_owd.get("owd_dl_approx_us")
    if raw_med is not None and len(raw_med):
        raw_med = pd.to_numeric(raw_med, errors="coerce").dropna()
        if len(raw_med):
            raw_p50 = float(raw_med.median())
            raw_p95 = float(raw_med.quantile(0.95))
            raw_p99 = float(raw_med.quantile(0.99))
            raw_max = float(raw_med.max())
        else:
            raw_p50 = raw_p95 = raw_p99 = raw_max = float("nan")
    else:
        raw_p50 = raw_p95 = raw_p99 = raw_max = float("nan")

    return {
        "n": n,
        "raw_p50_ms": raw_p50 / 1000.0 if raw_p50 == raw_p50 else float("nan"),
        "raw_p95_ms": raw_p95 / 1000.0 if raw_p95 == raw_p95 else float("nan"),
        "raw_p99_ms": raw_p99 / 1000.0 if raw_p99 == raw_p99 else float("nan"),
        "raw_max_ms": raw_max / 1000.0 if raw_max == raw_max else float("nan"),
    }


def compute_loss(df_owd: pd.DataFrame):
    """DL 損失率 = (aipc_seq の欠番 / 期待数)。"""
    if df_owd is None or df_owd.empty or "aipc_seq" not in df_owd.columns:
        return None
    aseq = pd.to_numeric(df_owd["aipc_seq"], errors="coerce").dropna().astype(int)
    if aseq.empty:
        return None
    lo, hi = aseq.min(), aseq.max()
    span = hi - lo + 1
    delivered = aseq.nunique()
    loss = (span - delivered) / span if span > 0 else 0.0
    return {"delivered": int(delivered), "expected": int(span), "loss_pct": loss * 100.0}


def compute_pps_bridge_stats(df: pd.DataFrame | None):
    if df is None or df.empty:
        return None
    off = pd.to_numeric(df["bridge_offset_s"], errors="coerce").dropna()
    delays = pd.to_numeric(df["uart_delay_ms"], errors="coerce").dropna()
    if len(off) < 2:
        return None
    adj = off.diff().dropna() * 1e9   # ns
    return {
        "n": len(off),
        "range_ms": (off.max() - off.min()) * 1000,
        "adj_diff_sd_us": float(adj.std()) / 1000.0,
        "adj_diff_median_us": float(adj.median()) / 1000.0,
        "uart_delay_median_ms": float(delays.median()),
        "uart_delay_max_ms": float(delays.max()),
    }


def compute_m2k_dt_stats(df: pd.DataFrame | None):
    if df is None or df.empty or "dt_us" not in df.columns:
        return None
    dt = pd.to_numeric(df["dt_us"], errors="coerce").dropna()
    if dt.empty:
        return None
    return {
        "n": int(len(dt)),
        "median_us": float(dt.median()),
        "sd_us": float(dt.std()),
        "p95_us": float(dt.quantile(0.95)),
        "p99_us": float(dt.quantile(0.99)),
        "min_us": float(dt.min()),
        "max_us": float(dt.max()),
    }


# ============================================================
# Live tab 計算
# ============================================================
def compute_live_legs(run: str, window_s: float = LIVE_WINDOW_S) -> dict:
    """直近 `window_s` 秒の 4 leg 移動平均を計算。返り値:
      {
        'leg_aipc_wire':   {'median_us': ..., 'p95_us': ..., 'n': ...},
        'leg_wire_air':    {...},
        'leg_air_hid':     {...},
        'total':           {...},
        't_max': max_unix_t,
        'now_text': ...,
      }
    PPS bridge を介して air leg の TSF→unix 変換。aipc_seq join で AIPC tx / HID rx /
    wire RX を対応取り (sniffer は時刻近接で対応)。
    現状実装は **AIPC → HID rx (= total) のみ**、wire / air leg は CSV 揃ったら追加。
    """
    out = {
        "leg_aipc_wire": None, "leg_wire_air": None,
        "leg_air_hid": None, "total": None,
        "t_max": None, "now_text": "—",
    }
    run_dir = run_dir_for(run)
    if run_dir is None:
        return out
    p_owd = run_dir / CSV_NAMES["owd_dl"]
    if not p_owd.exists():
        return out
    df_owd = tail_csv(p_owd)
    if df_owd is None or df_owd.empty:
        return out
    if "t_rpid_recv_unix" not in df_owd.columns or "corr_unix_time" not in df_owd.columns:
        return out
    # window 抽出
    t_recv = pd.to_numeric(df_owd["t_rpid_recv_unix"], errors="coerce")
    t_send = pd.to_numeric(df_owd["corr_unix_time"], errors="coerce")
    mask = t_recv.notna() & t_send.notna()
    df_owd = df_owd.loc[mask]
    if df_owd.empty:
        return out
    t_max = float(pd.to_numeric(df_owd["t_rpid_recv_unix"], errors="coerce").max())
    out["t_max"] = t_max
    out["now_text"] = pd.to_datetime(t_max, unit="s").strftime("%H:%M:%S")
    df_owd = window_filter(df_owd, "t_rpid_recv_unix", t_max, window_s)
    if df_owd.empty:
        return out
    # total: t_rpid_recv − corr_unix (これは broadcast 戻り含む overstating、後で PPS bridge 適用)
    owd_us = (pd.to_numeric(df_owd["t_rpid_recv_unix"], errors="coerce")
              - pd.to_numeric(df_owd["corr_unix_time"], errors="coerce")) * 1e6
    owd_us = owd_us.dropna()
    if len(owd_us):
        out["total"] = {
            "median_us": float(owd_us.median()),
            "mean_us":   float(owd_us.mean()),
            "p95_us":    float(owd_us.quantile(0.95)),
            "p99_us":    float(owd_us.quantile(0.99)),
            "n":         int(len(owd_us)),
            "samples":   owd_us.tolist(),
        }
    # TODO: wire_capture.csv との join で leg_aipc_wire、sniffer + PPS bridge で leg_wire_air / leg_air_hid
    return out


def compute_packet_traffic(run: str, window_s: float = LIVE_WINDOW_S) -> dict | None:
    """直近 window の **パケット送信状況** を集計。RasPi 側 CSV のみから推定:
       - TX rate (Hz)        = rx_dl 件数 / window (AIPC TX rate と等しい、loss 0%想定)
       - 累積送信数          = aipc_seq の最大値 + 1
       - DL loss (window 内) = aipc_seq 欠番 / 期待数
       - air→HID rate (Hz)   = sniffer.csv の dst=HID OUI frame / window
       - HID 内取りこぼし    = air_to_hid - rx_dl
    """
    run_dir = run_dir_for(run)
    if run_dir is None:
        return None
    df_owd = tail_csv(run_dir / CSV_NAMES["owd_dl"])
    if df_owd is None or df_owd.empty:
        return None
    if "t_rpid_recv_unix" not in df_owd.columns or "aipc_seq" not in df_owd.columns:
        return None
    t = pd.to_numeric(df_owd["t_rpid_recv_unix"], errors="coerce")
    if t.dropna().empty:
        return None
    t_max = float(t.max())
    df = df_owd.loc[(t >= t_max - window_s) & (t <= t_max + 1)]
    if df.empty:
        return None
    aipc_seq = pd.to_numeric(df["aipc_seq"], errors="coerce").dropna().astype("int64")
    if aipc_seq.empty:
        return None
    n_recv = len(aipc_seq)
    lo, hi = int(aipc_seq.min()), int(aipc_seq.max())
    span = hi - lo + 1
    delivered = aipc_seq.nunique()
    loss = (span - delivered) / span * 100 if span > 0 else 0.0
    out = {
        "tx_rate_hz": n_recv / window_s,
        "cumulative_tx": hi + 1,
        "loss_pct": loss,
        "delivered": delivered,
        "expected": span,
        "missing": span - delivered,
        "air_to_hid_rate_hz": None,
        "air_hid_loss_pct": None,
        "n_recv": n_recv,
    }
    # sniffer air → HID frame rate
    sn_path = run_dir / CSV_NAMES["sniffer"]
    if sn_path.exists():
        df_sn = tail_csv(sn_path)
        if df_sn is not None and not df_sn.empty and "dst" in df_sn and "t_rpid_recv_unix" in df_sn:
            ts = pd.to_numeric(df_sn["t_rpid_recv_unix"], errors="coerce")
            sn_w = df_sn.loc[(ts >= t_max - window_s) & (ts <= t_max + 1)]
            # dst が HID 想定 OUI (D0:CF:13 = Espressif) — XIAO C5 と devkit 共通
            if not sn_w.empty:
                hid_frames = sn_w["dst"].astype(str).str.upper().str.startswith("D0:CF:13").sum()
                out["air_to_hid_rate_hz"] = float(hid_frames) / window_s
                if hid_frames > 0:
                    # air 観測との比較で HID 内取りこぼし率
                    out["air_hid_loss_pct"] = max(0.0, (hid_frames - n_recv) / hid_frames * 100)
    return out


def network_diagram(legs: dict) -> go.Figure:
    """ネットワーク模式図 (Plotly graph_objects)。
    nodes: [AIPC] [Switch (SPAN)] [AP] [HID]
    edges に leg 名 + 移動平均 median μs を表示。
    """
    # node 座標 (横並び)
    nodes = [
        {"name": "AIPC",      "x": 0.05, "y": 0.5},
        {"name": "Switch/PHC", "x": 0.30, "y": 0.5},
        {"name": "AP (sniffer 並走)", "x": 0.60, "y": 0.5},
        {"name": "HID",       "x": 0.92, "y": 0.5},
    ]
    # edge metadata
    def fmt_us(d):
        if d is None: return "(待機)"
        return f"{d['median_us']/1000:.2f} ms\n(n={d['n']})"
    edges = [
        {"from": 0, "to": 1, "label": "wire RX\n" + fmt_us(legs.get("leg_aipc_wire"))},
        {"from": 1, "to": 2, "label": "air RX\n" + fmt_us(legs.get("leg_wire_air"))},
        {"from": 2, "to": 3, "label": "HID rx\n" + fmt_us(legs.get("leg_air_hid"))},
    ]
    fig = go.Figure()
    # edges (lines)
    edge_x, edge_y = [], []
    for e in edges:
        x0, y0 = nodes[e["from"]]["x"], nodes[e["from"]]["y"]
        x1, y1 = nodes[e["to"]]["x"], nodes[e["to"]]["y"]
        edge_x += [x0, x1, None]
        edge_y += [y0, y1, None]
    fig.add_trace(go.Scatter(
        x=edge_x, y=edge_y, mode="lines",
        line=dict(width=3, color="#888"),
        hoverinfo="none", showlegend=False,
    ))
    # edge labels (中点)
    for e in edges:
        mx = (nodes[e["from"]]["x"] + nodes[e["to"]]["x"]) / 2
        my = (nodes[e["from"]]["y"] + nodes[e["to"]]["y"]) / 2 + 0.10
        fig.add_annotation(
            x=mx, y=my, text=e["label"], showarrow=False,
            font=dict(size=14, color="#0a84ff"),
            bgcolor="rgba(255,255,255,0.9)", bordercolor="#0a84ff",
            borderwidth=1, borderpad=4,
        )
    # nodes (circles + label)
    nx = [n["x"] for n in nodes]
    ny = [n["y"] for n in nodes]
    fig.add_trace(go.Scatter(
        x=nx, y=ny, mode="markers+text",
        marker=dict(size=60, color="#4f8df9", line=dict(width=2, color="#1f4ed8")),
        text=[n["name"] for n in nodes],
        textposition="bottom center",
        textfont=dict(size=14, color="#222"),
        hovertext=[n["name"] for n in nodes],
        hoverinfo="text", showlegend=False,
    ))
    # total OWD (右端外、独立 KPI)
    total = legs.get("total")
    if total is not None:
        fig.add_annotation(
            x=0.99, y=0.05,
            text=f"<b>Total OWD (raw):</b><br>median {total['median_us']/1000:.2f} ms<br>"
                 f"p95 {total['p95_us']/1000:.2f} ms<br>n={total['n']}",
            showarrow=False, xanchor="right",
            font=dict(size=12, color="#222"),
            bgcolor="rgba(255,247,200,0.9)", bordercolor="#c0a000", borderwidth=1, borderpad=6,
        )
    fig.update_layout(
        showlegend=False,
        xaxis=dict(visible=False, range=[0, 1]),
        yaxis=dict(visible=False, range=[0, 1]),
        margin=dict(l=20, r=20, t=30, b=20),
        height=300,
        plot_bgcolor="white",
    )
    return fig


def compute_sniffer_summary(df_sn: pd.DataFrame | None, df_hb: pd.DataFrame | None):
    if df_sn is None or df_sn.empty:
        return None
    n_frame = len(df_sn)
    # RSSI 集計
    rssi = pd.to_numeric(df_sn.get("rssi"), errors="coerce").dropna() if "rssi" in df_sn else None
    rssi_med = float(rssi.median()) if rssi is not None and len(rssi) else None
    # dst 別件数 (broadcast vs unicast 比)
    if "dst" in df_sn:
        n_bcast = int((df_sn["dst"] == "FF:FF:FF:FF:FF:FF").sum())
    else:
        n_bcast = 0
    # dropped 推移 (sniffer_hb)
    dropped = 0
    if df_hb is not None and not df_hb.empty and "dropped_total" in df_hb:
        d = pd.to_numeric(df_hb["dropped_total"], errors="coerce").dropna()
        dropped = int(d.iloc[-1]) if len(d) else 0
    return {
        "n_frame": n_frame,
        "n_bcast": n_bcast,
        "n_ucast": n_frame - n_bcast,
        "rssi_median_dbm": rssi_med,
        "dropped_total": dropped,
    }


# ============================================================
# UI
# ============================================================
def main():
    st.title("📡 GreenTea Network Latency Viewer")
    st.caption(
        "RoboCup SSL 2026 Year 1 Radio Communications Challenge — "
        "計測結果ダッシュボード ([docs/phase3_findings.md](#) §2 確定数字に対応)"
    )

    # ----- sidebar -----
    runs = list_runs()  # list[(name, full_path)]
    if not runs:
        st.error(f"run ディレクトリが見つかりません。\n"
                 f"scan paths: {OUT_GLOBS}")
        st.stop()

    with st.sidebar:
        st.header("Run 選択 (過去 run の閲覧用)")
        run_names = [name for name, _ in runs]
        run_paths = {name: full for name, full in runs}
        run = st.selectbox("対象 run", run_names, index=0)
        full_path = run_paths.get(run, "?")
        is_tmpfs = full_path.startswith("/dev/shm/")
        st.markdown(f"**path**: `{full_path}/`")
        if is_tmpfs:
            st.caption("🔴 tmpfs (live viewer)。再起動で消失")
        st.markdown("---")
        st.caption("※ Live タブはこの選択を無視、アクティブ計測を自動検出します")

    # ----- load -----
    df_owd      = load_csv(run, "owd_dl")
    df_sn       = load_csv(run, "sniffer")
    df_hb       = load_csv(run, "sniffer_hb")
    df_pps_b    = load_csv(run, "pps_bridge")
    df_m2k      = load_csv(run, "m2k_dt")

    # ----- tabs -----
    tabs = st.tabs([
        "🔴 Live",
        "📊 Overview (7 項目 KPI)",
        "⏱️ OWD",
        "📉 Loss",
        "🎯 PPS Δt",
        "📡 Interference",
        "🔁 Raw CSV",
    ])

    # =================== Live ===================
    with tabs[0]:
        # autorefresh (2s 周期) — アクティブ run 検出も毎周期 refresh
        if HAS_AUTOREFRESH:
            st_autorefresh(interval=LIVE_REFRESH_MS, key="live_autorefresh")
        else:
            st.warning("streamlit-autorefresh 未 install — 手動 refresh が必要")

        active_run = find_active_run(LIVE_ACTIVE_THRESH_S)

        head_left, head_right = st.columns([3, 1])
        with head_right:
            st.markdown("**設定**")
            st.caption(f"更新周期: {LIVE_REFRESH_MS/1000:.0f}s")
            st.caption(f"移動平均 window: {LIVE_WINDOW_S}s")
            st.caption(f"アクティブ判定: ≤ {LIVE_ACTIVE_THRESH_S}s 以内更新")

        if active_run is None:
            with head_left:
                st.warning(
                    f"### 🛑 計測中の run が見つかりません\n"
                    f"`{CSV_NAMES['owd_dl']}` 等の CSV が直近 {LIVE_ACTIVE_THRESH_S} 秒以内に "
                    f"更新されている run が無いため、Live 表示は空です。\n\n"
                    "**計測を開始する手順:**\n"
                    "```bash\n"
                    "ssh gochiuma@192.168.4.212\n"
                    'tmux new -d -s live "cd ~/out/live_$(date +%H%M) && \\\n'
                    '  python3 -u ~/gtnlv/gtnlv_rpid.py --robot-ids 0 --duration 3600 \\\n'
                    '    --sniffer-port /dev/ttyUSB0 --pps-device /dev/pps0 --out-dir ."\n'
                    "```\n"
                    "+ AIPC で `pc_emulator` を robot-id 0 で並走。"
                )
                st.caption("過去 run の結果は他タブ (Overview / OWD / PPS / Raw CSV) で sidebar から選択して閲覧してください。")
        else:
            with head_left:
                st.success(f"### 🟢 計測中: `{active_run}`")
                legs = compute_live_legs(active_run)
                st.markdown(f"### 📡 ネットワーク模式図 (直近 {LIVE_WINDOW_S}s 移動平均)")
                st.markdown(f"<small>計測時刻: <code>{legs['now_text']}</code></small>",
                            unsafe_allow_html=True)
                fig = network_diagram(legs)
                st.plotly_chart(fig, use_container_width=True)

            # 直近 window の OWD 時系列
            st.markdown(f"### ⏱️ Total OWD 時系列 (直近 {LIVE_WINDOW_S}s)")
            if legs["total"] is None or legs["t_max"] is None:
                st.info("CSV ファイル更新中だが、データはまだ window に届いていません。")
            else:
                run_dir = run_dir_for(active_run)
                df_live = tail_csv(run_dir / CSV_NAMES["owd_dl"]) if run_dir else None
                if df_live is not None and not df_live.empty:
                    df_live = window_filter(df_live, "t_rpid_recv_unix", legs["t_max"], LIVE_WINDOW_S)
                    if not df_live.empty:
                        t = pd.to_numeric(df_live["t_rpid_recv_unix"], errors="coerce")
                        owd = (t - pd.to_numeric(df_live["corr_unix_time"], errors="coerce")) * 1e6
                        plot_df = pd.DataFrame({
                            "time": pd.to_datetime(t, unit="s"),
                            "OWD (μs)": owd,
                        }).dropna()
                        fig_ts = px.line(plot_df, x="time", y="OWD (μs)",
                                         title=f"DL OWD (raw、broadcast 戻り含む) — n={len(plot_df)}")
                        fig_ts.update_layout(height=300, margin=dict(l=20, r=20, t=40, b=20))
                        st.plotly_chart(fig_ts, use_container_width=True)

            # leg 別 KPI カード
            st.markdown("### 📐 各 leg の移動平均")
            c1, c2, c3, c4 = st.columns(4)
            def metric_card(col, name, leg):
                with col:
                    if leg is None:
                        st.metric(name, "—", help="計測 CSV 未生成 (wire_capture / sniffer 並走で取得)")
                    else:
                        st.metric(name, f"{leg['median_us']/1000:.3f} ms",
                                  delta=f"p95 {leg['p95_us']/1000:.2f}",
                                  help=f"n={leg['n']}, mean={leg.get('mean_us', float('nan'))/1000:.3f} ms")
            metric_card(c1, "leg1: AIPC→wire", legs.get("leg_aipc_wire"))
            metric_card(c2, "leg2: wire→air",  legs.get("leg_wire_air"))
            metric_card(c3, "leg3: air→HID",   legs.get("leg_air_hid"))
            metric_card(c4, "total (raw)",     legs.get("total"))

            st.caption(
                "※ wire / air leg は wire_capture.py + sniffer の並走と aipc_seq/時刻近接 join "
                "が必要 (実装中)。現状は total (= AIPC tx → RasPi 着 broadcast) のみ表示。"
            )

            # ----- パケット送信状況 -----
            st.markdown(f"### 📦 パケット送信状況 (直近 {LIVE_WINDOW_S}s)")
            traffic = compute_packet_traffic(active_run, LIVE_WINDOW_S)
            if traffic is None:
                st.info("packet traffic 集計に必要なデータがまだ揃いません。")
            else:
                t1, t2, t3, t4, t5 = st.columns(5)
                t1.metric(
                    "TX rate (実効)",
                    f"{traffic['tx_rate_hz']:.1f} Hz",
                    help="AIPC 送信 rate ≈ HID 受信 rx_dl rate (loss 0% 想定で一致)",
                )
                t2.metric(
                    "累積送信数",
                    f"{traffic['cumulative_tx']:,}",
                    help="aipc_seq の最大値 + 1 (= AIPC pc_emulator の送信総数)",
                )
                t3.metric(
                    f"DL loss ({LIVE_WINDOW_S}s)",
                    f"{traffic['loss_pct']:.3f}%",
                    delta=f"{traffic['missing']} missed" if traffic["missing"] > 0 else "all OK",
                    delta_color="inverse",
                    help=f"window 内: delivered={traffic['delivered']} / expected={traffic['expected']}",
                )
                t4.metric(
                    "air→HID rate",
                    f"{traffic['air_to_hid_rate_hz']:.1f} Hz"
                    if traffic["air_to_hid_rate_hz"] is not None else "—",
                    help="sniffer 観測の dst=HID(D0:CF:13 OUI) frame rate。"
                         "AP→HID の WiFi air 到達量",
                )
                t5.metric(
                    "HID 内取りこぼし",
                    f"{traffic['air_hid_loss_pct']:.2f}%"
                    if traffic["air_hid_loss_pct"] is not None else "—",
                    help="(air→HID 観測 − rx_dl emit) / air→HID 観測。"
                         "WiFi 着 frame は来てるのに HID 内で drop されたかの推定",
                )

    # =================== Overview ===================
    with tabs[0]:
        st.subheader(f"Run: `{run}`")

        owd_summary = compute_dl_owd_summary(df_owd, df_pps_b)
        loss = compute_loss(df_owd)
        pps_b = compute_pps_bridge_stats(df_pps_b)
        m2k = compute_m2k_dt_stats(df_m2k)
        sn = compute_sniffer_summary(df_sn, df_hb)

        # KPI cards (Year 1 challenge 7 項目)
        col1, col2, col3, col4 = st.columns(4)
        with col1:
            st.metric("①  DL OWD median (raw approx)",
                      f"{owd_summary.get('raw_p50_ms', float('nan')):.2f} ms"
                      if owd_summary else "—",
                      help="`owd_dl.csv` の `owd_dl_approx_us` median。NTP-bound 近似値。"
                           "PPS bridge 適用後の真値は sniffer_bridge.py 出力 (別タブ) で確認")
        with col2:
            st.metric("②  DL Packet Loss",
                      f"{loss['loss_pct']:.3f}%" if loss else "—",
                      help=f"aipc_seq 欠番ベース。delivered={loss['delivered']}/expected={loss['expected']}"
                           if loss else "")
        with col3:
            st.metric("③  Data Rate (HID rx)",
                      f"{loss['delivered'] * 64 * 8 / 1000:.1f} kbps" if loss else "—",
                      help="rx_dl × 64 byte payload。100Hz × 64B = 51.2 kbps が typical")
        with col4:
            st.metric("④  Interference (近傍 src)",
                      f"{sn['rssi_median_dbm']:.0f} dBm" if sn and sn["rssi_median_dbm"] is not None else "—",
                      help=f"sniffer RSSI median。frame={sn['n_frame']}, dropped={sn['dropped_total']}"
                           if sn else "")
        col5, col6, col7, _ = st.columns(4)
        with col5:
            st.metric("⑤  Startup Time",
                      "未計測",
                      help="reflector cold boot → first rx_dl の自動測定が未実装 (task #5)")
        with col6:
            st.metric("⑥  Power",
                      "未計測",
                      help="USB 電流計 or INA219 で各 chip の消費を測定 (task #6)")
        with col7:
            st.metric("⑦  Cost (Component BOM)",
                      "TBD",
                      help="主要部品: EC25-J、ADALM2000、Xikestor SKS3200M、LN6001-JP、ESP32-C5 ×2、RPi5、ASIX AX88179")

        st.markdown("---")

        # PPS Δt サブ KPI
        st.subheader("🎯 計測系 内部精度 (chip 間 TSF 同期)")
        col1, col2, col3, col4 = st.columns(4)
        if m2k:
            with col1:
                st.metric("PPS Δt median", f"{m2k['median_us']:+.1f} μs",
                          help="ADALM2000 で sniffer/HID GPIO10 PPS の立上り Δt 連続観測")
            with col2:
                st.metric("PPS Δt sd", f"{m2k['sd_us']:.1f} μs",
                          help="esp_timer dispatch jitter 由来")
            with col3:
                st.metric("PPS Δt p99", f"{m2k['p99_us']:+.1f} μs")
            with col4:
                st.metric("PPS Δt max", f"{m2k['max_us']:+.1f} μs")
        else:
            st.info("ADALM2000 PPS Δt 測定 (`dt.csv`) なし。"
                    "`tools/m2k_pps_diff/pps_diff.py` で計測可能。")

        # PPS bridge サブ KPI
        st.subheader("🌉 PPS GPIO bridge (TSF↔unix 計測精度)")
        col1, col2, col3, col4 = st.columns(4)
        if pps_b:
            with col1:
                st.metric("PPS bridge N", f"{pps_b['n']}",
                          help="1Hz PPS event で対応取れたペア数")
            with col2:
                st.metric("bridge_offset 累積 range",
                          f"{pps_b['range_ms']:.2f} ms",
                          help="run 全体での drift + jitter テール")
            with col3:
                st.metric("per-second 変動 sd",
                          f"{pps_b['adj_diff_sd_us']:.1f} μs",
                          help="esp_timer dispatch jitter (= PPS Δt sd と一致するはず)")
            with col4:
                st.metric("UART transport max",
                          f"{pps_b['uart_delay_max_ms']:.2f} ms",
                          help=f"median {pps_b['uart_delay_median_ms']:.2f} ms。"
                               "PPS GPIO bridge はこれを bypass する")
        else:
            st.info("PPS bridge データ (`pps_bridge.csv`) なし。"
                    "`gtnlv-rpid --pps-device /dev/pps0` で生成。")

        st.markdown("---")
        st.caption("詳細グラフは他タブで提供 (実装中)。")

    # =================== OWD ===================
    with tabs[1]:
        st.info("📌 OWD タブは実装中。"
                "sniffer_bridge.py の出力 (bridge_compare.csv) を読んで 4 方式比較を表示予定。")
        if df_owd is not None and not df_owd.empty and "owd_dl_approx_us" in df_owd:
            vals = pd.to_numeric(df_owd["owd_dl_approx_us"], errors="coerce").dropna()
            if len(vals):
                fig = px.histogram(vals, nbins=80, title="DL OWD approx (raw、NTP-bound)、μs")
                fig.update_layout(showlegend=False, xaxis_title="μs", yaxis_title="count")
                st.plotly_chart(fig, use_container_width=True)

    # =================== Loss ===================
    with tabs[2]:
        st.info("📌 Loss タブは実装中。aipc_seq 欠番 timeline と連続 loss 解析を予定。")
        if loss:
            st.write(loss)

    # =================== PPS Δt ===================
    with tabs[3]:
        st.info("📌 PPS Δt タブは実装中。条件別 (idle/100/250/500/750/1000Hz/混雑) 比較表とヒストグラム overlay を予定。")
        if df_m2k is not None and not df_m2k.empty and "dt_us" in df_m2k:
            dt = pd.to_numeric(df_m2k["dt_us"], errors="coerce").dropna()
            fig = px.histogram(dt, nbins=60, title="PPS Δt = HID(ch2) − sniffer(ch1)、μs")
            fig.update_layout(showlegend=False, xaxis_title="μs")
            st.plotly_chart(fig, use_container_width=True)

    # =================== Interference ===================
    with tabs[4]:
        st.info("📌 Interference タブは実装中。sniffer.csv から retry-bit / RSSI / 近隣 AP beacon 分布を予定。")
        if sn:
            st.write(sn)

    # =================== Raw CSV ===================
    with tabs[5]:
        st.subheader("Run 内 CSV 一覧")
        run_dir = run_dir_for(run)
        if run_dir is None:
            st.warning(f"run `{run}` の dir が見つかりません")
            return
        rows = []
        for p in sorted(run_dir.glob("*.csv")):
            rows.append({
                "name": p.name,
                "size_kb": round(p.stat().st_size / 1024, 1),
                "lines": sum(1 for _ in open(p, "rb")),
            })
        if rows:
            st.dataframe(pd.DataFrame(rows), use_container_width=True)
        st.markdown("---")
        kind = st.selectbox("プレビュー (head 20 行)",
                            list(CSV_NAMES.keys()), index=0)
        df_prev = load_csv(run, kind)
        if df_prev is not None:
            st.dataframe(df_prev.head(20), use_container_width=True)
        else:
            st.warning(f"{CSV_NAMES[kind]} は run 内になし")


if __name__ == "__main__":
    main()
