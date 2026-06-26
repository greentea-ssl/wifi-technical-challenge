#!/usr/bin/env python3
# sniffer_bridge.py — TSF↔unix bridge を sniffer の (tsf_us, t_rpid_recv_unix)
# ペアから min-filter で算出し、reflector の t_hid_rx_tsf に適用して
# **絶対 DL OWD** を計算する。
#
# 設計上の利点:
#   - sniffer は単一 dedicated device、複数 reflector (本番 = 複数ロボット)
#     の数が変わっても bridge source は固定
#   - sniffer は air frame ごとに tsf_us (AP TSF) を記録する (sniffer.ino で
#     esp_timer↔TSF 中点フィットを 100ms 周期で更新、cb で適用)
#   - reflector は計測対象であって bridge source ではない (独立性保持)
#
# bridge offset の意味:
#   delta_i = t_rpid_recv_i - sniffer.tsf_us_i / 1e6
#   floor = min over run of delta_i
#         = TSF↔unix の真の offset + (sniffer air RX → UART → host kernel の transport floor)
#   transport floor は数 ms 程度 (CP2102N + USB CDC) なので bridge OWD は
#   その分だけ正側にバイアスされる
#
# Usage:
#   python3 sniffer_bridge.py --sniffer sniffer.csv --owd owd_dl.csv \
#       [--window 6000] [--out bridge_owd.csv]

import argparse
import csv
import sys
from collections import deque


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sniffer", required=True, help="sniffer.csv (with tsf_us column)")
    ap.add_argument("--owd", required=True, help="owd_dl.csv (with t_hid_rx_tsf_us)")
    ap.add_argument("--window", type=int, default=6000,
                    help="rolling window size for min-filter (default 6000)")
    ap.add_argument("--pps-bridge", default=None,
                    help="optional pps_bridge.csv (from gtnlv-rpid + /dev/pps0)。指定時は "
                         "abs_owd_pps を計算して UART bridge と比較する")
    ap.add_argument("--out", default=None, help="optional CSV with per-packet absolute OWD")
    return ap.parse_args()


def load_pps_bridge_pairs(path):
    """pps_bridge.csv → sorted list of (unix_assert, tsf_us, bridge_offset_s).
    bridge_offset = unix_assert - tsf_us/1e6 (per-second sample、esp_timer
    dispatch jitter floor 23μs)。"""
    pairs = []
    with open(path) as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                ua = float(row["unix_assert"])
                tsf = int(row["tsf_us"])
                off = float(row["bridge_offset_s"])
                pairs.append((ua, tsf, off))
            except (KeyError, ValueError):
                continue
    pairs.sort(key=lambda x: x[0])
    return pairs


def load_sniffer_bridge_pairs(path):
    """Returns sorted list of (t_rpid_unix_sec, tsf_us). Skips tsf_us == 0
    (calibration not yet done at boot time)."""
    pairs = []
    skipped_no_tsf = 0
    with open(path) as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                tsf = int(row["tsf_us"])
                if tsf == 0:
                    skipped_no_tsf += 1
                    continue
                t_rpid = float(row["t_rpid_recv_unix"])
                if t_rpid > 0:
                    pairs.append((t_rpid, tsf))
            except (KeyError, ValueError):
                continue
    pairs.sort(key=lambda x: x[0])
    return pairs, skipped_no_tsf


def load_owd(path):
    """rx_dl (owd_dl.csv) を読み dedup + sort。dedup キーは cycle_count を主、
    無ければ hid_seq に fallback (issue: aipc_seq は廃止され空列のため、旧実装は
    int("") で全行 ValueError → 0 行になり絶対 OWD が無言で死んでいた)。"""
    rows = []
    seen = set()
    with open(path) as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                key = None
                for col in ("cycle_count", "hid_seq", "dl_seq"):
                    v = row.get(col)
                    if v not in (None, "", "None"):
                        key = int(v)
                        break
                if key is None:
                    continue
                # multi-robot 安全のため robot_id も含める
                rid = row.get("robot_id")
                seen_key = (rid, key)
                if seen_key in seen:
                    continue
                seen.add(seen_key)
                rows.append((
                    float(row["corr_unix_time"]),
                    int(row["t_hid_rx_tsf_us"]),
                    float(row["t_rpid_recv_unix"]),
                    key,
                ))
            except (KeyError, ValueError):
                continue
    rows.sort(key=lambda x: x[3])
    return rows


def percentile(sorted_vals, q):
    n = len(sorted_vals)
    if n == 0:
        return float("nan")
    return sorted_vals[min(int(n * q), n - 1)]


def main():
    args = parse_args()

    print(f"[i] loading sniffer: {args.sniffer}", file=sys.stderr)
    snif, skip_n = load_sniffer_bridge_pairs(args.sniffer)
    print(f"[i]   {len(snif)} sniffer pairs (skipped {skip_n} with tsf_us=0)",
          file=sys.stderr)
    if len(snif) < 100:
        print("[!] too few sniffer samples, bridge unreliable", file=sys.stderr)
        return

    print(f"[i] loading owd: {args.owd}", file=sys.stderr)
    owd = load_owd(args.owd)
    print(f"[i]   {len(owd)} owd_dl rows (dedup by cycle_count)", file=sys.stderr)

    # === bridge: delta = t_rpid_recv - sniffer.tsf_us/1e6
    deltas = [(t_rpid - tsf / 1e6, t_rpid, tsf) for t_rpid, tsf in snif]
    deltas_only = sorted(d for d, _, _ in deltas)
    n_pairs = len(deltas_only)
    global_min = deltas_only[0]

    transport_us = sorted((d - global_min) * 1e6 for d in deltas_only)

    print()
    print("=== sniffer (tsf_us, t_rpid_recv) pairs as bridge source ===")
    print(f"  N pairs              = {n_pairs}")
    print(f"  global min (delta)   = {global_min:.9f} s")
    print(f"  → bridge_offset(global) = unix when TSF=0 (plus UART floor bias)")
    print()
    print("=== transport delay (delta - global_min, μs) ===")
    print(f"  対応する経路: sniffer chip TSF (air RX moment) → cb → ring → UART → CP2102N → USB CDC → kernel")
    print(f"  min   = {transport_us[0]:>10.1f}")
    print(f"  p0.1  = {percentile(transport_us, 0.001):>10.1f}")
    print(f"  p1    = {percentile(transport_us, 0.01):>10.1f}")
    print(f"  p5    = {percentile(transport_us, 0.05):>10.1f}")
    print(f"  p50   = {percentile(transport_us, 0.5):>10.1f}")
    print(f"  p95   = {percentile(transport_us, 0.95):>10.1f}")
    print(f"  p99   = {percentile(transport_us, 0.99):>10.1f}")
    print(f"  max   = {transport_us[-1]:>10.1f}")

    # === rolling-window min filter on time-sorted deltas
    # sniffer pairs are sorted by t_rpid (host time). rolling window over
    # N samples ≈ window/sniffer_pps seconds.
    delta_seq = [d for d, _, _ in deltas]  # in chronological order
    n_seq = len(delta_seq)
    rolling_min_vals = [0.0] * n_seq
    dq = deque()
    for i in range(n_seq):
        while dq and delta_seq[dq[-1]] >= delta_seq[i]:
            dq.pop()
        dq.append(i)
        while dq and dq[0] <= i - args.window:
            dq.popleft()
        rolling_min_vals[i] = delta_seq[dq[0]]

    # Build interpolation function: for a given t_rpid query, find the closest
    # sniffer pair's index and use its rolling_min as bridge offset.
    # Simplest: linear scan with pre-sorted t_rpid times.
    snif_times = [t for t, _, _ in deltas]  # sorted

    def rolling_offset_for(t_query):
        """Find sniffer pair closest in time to t_query, return rolling_min at that index."""
        # binary search
        import bisect
        idx = bisect.bisect_left(snif_times, t_query)
        if idx >= n_seq:
            idx = n_seq - 1
        elif idx > 0 and (t_query - snif_times[idx - 1]) < (snif_times[idx] - t_query):
            idx = idx - 1
        return rolling_min_vals[idx]

    # === PPS bridge (optional)
    pps_pairs = []
    if args.pps_bridge:
        print(f"[i] loading pps bridge: {args.pps_bridge}", file=sys.stderr)
        pps_pairs = load_pps_bridge_pairs(args.pps_bridge)
        print(f"[i]   {len(pps_pairs)} PPS pairs", file=sys.stderr)
        if pps_pairs:
            # PPS bridge_offset の per-second 変動 + 累積 drift 統計
            offs = [p[2] for p in pps_pairs]
            print()
            print("=== PPS bridge_offset 統計 (per-second sample) ===")
            print(f"  median   = {percentile(sorted(offs), 0.5):.9f} s")
            print(f"  range    = {max(offs) - min(offs):.6f} s (累積 drift + jitter)")
            if len(offs) > 1:
                adj_diffs = [(offs[i+1] - offs[i]) * 1e9 for i in range(len(offs)-1)]
                ad_s = sorted(adj_diffs)
                print(f"  adj-diff ns  median = {percentile(ad_s, 0.5):.1f}  "
                      f"min = {min(adj_diffs):.1f}  max = {max(adj_diffs):.1f}")
                # SD 簡易計算
                mean = sum(adj_diffs) / len(adj_diffs)
                var = sum((x - mean) ** 2 for x in adj_diffs) / max(len(adj_diffs)-1, 1)
                import math
                sd = math.sqrt(var)
                print(f"  adj-diff sd  = {sd:.1f} ns  (esp_timer dispatch jitter)")

    import bisect
    pps_times = [p[0] for p in pps_pairs]
    pps_offsets = [p[2] for p in pps_pairs]

    def pps_offset_for(t_query):
        """PPS event timestamps の中から t_query に最も近い 2 点で線形補間。"""
        if not pps_times:
            return None
        idx = bisect.bisect_left(pps_times, t_query)
        if idx == 0:
            return pps_offsets[0]
        if idx >= len(pps_times):
            return pps_offsets[-1]
        # 線形補間 (PPS 間隔 1s に対し drift 0.01ppm = 10ns/s)
        t0, t1 = pps_times[idx-1], pps_times[idx]
        o0, o1 = pps_offsets[idx-1], pps_offsets[idx]
        if t1 == t0:
            return o0
        frac = (t_query - t0) / (t1 - t0)
        return o0 + frac * (o1 - o0)

    # === apply bridges to reflector's t_hid_rx_tsf
    abs_owd_global = []
    abs_owd_rolling = []
    abs_owd_pps = []
    raw_owd = []
    for corr, tsf, t_rpid, aseq in owd:
        # global: bridge_offset = global_min
        owd_g = (tsf / 1e6 + global_min) - corr
        abs_owd_global.append(owd_g * 1e6)

        # rolling: bridge_offset = rolling min at sniffer time closest to this packet's t_rpid
        off_r = rolling_offset_for(t_rpid)
        owd_r = (tsf / 1e6 + off_r) - corr
        abs_owd_rolling.append(owd_r * 1e6)

        # PPS: bridge_offset = per-second PPS sample 線形補間 (UART transport bypass)
        if pps_pairs:
            off_p = pps_offset_for(t_rpid)
            owd_p = (tsf / 1e6 + off_p) - corr
            abs_owd_pps.append(owd_p * 1e6)
        else:
            abs_owd_pps.append(float("nan"))

        raw_owd.append((t_rpid - corr) * 1e6)

    def stats(name, vals):
        s = sorted(vals)
        print(f"\n=== {name} (μs) ===")
        if not s:
            print("  (no data)"); return
        print(f"  N      = {len(s)}")
        print(f"  min    = {s[0]:>10.1f}")
        print(f"  median = {percentile(s, 0.5):>10.1f}")
        print(f"  mean   = {sum(s)/len(s):>10.1f}")
        print(f"  p95    = {percentile(s, 0.95):>10.1f}")
        print(f"  p99    = {percentile(s, 0.99):>10.1f}")
        print(f"  max    = {s[-1]:>10.1f}")

    stats("Absolute DL OWD via global bridge (sniffer-based)", abs_owd_global)
    stats(f"Absolute DL OWD via rolling-window (window={args.window}) bridge",
          abs_owd_rolling)
    if pps_pairs:
        valid_pps = [v for v in abs_owd_pps if not (v != v)]   # filter NaN
        stats("Absolute DL OWD via PPS GPIO bridge (UART bypass)", valid_pps)
        # global UART vs PPS の差分 = UART transport floor 系統 bias
        diffs = [abs_owd_global[i] - abs_owd_pps[i]
                 for i in range(len(abs_owd_pps))
                 if not (abs_owd_pps[i] != abs_owd_pps[i])]
        stats("Bias diff: (global UART bridge) - (PPS bridge) [= UART floor 分]", diffs)
    stats("Reference: raw OWD (t_rpid_recv - corr_unix, NTP-bound)", raw_owd)

    print()
    print("[interpretation]")
    print("  - bridge_offset_global = TSF↔unix 真の offset + (sniffer UART transport の最小 floor)")
    print("    → 絶対 OWD は UART transport floor 分だけ正側にバイアス (1-数 ms)")
    print("  - rolling は短時間ごとの local min を使うので TSF discontinuity に robust、")
    print("    ただし長い run でも各 window 内の最小が物理 floor を反映")
    print("  - PPS bridge は GPIO 割込で UART 経路 bypass、floor は esp_timer dispatch jitter")
    print("    (sd ~20 μs in §2.18.3、混雑下 sd 23 μs in §2.19) で決まる")
    print("    → 'global UART - PPS' = UART transport floor bias の実測値")
    print("  - sniffer は dedicated source なので 複数 reflector の有無に影響されない")

    if args.out:
        with open(args.out, "w", newline="") as f:
            w = csv.writer(f)
            w.writerow([
                "cycle_count", "corr_unix_time", "t_hid_rx_tsf_us",
                "t_rpid_recv_unix",
                "abs_owd_global_us", "abs_owd_rolling_us",
                "abs_owd_pps_us", "raw_owd_us",
            ])
            for i, (corr, tsf, t_rpid, aseq) in enumerate(owd):
                pps_val = f"{abs_owd_pps[i]:.1f}" if not (abs_owd_pps[i] != abs_owd_pps[i]) else ""
                w.writerow([
                    aseq, f"{corr:.6f}", tsf, f"{t_rpid:.6f}",
                    f"{abs_owd_global[i]:.1f}",
                    f"{abs_owd_rolling[i]:.1f}",
                    pps_val,
                    f"{raw_owd[i]:.1f}",
                ])
        print(f"\n[i] wrote {args.out}", file=sys.stderr)


if __name__ == "__main__":
    main()
