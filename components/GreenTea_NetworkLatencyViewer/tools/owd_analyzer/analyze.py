#!/usr/bin/env python3
# owd_analyzer — derive true OWD distribution from gtnlv-rpid v0 outputs.
#
# Input:
#   - phase1_results/owd_dl.csv  (rx_dl records: corr_unix_time, t_hid_rx_tsf_us, ...)
#
# Method:
#   For each rx_dl pair (corr_unix_time, t_hid_rx_tsf_us):
#     pair_delay_us = t_hid_rx_tsf_us - corr_unix_time * 1e6
#   This is "TSF axis time at HID receive" minus "unix axis time at AIPC send",
#   expressed in microseconds. The two axes have unknown offset; subtracting
#   the minimum over the run gives "delay above the floor", which is what we
#   want for OWD variance characterization:
#
#     owd_dl_relative_us = pair_delay_us - rolling_min(pair_delay_us)
#
#   The rolling_min represents the floor = (TSF↔unix offset) + (minimum true
#   AIPC→HID delay). Subtracting it removes both the unknown clock offset and
#   the minimum delay, leaving only the per-packet AP queue + air jitter
#   excess. mean/var/max of this is the meaningful metric.
#
#   For absolute OWD reporting (challenge submission), add an estimated
#   minimum: MIN_OWD_FLOOR_US ~= 500-1000us (typical AP+air for unloaded
#   WiFi 6). This is a project-wide constant we calibrate separately.
#
# Output:
#   - phase1_results/owd_dl_relative.csv (per-packet relative OWD)
#   - stdout: summary (mean/median/p95/p99/max, packet count, loss estimate)

import argparse
import csv
import math
import statistics
import sys
from pathlib import Path


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--in-dir", default="phase1_results",
                    help="directory containing owd_dl.csv / tx_ul.csv / uplink_arrivals.csv")
    ap.add_argument("--out-dir", default=None,
                    help="output dir (default = in-dir)")
    ap.add_argument("--min-floor-us", type=float, default=0.0,
                    help="estimated floor of true OWD (uncongested) for absolute reporting")
    ap.add_argument("--warmup-skip", type=int, default=10,
                    help="skip first N samples (TSF calibration convergence)")
    ap.add_argument("--ul-pair-window-ms", type=float, default=5.0,
                    help="time-proximity window for tx_ul ↔ uplink_arrival pairing")
    return ap.parse_args()


def _pct(svals, q):
    """nearest-rank パーセンタイル (定義統一、issue #5.1)。svals はソート済。
    rank = ceil(q*n)、1始まりを 0始まり index に。clamp で範囲外を防ぐ。"""
    n = len(svals)
    if n == 0:
        return 0.0
    idx = math.ceil(q * n) - 1
    return svals[min(max(idx, 0), n - 1)]


def summarize(label, vals):
    """Print mean/median/p95/p99/max of a list of microsecond values."""
    if not vals:
        print(f"=== {label}: no data ===")
        return
    n = len(vals)
    svals = sorted(vals)
    mean = sum(svals) / n
    # 標本標準偏差 (n-1、issue #5.3)。母標準偏差(/n)は小標本で過小。
    stdev = math.sqrt(sum((v - mean) ** 2 for v in svals) / (n - 1)) if n > 1 else 0.0
    median = statistics.median(svals)
    p95 = _pct(svals, 0.95)
    p99 = _pct(svals, 0.99)
    p999 = _pct(svals, 0.999)
    print(f"=== {label} ===")
    print(f"  N      = {n}")
    print(f"  mean   = {mean:>10.1f} us")
    print(f"  median = {median:>10.1f} us")
    print(f"  stdev  = {stdev:>10.1f} us")
    print(f"  p95    = {p95:>10.1f} us")
    print(f"  p99    = {p99:>10.1f} us")
    print(f"  p99.9  = {p999:>10.1f} us")
    print(f"  max    = {svals[-1]:>10.1f} us")


def gap_loss(seqs, modulus=None, reorder_tol=1000):
    """到着順 seq 列から欠番数を数える。連番の wrap-around・途中リセット
    (HID 再起動/再associate、set_ssid 切替)・**順序入替** を考慮する (issue #2, #6)。

    seqs は **到着順** (時系列) を仮定。後退 (cur < prev) を 3 通りに区別する:
      - **clean wrap** (modulus 既知で prev≈最大値・cur≈0) → 同一区間として +modulus でアンラップ
      - **reset** (大きな後退 cur < prev - reorder_tol = 新ベースライン) → 区間を分割
      - **順序入替** (小さな後退 prev - reorder_tol <= cur < prev) → 現区間に残す
        (区間内集計は sorted(set) なので入替は欠番に化けない)
    reset/wrap を「値域の大きなジャンプ」で判定することで、WiFi で日常的に起きる
    順序入替を reset と誤判定して損失を二重計上する退行 (issue #6) を防ぐ。
    返り値: (missing, expected, n_received) は全区間の合計。"""
    if not seqs:
        return 0, 0, 0
    segments = [[seqs[0]]]
    offset = 0
    for prev, cur in zip(seqs, seqs[1:]):
        if modulus and prev > modulus - reorder_tol and cur < reorder_tol:
            offset += modulus                      # clean wrap → 同一区間継続
            segments[-1].append(cur + offset)
        elif cur < prev - reorder_tol:
            offset = 0                             # 大きな後退 = reset → 新区間
            segments.append([cur])
        else:
            segments[-1].append(cur + offset)      # 前進 or 小さな後退(順序入替)
    missing = expected = n_received = 0
    for seg in segments:
        u = sorted(set(seg))
        exp = u[-1] - u[0] + 1
        rec = len(u)
        missing += exp - rec
        expected += exp
        n_received += rec
    return missing, expected, n_received


def analyze_downlink(in_dir: Path, out_dir: Path, warmup_skip: int, min_floor_us: float):
    in_path = in_dir / "owd_dl.csv"
    if not in_path.exists():
        print(f"[w] no downlink data: {in_path} missing", file=sys.stderr)
        return

    rows = []
    with in_path.open() as f:
        reader = csv.DictReader(f)
        for r in reader:
            try:
                row = {
                    "corr_unix_time": float(r["corr_unix_time"]),
                    "t_hid_rx_tsf_us": int(r["t_hid_rx_tsf_us"]),
                    "t_rpid_recv_unix": float(r["t_rpid_recv_unix"]),
                    "hid_seq": int(r["hid_seq"]),
                    "hid_ip": r["hid_ip"],
                    "robot_id": r["robot_id"],
                    "owd_dl_approx_us": float(r["owd_dl_approx_us"]),
                }
                # Optional new fields (post-loss-tracking version)
                if r.get("aipc_seq") and r["aipc_seq"] not in ("", "None"):
                    row["aipc_seq"] = int(r["aipc_seq"])
                if r.get("dl_seq") and r["dl_seq"] not in ("", "None"):
                    row["dl_seq"] = int(r["dl_seq"])
                rows.append(row)
            except (KeyError, ValueError):
                continue

    if not rows:
        print(f"[w] no rx_dl rows", file=sys.stderr)
        return

    print(f"[i] DL: loaded {len(rows)} rx_dl rows", file=sys.stderr)
    skip = min(warmup_skip, len(rows) - 1) if len(rows) > warmup_skip else 0
    work = rows[skip:]
    print(f"[i] DL: processing {len(work)} rows after skip={skip}", file=sys.stderr)

    # Dedup by (hid_seq, aipc_seq) — broadcast can be received on multiple
    # interfaces (wlan0 + USB-Eth on RasPi), inflating N. Keep first arrival.
    _seen = set()
    deduped = []
    for r in work:
        key = (r.get("aipc_seq"), r["hid_seq"])
        if key in _seen:
            continue
        _seen.add(key)
        deduped.append(r)
    if len(deduped) < len(work):
        print(f"[i] DL: deduped {len(work)-len(deduped)} broadcast duplicates "
              f"(kept {len(deduped)}/{len(work)})", file=sys.stderr)
    work = deduped

    for r in work:
        r["pair_delay_us"] = r["t_hid_rx_tsf_us"] - r["corr_unix_time"] * 1e6

    # Rolling-window min-filter floor (window = 60s of samples ≈ 6000 at 100Hz).
    # Global min() is fragile to TSF discontinuities (re-associate / beacon resync)
    # which can shift the floor by 100s of ms. Rolling min keeps floor local.
    WINDOW = 6000
    n = len(work)
    if n <= WINDOW:
        floor_us = min(r["pair_delay_us"] for r in work)
        for r in work:
            r["owd_dl_relative_us"] = r["pair_delay_us"] - floor_us
    else:
        from collections import deque
        # left-aligned rolling window (each row's floor = min over next WINDOW samples)
        dq = deque()
        # First pass: precompute rolling min array
        rolling_min = [0.0] * n
        for i in range(n):
            v = work[i]["pair_delay_us"]
            while dq and work[dq[-1]]["pair_delay_us"] >= v:
                dq.pop()
            dq.append(i)
            while dq and dq[0] < i - WINDOW + 1:
                dq.popleft()
            rolling_min[i] = work[dq[0]]["pair_delay_us"]
        for i, r in enumerate(work):
            r["owd_dl_relative_us"] = r["pair_delay_us"] - rolling_min[i]

    out_path = out_dir / "owd_dl_relative.csv"
    with out_path.open("w", newline="", buffering=1) as f:
        w = csv.DictWriter(f, fieldnames=[
            "robot_id", "hid_ip", "hid_seq",
            "corr_unix_time", "t_hid_rx_tsf_us",
            "pair_delay_us", "owd_dl_relative_us"])
        w.writeheader()
        for r in work:
            w.writerow({
                "robot_id": r["robot_id"], "hid_ip": r["hid_ip"], "hid_seq": r["hid_seq"],
                "corr_unix_time": f"{r['corr_unix_time']:.6f}",
                "t_hid_rx_tsf_us": r["t_hid_rx_tsf_us"],
                "pair_delay_us": f"{r['pair_delay_us']:.1f}",
                "owd_dl_relative_us": f"{r['owd_dl_relative_us']:.1f}",
            })
    print(f"[i] DL: wrote {out_path}", file=sys.stderr)

    # Primary metric: raw approx OWD (t_rpid_recv - corr_unix_time).
    # Includes AIPC→AP→HID OWD + HID→RasPi broadcast trip, NTP-bound clock sync.
    # This is the recommended number for challenge reporting.
    print()
    summarize("Downlink OWD (raw: t_rpid_recv − t_aipc_send)",
              [r["owd_dl_approx_us"] for r in work])

    print()
    summarize("Downlink OWD (TSF-bridge, rolling-window relative)",
              [r["owd_dl_relative_us"] for r in work])
    if min_floor_us > 0:
        print()
        summarize(f"Downlink OWD (absolute, floor={min_floor_us:.0f}us)",
                  [r["owd_dl_relative_us"] + min_floor_us for r in work])

    # Capture/loss
    if len(work) >= 2:
        elapsed = work[-1]["corr_unix_time"] - work[0]["corr_unix_time"]
        if elapsed > 0:
            rate = (len(work) - 1) / elapsed
            print()
            print(f"=== DL rate (received) === {rate:.1f} Hz over {elapsed:.1f}s")

    # Packet loss via aipc_seq (if present)
    aipc_seqs = [r.get("aipc_seq") for r in work if "aipc_seq" in r]
    if aipc_seqs:
        missing, expected, received = gap_loss(aipc_seqs, modulus=2**32)
        loss_pct = (missing / expected * 100.0) if expected > 0 else 0.0
        print()
        print(f"=== DL packet loss (via aipc_seq) ===")
        print(f"  expected (max-min+1) = {expected}")
        print(f"  received (unique)    = {received}")
        print(f"  missing              = {missing}")
        print(f"  loss rate            = {loss_pct:.3f} %")
    # Also via dl_seq (HID-internal counter for rx_dl emissions)
    dl_seqs = [r.get("dl_seq") for r in work if "dl_seq" in r]
    if dl_seqs:
        missing, expected, received = gap_loss(dl_seqs, modulus=2**32)
        loss_pct = (missing / expected * 100.0) if expected > 0 else 0.0
        print()
        print(f"=== rx_dl broadcast monitoring loss (via dl_seq) ===")
        print(f"  expected (max-min+1) = {expected}")
        print(f"  received (unique)    = {received}")
        print(f"  missing              = {missing}")
        print(f"  loss rate            = {loss_pct:.3f} %  (rx_dl broadcast → rpid host)")


def analyze_uplink(in_dir: Path, out_dir: Path, window_ms: float, min_floor_us: float):
    tx_path = in_dir / "tx_ul.csv"
    rx_path = in_dir / "uplink_arrivals.csv"
    if not tx_path.exists() or not rx_path.exists():
        print(f"[w] no uplink data (tx_ul.csv / uplink_arrivals.csv missing)", file=sys.stderr)
        return

    tx_rows = []
    with tx_path.open() as f:
        for r in csv.DictReader(f):
            try:
                rec = {
                    "robot_id": r["robot_id"],
                    "hid_ip": r["hid_ip"],
                    "hid_seq": int(r["hid_seq"]),
                    "tx_port": int(r["tx_port"]),
                    "t_hid_tx_tsf_us": int(r["t_hid_tx_tsf_us"]),
                    "t_rpid_recv_unix": float(r["t_rpid_recv_unix"]),
                    "frame_size": int(r["frame_size"]),
                }
                if r.get("ul_seq") and r["ul_seq"] not in ("", "None"):
                    rec["ul_seq"] = int(r["ul_seq"])
                tx_rows.append(rec)
            except (KeyError, ValueError):
                continue

    rx_rows = []
    with rx_path.open() as f:
        for r in csv.DictReader(f):
            try:
                rx_rows.append({
                    "robot_id": r["robot_id"],
                    "hid_ip": r["hid_ip"],
                    "t_rpid_recv_unix": float(r["t_rpid_recv_unix"]),
                    "size_bytes": int(r["size_bytes"]),
                })
            except (KeyError, ValueError):
                continue

    print(f"[i] UL: tx_ul={len(tx_rows)} rx_uplink={len(rx_rows)}", file=sys.stderr)
    if not tx_rows or not rx_rows:
        return

    # Sort both by t_rpid_recv_unix
    tx_rows.sort(key=lambda r: r["t_rpid_recv_unix"])
    rx_rows.sort(key=lambda r: r["t_rpid_recv_unix"])

    # Time-proximity pairing: for each tx_ul, find nearest rx_arrival in time within window
    window_s = window_ms / 1000.0
    pairs = []
    used = [False] * len(rx_rows)   # rx を 1 対 1 で consume (issue #5.2: 多対一防止)
    j = 0  # rx index pointer (lower bound、未使用 rx の最小 index)
    for tx in tx_rows:
        # advance j to first rx within (tx.t - window, tx.t + window)
        while j < len(rx_rows) and rx_rows[j]["t_rpid_recv_unix"] < tx["t_rpid_recv_unix"] - window_s:
            j += 1
        # search nearest UNUSED rx from current j up to first that's after window
        best = None
        best_dt = None
        best_idx = -1
        k = j
        while k < len(rx_rows) and rx_rows[k]["t_rpid_recv_unix"] <= tx["t_rpid_recv_unix"] + window_s:
            # same hid_ip (multi-robot safe) かつ未使用のみ
            if not used[k] and rx_rows[k]["hid_ip"] == tx["hid_ip"]:
                dt = abs(rx_rows[k]["t_rpid_recv_unix"] - tx["t_rpid_recv_unix"])
                if best_dt is None or dt < best_dt:
                    best, best_dt, best_idx = rx_rows[k], dt, k
            k += 1
        if best is None:
            continue
        used[best_idx] = True   # この rx を消費 → 他 tx が再利用不可
        pair_delay_us = best["t_rpid_recv_unix"] * 1e6 - tx["t_hid_tx_tsf_us"]
        pairs.append({
            "robot_id": tx["robot_id"], "hid_ip": tx["hid_ip"], "hid_seq": tx["hid_seq"],
            "tx_port": tx["tx_port"],
            "t_hid_tx_tsf_us": tx["t_hid_tx_tsf_us"],
            "t_rpid_recv_unix": best["t_rpid_recv_unix"],
            "pair_window_dt_ms": best_dt * 1000.0,
            "pair_delay_us": pair_delay_us,
        })

    print(f"[i] UL: matched {len(pairs)} pairs (out of {len(tx_rows)} tx_ul)", file=sys.stderr)
    if not pairs:
        return

    floor_us = min(p["pair_delay_us"] for p in pairs)
    for p in pairs:
        p["owd_ul_relative_us"] = p["pair_delay_us"] - floor_us

    out_path = out_dir / "owd_ul_relative.csv"
    with out_path.open("w", newline="", buffering=1) as f:
        w = csv.DictWriter(f, fieldnames=[
            "robot_id", "hid_ip", "hid_seq", "tx_port",
            "t_hid_tx_tsf_us", "t_rpid_recv_unix", "pair_window_dt_ms",
            "pair_delay_us", "owd_ul_relative_us"])
        w.writeheader()
        for p in pairs:
            w.writerow({
                "robot_id": p["robot_id"], "hid_ip": p["hid_ip"], "hid_seq": p["hid_seq"],
                "tx_port": p["tx_port"], "t_hid_tx_tsf_us": p["t_hid_tx_tsf_us"],
                "t_rpid_recv_unix": f"{p['t_rpid_recv_unix']:.6f}",
                "pair_window_dt_ms": f"{p['pair_window_dt_ms']:.3f}",
                "pair_delay_us": f"{p['pair_delay_us']:.1f}",
                "owd_ul_relative_us": f"{p['owd_ul_relative_us']:.1f}",
            })
    print(f"[i] UL: wrote {out_path}", file=sys.stderr)

    print()
    summarize("Uplink OWD (relative to observed floor)",
              [p["owd_ul_relative_us"] for p in pairs])
    if min_floor_us > 0:
        print()
        summarize(f"Uplink OWD (absolute, floor={min_floor_us:.0f}us)",
                  [p["owd_ul_relative_us"] + min_floor_us for p in pairs])

    # Uplink loss via ul_seq (HID-internal counter for tx_ul emissions)
    ul_seqs = [r.get("ul_seq") for r in tx_rows if "ul_seq" in r]
    if ul_seqs:
        missing, expected, received = gap_loss(ul_seqs, modulus=2**32)
        loss_pct = (missing / expected * 100.0) if expected > 0 else 0.0
        print()
        print(f"=== tx_ul broadcast monitoring loss (via ul_seq) ===")
        print(f"  expected (max-min+1) = {expected}")
        print(f"  received (unique)    = {received}")
        print(f"  missing              = {missing}")
        print(f"  loss rate            = {loss_pct:.3f} %  (tx_ul broadcast → rpid host)")

    # Production uplink pairing rate (proxy for uplink WiFi loss)
    pair_rate = len(pairs) / len(tx_rows) * 100 if tx_rows else 0
    print()
    print(f"=== production uplink ↔ tx_ul pairing ===")
    print(f"  pair rate           = {pair_rate:.3f} %")
    print(f"  unpaired tx_ul       = {len(tx_rows) - len(pairs)}")


def main():
    args = parse_args()
    in_dir = Path(args.in_dir)
    out_dir = Path(args.out_dir) if args.out_dir else in_dir
    out_dir.mkdir(parents=True, exist_ok=True)
    if not in_dir.exists():
        sys.exit(f"[err] {in_dir} not found")
    analyze_downlink(in_dir, out_dir, args.warmup_skip, args.min_floor_us)
    print()
    analyze_uplink(in_dir, out_dir, args.ul_pair_window_ms, args.min_floor_us)

if __name__ == "__main__":
    main()
