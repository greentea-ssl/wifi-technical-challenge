#!/usr/bin/env python3
# R12 host runner — capture promiscuous-mode output and check that
# rx_timestamp behaves (architecture.md R12, lessons_learned §3.5):
#   * monotonic across consecutive frames from the same BSSID
#   * no duplicates / no 0-run bug (espressif/esp-idf#2468)
#   * bb_format includes HE (4..7) when AP is in 11ax mode
#
# Usage:
#   python3 run.py --port /dev/ttyACM1 --duration 60 --out phase0_results/r12_xiao.csv
#
# Pairs with: tools/esp_firmware/r12_promisc_test/r12_promisc_test.ino

import argparse
import csv
import signal
import sys
import time
from collections import Counter
from pathlib import Path

try:
    import serial
except ImportError:
    sys.stderr.write("pyserial not installed; pip install pyserial\n")
    sys.exit(2)

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
    ap.add_argument("--port", required=True)
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--out", required=True, help="Output CSV path")
    return ap.parse_args()


def main():
    args = parse_args()
    out_path = Path(args.out)

    stop = {"v": False}

    def handler(sig, frame):
        stop["v"] = True

    signal.signal(signal.SIGINT, handler)

    print(f"[i] port={args.port} duration={args.duration}s out={out_path}", flush=True)
    rows = []
    with serial.Serial(args.port, args.baud, timeout=1) as ser, out_path.open("w", newline="", buffering=1) as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADERS)
        t_start = time.monotonic()
        last_report = t_start
        while not stop["v"] and (time.monotonic() - t_start) < args.duration:
            line = ser.readline().decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if line.startswith("#"):
                print(f"[fw] {line}", flush=True)
                continue
            if not line.startswith("R12,"):
                continue
            parts = line.split(",")
            if len(parts) != len(CSV_HEADERS) + 1:
                print(f"[w] parse fail (field count {len(parts)}): {line}", flush=True)
                continue
            writer.writerow(parts[1:])
            rows.append(parts[1:])
            now = time.monotonic()
            if now - last_report >= 5.0:
                last_report = now
                last = parts[1:]
                print(f"[i] frames={len(rows)} elapsed={now - t_start:.1f}s last_bb={last[3]} last_rssi={last[6]} dropped={last[13]}", flush=True)

    print(f"[i] collected {len(rows)} frames → {out_path}", flush=True)
    if len(rows) < 2:
        print("[w] too few frames to summarize")
        return

    # Decode numeric columns we need.
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
    print("=== bb_format histogram (PHY type of received frames) ===")
    for code, n in bb.most_common():
        name = BB_FORMAT_NAMES.get(code, f"?{code}")
        print(f"  {code} ({name}): {n}")
    print()
    print("=== Source MAC (BSSID) histogram (top 5) ===")
    for mac, n in src_set.most_common(5):
        print(f"  {mac}: {n}")
    print()
    print("=== rx_timestamp_us behavior (32-bit, wraps every ~71min) ===")
    # Monotonic check, allowing for one wrap.
    nonmono = 0
    dup = 0
    for i in range(1, len(rx_ts)):
        d = rx_ts[i] - rx_ts[i - 1]
        if d == 0:
            dup += 1
        elif d < 0 and d > -2_000_000_000:
            # negative but not a wrap-around-sized delta
            nonmono += 1
    print(f"  n_frames     = {len(rx_ts)}")
    print(f"  duplicates   = {dup}   (consecutive identical rx_timestamp; cf. esp-idf#2468)")
    print(f"  non-monotonic= {nonmono} (negative delta excluding wrap)")
    print(f"  fw dropped   = {dropped_final} (ring overruns reported by firmware)")
    print()
    print("=== RSSI ===")
    rs = sorted(rssi_list)
    if rs:
        print(f"  min/median/max = {rs[0]}/{rs[len(rs)//2]}/{rs[-1]} dBm")

    print()
    he_total = sum(n for c, n in bb.items() if 4 <= c <= 7)
    if he_total > 0:
        print(f"[PASS] HE frames observed: {he_total}")
    else:
        print("[FAIL] No HE PPDU frames observed — AP may not be in 11ax mode, or downlink HE was filtered")


if __name__ == "__main__":
    main()
