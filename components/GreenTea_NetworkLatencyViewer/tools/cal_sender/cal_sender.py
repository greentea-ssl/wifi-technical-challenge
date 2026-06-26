#!/usr/bin/env python3
# cal_sender — AP unicast vs broadcast calibration packet generator
#
# Alternately sends:
#   - broadcast UDP to <broadcast>:43000 (type_flag=0)
#   - unicast UDP to <sniffer_ip>:43000 (type_flag=1)
# at a low rate. The sniffer firmware (with cal listener) records each
# arrival's esp_timer; analyzer compares the delays between the two groups
# to estimate AP's unicast-vs-broadcast processing time difference, plus
# per-group absolute delay distribution.
#
# Payload layout (16 bytes):
#   offset 0-7:   cal_send_rt (double LE, host CLOCK_REALTIME at send)
#   offset 8-11:  cal_seq (uint32 LE, monotonic)
#   offset 12:    type_flag (0 = bc, 1 = uc)
#   offset 13-15: reserved (zero)
#
# Usage:
#   python3 cal_sender.py --sniffer-ip 192.168.1.193 --bc-target 192.168.1.255 \
#       --rate-pair 1.0 --duration 300 --out phase1_results_cal/send_log.csv

import argparse
import csv
import socket
import struct
import sys
import time
from pathlib import Path


CAL_PORT = 43000


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--sniffer-ip", required=True, help="IP of sniffer for unicast target")
    ap.add_argument("--bc-target", default="192.168.1.255", help="broadcast target IP")
    ap.add_argument("--rate-pair", type=float, default=1.0,
                    help="pairs (bc + uc) per second; default 1 → 1 bc + 1 uc per second")
    ap.add_argument("--duration", type=float, default=300.0)
    ap.add_argument("--out", required=True, help="CSV log of sent packets")
    return ap.parse_args()


def build_packet(cal_seq: int, type_flag: int, send_rt: float) -> bytes:
    pkt = bytearray(16)
    struct.pack_into("<d", pkt, 0, send_rt)
    struct.pack_into("<I", pkt, 8, cal_seq & 0xFFFFFFFF)
    pkt[12] = type_flag & 0xFF
    return bytes(pkt)


def main():
    args = parse_args()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)

    period_total = 1.0 / args.rate_pair          # seconds per pair
    half = period_total / 2.0                     # spacing between bc and uc

    print(f"[i] cal_sender bc={args.bc_target}:{CAL_PORT} uc={args.sniffer_ip}:{CAL_PORT}",
          file=sys.stderr)
    print(f"[i] rate_pair={args.rate_pair}/s duration={args.duration}s out={out_path}",
          file=sys.stderr)

    t_start = time.monotonic()
    cal_seq = 0
    with out_path.open("w", newline="", buffering=1) as f:
        w = csv.writer(f)
        w.writerow(["cal_seq", "type_flag", "type", "target_ip", "send_rt_unix"])
        next_t = t_start
        while True:
            now = time.monotonic()
            if (now - t_start) >= args.duration:
                break
            # broadcast first, then half-period later unicast
            for type_flag, target_ip in [(0, args.bc_target), (1, args.sniffer_ip)]:
                t_now = time.monotonic()
                if (t_now - t_start) >= args.duration:
                    break
                send_rt = time.time()
                pkt = build_packet(cal_seq, type_flag, send_rt)
                try:
                    sock.sendto(pkt, (target_ip, CAL_PORT))
                except OSError as e:
                    print(f"[w] send {target_ip}: {e}", file=sys.stderr)
                w.writerow([cal_seq, type_flag, "bc" if type_flag == 0 else "uc",
                            target_ip, f"{send_rt:.6f}"])
                cal_seq += 1
                if cal_seq <= 4 or cal_seq % 20 == 0:
                    print(f"[tx] #{cal_seq} type={'bc' if type_flag==0 else 'uc'} → {target_ip}",
                          file=sys.stderr)
                time.sleep(half)
    sock.close()
    print(f"[done] sent {cal_seq} cal packets", file=sys.stderr)


if __name__ == "__main__":
    main()
