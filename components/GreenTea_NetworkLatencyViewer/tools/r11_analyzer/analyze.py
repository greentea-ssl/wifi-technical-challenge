#!/usr/bin/env python3
# R11 analyzer — join two R12 captures (e.g., XIAO via UDP, devkit via serial),
# match same physical frame by (src_mac, hdr_seq), and characterize the
# rx_timestamp delta distribution. Per phase0_runbook §1.3, this is what
# constrains how well sniffer↔robot processing delay can be calibrated.
#
# 802.11 sequence numbers are 12 bit (0..4095) and wrap; we resolve wrap by
# pairing each devkit frame with the temporally-nearest XIAO frame that has
# the same (src_mac, hdr_seq).

import argparse
import csv
import math
import statistics
import sys
from collections import defaultdict
from pathlib import Path


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--a", required=True, help="CSV from sniffer A (e.g., XIAO UDP)")
    ap.add_argument("--b", required=True, help="CSV from sniffer B (e.g., devkit serial)")
    ap.add_argument("--label-a", default="A")
    ap.add_argument("--label-b", default="B")
    ap.add_argument("--src", default=None, help="Restrict to this src_mac (optional, default: most common src in A)")
    ap.add_argument("--window-us", type=int, default=200_000,
                    help="Max |t_local_a - t_local_b| to call two records the same frame (μs)")
    return ap.parse_args()


def load(path):
    rows = []
    with open(path) as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                rows.append({
                    "rx_seq": int(row["rx_seq"]),
                    "t_local_us": int(row["t_local_us"]),
                    "rx_timestamp_us": int(row["rx_timestamp_us"]),
                    "bb_format": int(row["bb_format"]),
                    "channel": int(row["channel"]),
                    "rssi": int(row["rssi"]),
                    "sig_len": int(row["sig_len"]),
                    "src": row["src"],
                    "dst": row["dst"],
                    "hdr_seq": int(row["hdr_seq"]),
                })
            except (KeyError, ValueError):
                continue
    return rows


def pick_src(rows):
    from collections import Counter
    c = Counter(r["src"] for r in rows)
    if not c:
        return None
    return c.most_common(1)[0][0]


def main():
    args = parse_args()
    a_rows = load(args.a)
    b_rows = load(args.b)
    print(f"[i] {args.label_a}={args.a}  rows={len(a_rows)}")
    print(f"[i] {args.label_b}={args.b}  rows={len(b_rows)}")
    if not a_rows or not b_rows:
        sys.exit("[err] empty input")

    src = args.src or pick_src(a_rows)
    print(f"[i] joining on src_mac={src}")
    a_rows = [r for r in a_rows if r["src"] == src]
    b_rows = [r for r in b_rows if r["src"] == src]
    print(f"[i] after src filter: {args.label_a}={len(a_rows)}  {args.label_b}={len(b_rows)}")

    # Two boards have independent local-time references for rx_timestamp_us,
    # so the absolute delta is some unknown large offset + a small noise term
    # we want to characterize. Match purely on (src_mac, hdr_seq) for now; if
    # a hdr_seq repeats (12-bit wraps every ~3 min at ~22fps), we use the
    # devkit record whose esp_timer (t_local_us) is closest to the XIAO one
    # after removing each board's per-capture t_local baseline.
    a_t0 = a_rows[0]["t_local_us"]
    b_t0 = b_rows[0]["t_local_us"]

    b_by_seq = defaultdict(list)
    for r in b_rows:
        b_by_seq[r["hdr_seq"]].append(r)

    pairs = []
    unmatched_a = 0
    for ra in a_rows:
        candidates = b_by_seq.get(ra["hdr_seq"], [])
        if not candidates:
            unmatched_a += 1
            continue
        if len(candidates) == 1:
            best = candidates[0]
        else:
            # Pick the candidate whose normalized t_local (relative to its
            # capture start) is closest to ra's.
            a_rel = ra["t_local_us"] - a_t0
            best = min(candidates,
                       key=lambda rb: abs((rb["t_local_us"] - b_t0) - a_rel))
        dt = ra["rx_timestamp_us"] - best["rx_timestamp_us"]
        if dt > 2**31:
            dt -= 2**32
        elif dt < -2**31:
            dt += 2**32
        pairs.append((ra, best, dt))

    print(f"[i] matched {len(pairs)} pairs   unmatched A: {unmatched_a}   B unused: {len(b_rows) - len(set(id(p[1]) for p in pairs))}")
    if not pairs:
        sys.exit("[err] no matches — check src_mac / window-us")

    deltas = [p[2] for p in pairs]
    deltas_sorted = sorted(deltas)
    n = len(deltas)
    mean = sum(deltas) / n
    median = deltas_sorted[n // 2]
    p05 = deltas_sorted[int(n * 0.05)]
    p95 = deltas_sorted[int(n * 0.95)]
    stdev = math.sqrt(sum((d - mean) ** 2 for d in deltas) / n)

    print()
    print("=== rx_timestamp delta (μs)  [A - B] ===")
    print(f"  n              = {n}")
    print(f"  mean (raw)     = {mean:+.2f}   [absolute offset; not meaningful — each board's rx_timestamp counter started at different times]")
    print(f"  stdev (raw)    = {stdev:.2f}   [includes free-running clock drift between A and B]")
    print(f"  p05 / p95      = {p05:+d} / {p95:+d}")
    print(f"  min / max      = {min(deltas):+d} / {max(deltas):+d}")

    pass_drift = pass_resid = False
    if n >= 32:
        ta = [p[0]["t_local_us"] for p in pairs]
        mta = sum(ta) / n
        md = sum(deltas) / n
        sxx = sum((t - mta) ** 2 for t in ta)
        sxy = sum((ta[i] - mta) * (deltas[i] - md) for i in range(n))
        slope = sxy / sxx if sxx > 0 else 0.0
        intercept = md - slope * mta
        slope_us_per_s = slope * 1e6
        resid = [deltas[i] - (slope * ta[i] + intercept) for i in range(n)]
        abs_r = sorted(abs(r) for r in resid)
        rms = math.sqrt(sum(r * r for r in resid) / n)
        print()
        print("=== After removing linear drift (this is what matters in practice — ===")
        print("=== drift cancels once both boards project to the same AP TSF)      ===")
        print(f"  drift slope    = {slope_us_per_s:+.4f} μs/s  ({slope_us_per_s:+.3f} ppm)")
        print(f"  residual RMS   = {rms:.2f} μs")
        print(f"  |resid| p50    = {abs_r[n // 2]:.1f} μs")
        print(f"  |resid| p95    = {abs_r[int(n * 0.95)]:.1f} μs")
        print(f"  |resid| p99    = {abs_r[int(n * 0.99)]:.1f} μs")
        print(f"  |resid| max    = {abs_r[-1]:.1f} μs")
        pass_drift = abs(slope_us_per_s) < 50      # 50 ppm between two random crystals is normal
        pass_resid = abs_r[int(n * 0.99)] < 10     # the residual noise we can't calibrate out

    print()
    print(f"R11 (phase0_runbook §1.3) — sniffer / robot processing-delay symmetry:")
    print(f"  drift < 50 ppm        : {'PASS' if pass_drift else 'FAIL'}  (expected: two free-running crystals differ ~±20ppm)")
    print(f"  |resid| p99 < 10 us   : {'PASS' if pass_resid else 'FAIL'}  (this is the noise we cannot calibrate out)")


if __name__ == "__main__":
    main()
