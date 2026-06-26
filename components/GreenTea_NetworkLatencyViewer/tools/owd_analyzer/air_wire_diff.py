#!/usr/bin/env python3
# air_wire_diff.py — Compute air-vs-wire arrival time difference for 4 cases:
#   DL bc  : host → 192.168.x.255      (cal_sender, dst port 43000, type_flag=0)
#   DL uc  : host → 192.168.x.100      (cal_sender to sniffer IP, dst port 43000, type_flag=1)
#   UL bc  : reflector → 192.168.x.255 (rx_dl JSON port 52001, fake_uplink port 50001)
#   UL uc  : reflector → host PC       (UL UC test channel, dst port 49000)
#
# Air = AX210 monitor pcap (radiotap, dot11)
# Wire = eth0 SPAN pcap (PHC nano timestamps)
#
# Matching strategy:
#   - DL bc/uc cal_sender: extract cal_seq from payload offset 8 (uint32 LE)
#   - UL uc: extract ul_uc_seq from payload offset 4 (uint32 LE, after magic 0xCA113AA0)
#   - UL bc 50001: timing-based match within ±5ms (fake_uplink @ 10Hz)
#   - UL bc 52001: similarly, JSON contains hid_seq but parsing optional
#
# Usage:
#   python3 air_wire_diff.py --air air.pcap --wire wire.pcap

import argparse
import struct
from collections import defaultdict
from scapy.all import rdpcap, Dot11, RadioTap, IP, UDP, Raw, sniff

CAL_PORT  = 43000
UL50001   = 50001
UL52001   = 52001
ULUC_PORT = 49000
ULUC_MAGIC = 0xCA113AA0


def get_pkt_time(pkt):
    """tcpdump -j adapter_unsynced 経由の radiotap TSFT or PHC ts."""
    return float(pkt.time)


def parse_cal_payload(pl: bytes):
    """cal_sender payload: send_RT (double), cal_seq (u32), type_flag (u8)."""
    if len(pl) < 13:
        return None
    send_rt, cal_seq, type_flag = struct.unpack("<dIB", pl[:13])
    return {"send_rt": send_rt, "cal_seq": cal_seq, "type_flag": type_flag}


def parse_uluc_payload(pl: bytes):
    if len(pl) < 16:
        return None
    magic, ul_uc_seq, t_local = struct.unpack("<IIQ", pl[:16])
    if magic != ULUC_MAGIC:
        return None
    return {"ul_uc_seq": ul_uc_seq, "t_local_us": t_local}


def index_pcap_by_udp(path, label):
    """Index UDP packets in pcap by (dst_port, sport_indep_key).
    Returns dict: { (port, key) -> [list of (ts, src_ip, dst_ip)] }"""
    print(f"[i] loading {label} pcap: {path}")
    pkts = rdpcap(path)
    print(f"[i]   {len(pkts)} raw frames")
    by_port = defaultdict(list)
    n_udp = 0
    for p in pkts:
        # Skip non-IP/UDP. For air pcap with 802.11, IP/UDP layers may need
        # to come via LLC. scapy's rdpcap should handle radiotap+dot11+llc.
        try:
            if not p.haslayer(UDP):
                continue
            ip = p[IP]
            udp = p[UDP]
            sport, dport = udp.sport, udp.dport
            raw = bytes(udp.payload) if udp.payload else b""
            ts = float(p.time)
            n_udp += 1
            entry = (ts, ip.src, ip.dst, raw)
            # group by destination port (covers both directions in our test)
            by_port[dport].append(entry)
        except Exception:
            continue
    print(f"[i]   {n_udp} UDP frames in {label}")
    return by_port


def match_cal(air, wire, type_filter):
    """Match cal frames by cal_seq for given type_flag."""
    label = "bc" if type_filter == 0 else "uc"
    a_by_seq = {}
    for ts, sip, dip, payload in air.get(CAL_PORT, []):
        info = parse_cal_payload(payload)
        if info and info["type_flag"] == type_filter:
            # keep first arrival per cal_seq (air may have AP rebroadcast too)
            if info["cal_seq"] not in a_by_seq:
                a_by_seq[info["cal_seq"]] = ts
    w_by_seq = {}
    for ts, sip, dip, payload in wire.get(CAL_PORT, []):
        info = parse_cal_payload(payload)
        if info and info["type_flag"] == type_filter:
            if info["cal_seq"] not in w_by_seq:
                w_by_seq[info["cal_seq"]] = ts
    common = set(a_by_seq.keys()) & set(w_by_seq.keys())
    diffs = sorted((a_by_seq[s] - w_by_seq[s]) * 1e6 for s in common)
    return label, len(a_by_seq), len(w_by_seq), len(common), diffs


def match_uluc(air, wire):
    """UL uc: match by ul_uc_seq."""
    a_by_seq = {}
    for ts, sip, dip, payload in air.get(ULUC_PORT, []):
        info = parse_uluc_payload(payload)
        if info and info["ul_uc_seq"] not in a_by_seq:
            a_by_seq[info["ul_uc_seq"]] = ts
    w_by_seq = {}
    for ts, sip, dip, payload in wire.get(ULUC_PORT, []):
        info = parse_uluc_payload(payload)
        if info and info["ul_uc_seq"] not in w_by_seq:
            w_by_seq[info["ul_uc_seq"]] = ts
    common = set(a_by_seq.keys()) & set(w_by_seq.keys())
    # UL: wire − air (eth0 is later because AP forwards to wire after receiving air)
    diffs = sorted((w_by_seq[s] - a_by_seq[s]) * 1e6 for s in common)
    return "uc", len(a_by_seq), len(w_by_seq), len(common), diffs


def match_ulbc_timing(air, wire, port):
    """UL bc: timing-based match. For each wire frame, find nearest air frame
    within ±50ms (loose window). Returns wire−air diff."""
    a_list = sorted(t for (t, _, _, _) in air.get(port, []))
    w_list = sorted(t for (t, _, _, _) in wire.get(port, []))
    diffs = []
    i = 0
    for w in w_list:
        # advance i to nearest air ts
        while i + 1 < len(a_list) and abs(a_list[i + 1] - w) < abs(a_list[i] - w):
            i += 1
        if i < len(a_list) and abs(a_list[i] - w) < 0.050:
            diffs.append((w - a_list[i]) * 1e6)
    return f"bc_{port}", len(a_list), len(w_list), len(diffs), sorted(diffs)


def summarize(label, na, nw, n_match, diffs):
    print(f"\n=== {label}: air={na} wire={nw} matched={n_match} ===")
    if not diffs:
        print("  (no matched samples)")
        return
    n = len(diffs)
    p = lambda q: diffs[min(int(n * q), n - 1)]
    print(f"  min    = {diffs[0]:>10.1f} us")
    print(f"  p5     = {p(0.05):>10.1f} us")
    print(f"  median = {p(0.50):>10.1f} us")
    print(f"  mean   = {sum(diffs)/n:>10.1f} us")
    print(f"  p95    = {p(0.95):>10.1f} us")
    print(f"  p99    = {p(0.99):>10.1f} us")
    print(f"  max    = {diffs[-1]:>10.1f} us")
    # histogram bins
    print("  hist (us):")
    bins = [0, 500, 1000, 2000, 5000, 10000, 30000, 50000, 100000, 1e7]
    counts = [0] * (len(bins) + 1)
    for d in diffs:
        ad = abs(d)
        for i, b in enumerate(bins):
            if ad < b:
                counts[i] += 1
                break
        else:
            counts[-1] += 1
    edges = ["<500us", "<1ms", "<2ms", "<5ms", "<10ms", "<30ms", "<50ms",
             "<100ms", "<10s", ">10s"]
    for e, c in zip(edges, counts):
        bar = '#' * min(50, c * 50 // max(1, n))
        print(f"    {e:>8s}: {c:>6d}  {bar}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--air", required=True, help="AX210 monitor pcap")
    ap.add_argument("--wire", required=True, help="eth0 SPAN pcap")
    args = ap.parse_args()

    air = index_pcap_by_udp(args.air, "air")
    wire = index_pcap_by_udp(args.wire, "wire")

    print()
    print("============================================================")
    print(" DL = cal_sender (host → AP → STA)")
    print(" Diff = air_arrival − wire_arrival  (positive = air later)")
    print("============================================================")
    for tf, name in [(0, "DL bc (host→.255)"), (1, "DL uc (host→.100 sniffer)")]:
        l, na, nw, nm, d = match_cal(air, wire, tf)
        summarize(name, na, nw, nm, d)

    print()
    print("============================================================")
    print(" UL = reflector → wire")
    print(" Diff = wire_arrival − air_arrival  (positive = wire later)")
    print("============================================================")
    for port, name in [(UL50001, "UL bc (.111→.255:50001 fake_uplink)"),
                       (UL52001, "UL bc (.111→.255:52001 rx_dl)"),
                       (ULUC_PORT, "UL uc (.111→.160:49000)")]:
        if port == ULUC_PORT:
            l, na, nw, nm, d = match_uluc(air, wire)
            summarize(name, na, nw, nm, d)
        else:
            l, na, nw, nm, d = match_ulbc_timing(air, wire, port)
            summarize(name, na, nw, nm, d)


if __name__ == "__main__":
    main()
