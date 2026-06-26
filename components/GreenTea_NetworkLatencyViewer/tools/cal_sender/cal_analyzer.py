#!/usr/bin/env python3
# cal_analyzer — pair cal_sender send log with sniffer cal_recv log,
# compute per-group (broadcast vs unicast) delay distribution.
#
# Delay metric for each cal packet:
#     pair_delay_us = t_recv_local_us - cal_send_rt * 1e6
#
# t_recv_local_us is sniffer's esp_timer (unknown offset vs unix epoch).
# Subtracting cal_send_rt yields an offset-shifted "delay". The OFFSET IS
# THE SAME for unicast and broadcast (same sniffer chip, same time period),
# so:
#   - mean(uc) - mean(bc)  = absolute AP processing difference (offset cancels)
#   - stdev(uc), stdev(bc) = group-internal jitter, absolute
#   - p95/p99 - min in group = absolute "tail above group floor"
#
# Usage:
#   python3 cal_analyzer.py \
#       --send-log phase1_results_cal/send_log.csv \
#       --recv-log phase1_results_cal/sniffer_cal.csv

import argparse
import csv
import math
import statistics
import sys
from pathlib import Path


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--send-log", required=True, help="cal_sender send_log.csv")
    ap.add_argument("--recv-log", required=True, help="sniffer runner cal CSV (sniffer_cal.csv)")
    return ap.parse_args()


def summary(label, vals):
    if not vals:
        print(f"=== {label}: no data ==="); return
    n = len(vals)
    s = sorted(vals)
    mean = sum(s) / n
    stdev = math.sqrt(sum((v - mean) ** 2 for v in s) / n) if n > 1 else 0.0
    median = statistics.median(s)
    p95 = s[min(int(n * 0.95), n - 1)]
    p99 = s[min(int(n * 0.99), n - 1)]
    print(f"=== {label}  (N={n}) ===")
    print(f"  min    = {s[0]:>14.1f} us")
    print(f"  median = {median:>14.1f} us")
    print(f"  mean   = {mean:>14.1f} us")
    print(f"  stdev  = {stdev:>14.1f} us  (offset-free; pure jitter)")
    print(f"  p95    = {s[-int(n*0.05) - 1]:>14.1f} us")
    print(f"  p99    = {p99:>14.1f} us")
    print(f"  max    = {s[-1]:>14.1f} us")
    print(f"  median−min = {median - s[0]:>14.1f} us  (absolute tail above floor)")
    print(f"  p95−min    = {p95 - s[0]:>14.1f} us")
    print(f"  max−min    = {s[-1] - s[0]:>14.1f} us")


def main():
    args = parse_args()
    sends = {}
    with Path(args.send_log).open() as f:
        for r in csv.DictReader(f):
            try:
                sends[int(r["cal_seq"])] = {
                    "type_flag": int(r["type_flag"]),
                    "send_rt": float(r["send_rt_unix"]),
                    "target": r["target_ip"],
                }
            except (KeyError, ValueError):
                continue
    print(f"[i] loaded {len(sends)} send records", file=sys.stderr)

    bc_delays = []
    uc_delays = []
    n_recv = 0
    unmatched = 0
    with Path(args.recv_log).open() as f:
        for r in csv.DictReader(f):
            try:
                cal_seq = int(r["cal_seq"])
                type_flag = int(r["type_flag"])
                t_recv = int(r["t_recv_local_us"])
                send_rt = float(r["cal_send_rt_unix"])
            except (KeyError, ValueError):
                continue
            n_recv += 1
            # cross-check type_flag against send log
            send = sends.get(cal_seq)
            if send is None:
                unmatched += 1
            else:
                if send["type_flag"] != type_flag:
                    print(f"[w] cal_seq={cal_seq} type mismatch send={send['type_flag']} recv={type_flag}",
                          file=sys.stderr)
            pair_delay_us = t_recv - send_rt * 1e6
            if type_flag == 0:
                bc_delays.append(pair_delay_us)
            else:
                uc_delays.append(pair_delay_us)

    print(f"[i] loaded {n_recv} recv records (unmatched-to-send-log: {unmatched})", file=sys.stderr)
    print(f"[i] broadcast={len(bc_delays)}  unicast={len(uc_delays)}", file=sys.stderr)
    print()

    summary("BROADCAST (RasPi→sniffer via 192.168.x.255)", bc_delays)
    print()
    summary("UNICAST   (RasPi→sniffer IP)", uc_delays)
    print()

    if bc_delays and uc_delays:
        # Difference of means: absolute (offset cancels)
        mean_bc = sum(bc_delays) / len(bc_delays)
        mean_uc = sum(uc_delays) / len(uc_delays)
        median_bc = statistics.median(bc_delays)
        median_uc = statistics.median(uc_delays)
        min_bc = min(bc_delays)
        min_uc = min(uc_delays)
        print("=== ABSOLUTE differences (offset cancels) ===")
        print(f"  mean(uc) - mean(bc)     = {mean_uc - mean_bc:>10.1f} us")
        print(f"  median(uc) - median(bc) = {median_uc - median_bc:>10.1f} us")
        print(f"  min(uc) - min(bc)       = {min_uc - min_bc:>10.1f} us")
        print()

        # Sent vs received
        sent_bc = sum(1 for s in sends.values() if s["type_flag"] == 0)
        sent_uc = sum(1 for s in sends.values() if s["type_flag"] == 1)
        loss_bc = (sent_bc - len(bc_delays)) / sent_bc * 100 if sent_bc else 0
        loss_uc = (sent_uc - len(uc_delays)) / sent_uc * 100 if sent_uc else 0
        print("=== Sent vs received ===")
        print(f"  broadcast: sent={sent_bc}  recv={len(bc_delays)}  loss={loss_bc:.2f}%")
        print(f"  unicast  : sent={sent_uc}  recv={len(uc_delays)}  loss={loss_uc:.2f}%")


if __name__ == "__main__":
    main()
