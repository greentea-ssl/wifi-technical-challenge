#!/usr/bin/env python3
# R12 UDP runner — receive promiscuous-mode records from XIAO over UDP and
# write them to a CSV using the same schema as the serial R12 runner.
#
# Pairs with: tools/esp_firmware/r12_udp_test/r12_udp_test.ino
# Output format identical to tools/r12_runner/run.py so the R11 analyzer can
# consume either source uniformly.

import argparse
import csv
import signal
import socket
import sys
import time
from collections import Counter
from pathlib import Path

BB_FORMAT_NAMES = {
    0: "11B", 1: "11G/A", 2: "HT", 3: "VHT",
    4: "HE_SU", 5: "HE_MU", 6: "HE_ERSU", 7: "HE_TB",
    11: "VHT_MU",
}

CSV_HEADERS = [
    "rx_seq", "t_local_us", "rx_timestamp_us",
    "bb_format", "rate", "channel", "rssi", "sig_len",
    "src", "dst", "fc_lo", "fc_hi", "hdr_seq", "dropped_total",
]


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--bind", default="0.0.0.0", help="UDP bind address (default 0.0.0.0)")
    ap.add_argument("--port", type=int, default=41250, help="UDP port (default 41250, matches sketch)")
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--out", required=True)
    return ap.parse_args()


def main():
    args = parse_args()
    out_path = Path(args.out)

    stop = {"v": False}

    def handler(sig, frame):
        stop["v"] = True

    signal.signal(signal.SIGINT, handler)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.bind((args.bind, args.port))
    sock.settimeout(1.0)
    print(f"[i] bound {args.bind}:{args.port} duration={args.duration}s out={out_path}", flush=True)

    rows = []
    with out_path.open("w", newline="", buffering=1) as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADERS)
        t_start = time.monotonic()
        last_report = t_start
        while not stop["v"] and (time.monotonic() - t_start) < args.duration:
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                continue
            # Each datagram may carry one CSV record (with trailing \n) or a
            # `# ...` info line.
            for line in data.decode("utf-8", errors="replace").splitlines():
                line = line.strip()
                if not line:
                    continue
                if line.startswith("#"):
                    print(f"[fw@{addr[0]}] {line}", flush=True)
                    continue
                if not line.startswith("R12,"):
                    continue
                parts = line.split(",")
                if len(parts) != len(CSV_HEADERS) + 1:
                    print(f"[w] parse fail (cols {len(parts)}): {line}", flush=True)
                    continue
                writer.writerow(parts[1:])
                rows.append(parts[1:])
                now = time.monotonic()
                if now - last_report >= 5.0:
                    last_report = now
                    last = parts[1:]
                    print(f"[i] frames={len(rows)} elapsed={now - t_start:.1f}s bb={last[3]} rssi={last[6]} dropped={last[13]}", flush=True)

    sock.close()
    print(f"[i] collected {len(rows)} frames → {out_path}", flush=True)
    if len(rows) < 2:
        return

    bb = Counter()
    rx_ts = []
    src_set = Counter()
    rssi_list = []
    dropped_final = 0
    for r in rows:
        try:
            bb[int(r[3])] += 1
            rx_ts.append(int(r[2]))
            src_set[r[8]] += 1
            rssi_list.append(int(r[6]))
            dropped_final = int(r[13])
        except (ValueError, IndexError):
            continue

    print()
    print("=== bb_format histogram ===")
    for code, n in bb.most_common():
        print(f"  {code} ({BB_FORMAT_NAMES.get(code, '?')}): {n}")
    print()
    print("=== Source MAC (BSSID) histogram (top 3) ===")
    for mac, n in src_set.most_common(3):
        print(f"  {mac}: {n}")
    print()
    nonmono = sum(1 for i in range(1, len(rx_ts))
                  if rx_ts[i] - rx_ts[i - 1] < 0 and rx_ts[i] - rx_ts[i - 1] > -2_000_000_000)
    dup = sum(1 for i in range(1, len(rx_ts)) if rx_ts[i] == rx_ts[i - 1])
    print("=== rx_timestamp ===")
    print(f"  n_frames     = {len(rx_ts)}")
    print(f"  duplicates   = {dup}")
    print(f"  non-monotonic= {nonmono}")
    print(f"  fw dropped   = {dropped_final}")
    if rssi_list:
        rs = sorted(rssi_list)
        print(f"  RSSI min/median/max = {rs[0]}/{rs[len(rs)//2]}/{rs[-1]} dBm")
    he_total = sum(n for c, n in bb.items() if 4 <= c <= 7)
    print()
    print(f"[{'PASS' if he_total > 0 else 'FAIL'}] HE frames: {he_total}")


if __name__ == "__main__":
    main()
