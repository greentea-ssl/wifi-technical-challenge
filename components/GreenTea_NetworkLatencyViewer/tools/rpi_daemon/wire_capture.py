#!/usr/bin/env python3
# wire_capture.py — eth0 SPAN frames with PHC hardware timestamp + CLOCK_REALTIME.
#
# Captures UDP frames flowing AIPC→AP on the SPAN mirror port (RasPi eth0).
# Records both PHC (raw hardware timestamp from /dev/ptp0) and CLOCK_REALTIME
# software timestamp via SO_TIMESTAMPING, so wire arrival can be aligned to
# the unix-axis used by gtnlv-rpid via (sw_ts, hw_ts) pairs.
#
# Pre-req:
#   sudo hwstamp_ctl -i eth0 -r 1 -t 0    # rx_filter ALL, tx OFF
#
# Output CSV (wire_capture.csv) columns:
#   cycle_count, t_hw_ns_phc, t_sw_unix_ns, src_ip, dst_ip, dst_port, payload_len
#
# Usage:
#   sudo python3 wire_capture.py --iface eth0 --dst-port 40001 \
#       --duration 300 --out wire_capture.csv

import argparse
import csv
import socket
import struct
import sys
import time
from pathlib import Path

# Linux SO_TIMESTAMPING flags (from <linux/net_tstamp.h>)
SOL_SOCKET            = 1
SO_TIMESTAMPING       = 37
SCM_TIMESTAMPING      = 37
SOF_TIMESTAMPING_RX_HARDWARE  = (1 << 2)
SOF_TIMESTAMPING_RX_SOFTWARE  = (1 << 3)
SOF_TIMESTAMPING_SOFTWARE     = (1 << 4)
SOF_TIMESTAMPING_RAW_HARDWARE = (1 << 6)

ETH_P_IP = 0x0800
IP_PROTO_UDP = 17

# In the AI downlink frame (downlink_command.md), the 64B payload has:
#   offset 38-45 = unix_time (double LE) used as correlation key
#   offset 51-53 = cycle_count (24bit LE) used as loss-tracking key (54-61 dummy)
# (旧実装は offset 52 を aipc_seq(uint32) として読んでいたが、aipc_seq は廃止され
#  cycle_count に統合済。offset 52-55 読みは cycle_count 上位 2B + dummy を跨ぎ無意味、issue #8)
CYCLE_COUNT_OFFSET_IN_PAYLOAD = 51


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("--iface", default="eth0")
    p.add_argument("--dst-port", type=int, default=40001,
                   help="UDP destination port to filter (default 40001 = robot_id 1)")
    p.add_argument("--dst-ip", default=None,
                   help="optional destination IP filter (e.g. 192.168.1.191)")
    p.add_argument("--duration", type=float, default=60.0)
    p.add_argument("--out", default="wire_capture.csv")
    return p.parse_args()


def parse_timespec_triplet(cmsg_data):
    """SCM_TIMESTAMPING delivers 3 struct timespec (sw / hw_legacy / hw_raw).
    On 64-bit Linux: timespec = { long sec; long nsec; } = 16 bytes each = 48 total."""
    if len(cmsg_data) < 48:
        return None, None
    sw_sec, sw_nsec, _hwt_sec, _hwt_nsec, hw_sec, hw_nsec = struct.unpack(
        "qqqqqq", cmsg_data[:48])
    sw_ns = sw_sec * 1_000_000_000 + sw_nsec if (sw_sec or sw_nsec) else None
    hw_ns = hw_sec * 1_000_000_000 + hw_nsec if (hw_sec or hw_nsec) else None
    return sw_ns, hw_ns


def main():
    args = parse_args()

    # Raw IPv4 socket on the interface (AF_PACKET, SOCK_RAW)
    s = socket.socket(socket.AF_PACKET, socket.SOCK_RAW, socket.htons(ETH_P_IP))
    s.bind((args.iface, 0))

    flags = (SOF_TIMESTAMPING_RX_HARDWARE | SOF_TIMESTAMPING_RX_SOFTWARE
             | SOF_TIMESTAMPING_SOFTWARE | SOF_TIMESTAMPING_RAW_HARDWARE)
    s.setsockopt(SOL_SOCKET, SO_TIMESTAMPING, flags)

    out_path = Path(args.out)
    fh = out_path.open("w", newline="", buffering=1)
    w = csv.writer(fh)
    w.writerow(["cycle_count", "t_hw_ns_phc", "t_sw_unix_ns",
                "src_ip", "dst_ip", "dst_port", "payload_len"])

    deadline = time.time() + args.duration
    n_pkts = 0
    n_match = 0
    n_hw = 0
    next_report = time.time() + 5.0

    while time.time() < deadline:
        try:
            data, ancdata, _flags, _addr = s.recvmsg(2048, 256)
        except KeyboardInterrupt:
            break
        n_pkts += 1

        # Parse Ethernet (14B) — already stripped since we bind to ETH_P_IP via AF_PACKET?
        # Actually AF_PACKET with SOCK_RAW + bound to ETH_P_IP delivers Ethernet+payload.
        # We need to skip 14B Ethernet header. But on macb driver, packet may already be
        # IP. Check by looking at first byte: IPv4 has version 4 → 0x45 = first byte 0x45.
        if len(data) < 20:
            continue
        # Skip ethernet header if present
        if data[0] != 0x45 and len(data) >= 14:
            # Check ethertype
            ethertype = struct.unpack("!H", data[12:14])[0]
            if ethertype == ETH_P_IP:
                ip_data = data[14:]
            else:
                continue
        else:
            ip_data = data

        if len(ip_data) < 28:  # IP(20) + UDP(8)
            continue
        ip_hdr0 = ip_data[0]
        ihl = (ip_hdr0 & 0x0F) * 4
        if (ip_hdr0 >> 4) != 4 or ihl < 20 or len(ip_data) < ihl + 8:
            continue
        proto = ip_data[9]
        if proto != IP_PROTO_UDP:
            continue
        src_ip = socket.inet_ntoa(ip_data[12:16])
        dst_ip = socket.inet_ntoa(ip_data[16:20])
        udp_off = ihl
        src_port, dst_port, ulen, _ucsum = struct.unpack("!HHHH", ip_data[udp_off:udp_off + 8])
        if args.dst_port != 0 and dst_port != args.dst_port:
            continue
        if args.dst_ip and dst_ip != args.dst_ip:
            continue
        payload = ip_data[udp_off + 8:udp_off + ulen]
        # --dst-port 0 で全 UDP record する時は payload 短くても cycle_count 不在で OK
        if args.dst_port != 0 and len(payload) < CYCLE_COUNT_OFFSET_IN_PAYLOAD + 3:
            continue
        if len(payload) >= CYCLE_COUNT_OFFSET_IN_PAYLOAD + 3:
            b = payload[CYCLE_COUNT_OFFSET_IN_PAYLOAD:CYCLE_COUNT_OFFSET_IN_PAYLOAD + 3]
            cycle_count = b[0] | (b[1] << 8) | (b[2] << 16)   # 24bit LE
        else:
            cycle_count = -1  # port 43000 (cal) や短い payload は seq 不在

        sw_ns, hw_ns = None, None
        for level, type_, cdata in ancdata:
            if level == SOL_SOCKET and type_ == SCM_TIMESTAMPING:
                sw_ns, hw_ns = parse_timespec_triplet(cdata)
                break
        if hw_ns is not None:
            n_hw += 1

        w.writerow([cycle_count, hw_ns if hw_ns else "", sw_ns if sw_ns else "",
                    src_ip, dst_ip, dst_port, len(payload)])
        n_match += 1

        if time.time() >= next_report:
            elapsed = next_report - (deadline - args.duration)
            print(f"[wire] elapsed={elapsed:.1f}s pkts_seen={n_pkts} "
                  f"matched={n_match} hw_ts={n_hw}", file=sys.stderr)
            next_report += 5.0

    fh.close()
    print(f"[wire] done. pkts_seen={n_pkts} matched={n_match} hw_ts={n_hw} "
          f"→ {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
