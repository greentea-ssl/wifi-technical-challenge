#!/usr/bin/env python3
# sniffer_runner — decode binary stream from sniffer.ino over UART.
#
# Protocol (per sniffer.ino):
#   sync = 0xC5 0xC5
#   header = sync(2) | type(1) | length(1)
#   type 0x01 (frame, 40 bytes total, payload 36 bytes):
#       rx_seq             u32 LE
#       t_local_us_lo      u32 LE
#       rx_timestamp_us    u32 LE
#       bb_format          u8
#       rate               u8
#       channel            u8
#       rssi               i8
#       sig_len            u16 LE
#       hdr_seq            u16 LE
#       src_mac            6 bytes
#       dst_mac            6 bytes
#       fc_lo, fc_hi       2 bytes
#       dropped_lo         u16 LE  (low 16 bits of cumulative drop count)
#   type 0x02 (heartbeat, 20 bytes total, payload 16 bytes):
#       captured_total     u32 LE
#       dropped_total      u32 LE
#       t_now_us_lo        u32 LE
#       rssi_now           i32 LE
#
# Usage:
#   python3 run.py --port /dev/ttyUSB0 --baud 2000000 --duration 60 \
#         --out phase0_results/sniffer.csv
#
# The runner reads at UART speed, syncs on the marker, decodes records,
# emits CSV, and prints per-second pps + drop deltas to stderr.

import argparse
import csv
import signal
import struct
import sys
import time
from pathlib import Path

try:
    import serial
except ImportError:
    sys.exit("pyserial not installed; pip install pyserial")


SYNC = bytes([0xC5, 0xC5])
TYPE_FRAME = 0x01
TYPE_HB    = 0x02
TYPE_CAL   = 0x03
TYPE_PPS   = 0x04
LEN_FRAME_PAYLOAD = 44   # sizeof(Entry)=42 + 2 (dropped_lo)
LEN_HB_PAYLOAD    = 16
LEN_CAL_PAYLOAD   = 24
LEN_PPS_PAYLOAD   = 16   # u64 tsf_us + u64 esp_timer_us
RECORD_FRAME_TOTAL = 4 + LEN_FRAME_PAYLOAD  # 48 bytes
RECORD_HB_TOTAL    = 4 + LEN_HB_PAYLOAD     # 20 bytes
RECORD_CAL_TOTAL   = 4 + LEN_CAL_PAYLOAD    # 28 bytes
RECORD_PPS_TOTAL   = 4 + LEN_PPS_PAYLOAD    # 20 bytes


FRAME_STRUCT = struct.Struct(
    "<"
    "I"   # rx_seq
    "I"   # t_local_us_lo
    "I"   # rx_timestamp_us
    "Q"   # tsf_us (uint64, AP TSF via midpoint fit、0 = calib not ready)
    "B"   # bb_format
    "B"   # rate
    "B"   # channel
    "b"   # rssi
    "H"   # sig_len
    "H"   # hdr_seq
    "6s"  # src
    "6s"  # dst
    "B"   # fc_lo
    "B"   # fc_hi
    "H"   # dropped_lo
)
assert FRAME_STRUCT.size == LEN_FRAME_PAYLOAD, FRAME_STRUCT.size

# v2: cycle_count(u32) 追加 (Entry=46/payload 48)、v3: + robot_id(u8) 追加 (Entry=47/payload 49)。
# gtnlv_rpid.py の内蔵 decoder と同様に 3 版を受理する (issue #7: 旧 run.py は v3 frame を
# 全破棄していた)。
LEN_FRAME_PAYLOAD_V2 = 48
LEN_FRAME_PAYLOAD_V3 = 49
FRAME_STRUCT_V2 = struct.Struct("<I I I Q B B B b H H 6s 6s B B I H")
FRAME_STRUCT_V3 = struct.Struct("<I I I Q B B B b H H 6s 6s B B I B H")
assert FRAME_STRUCT_V2.size == LEN_FRAME_PAYLOAD_V2, FRAME_STRUCT_V2.size
assert FRAME_STRUCT_V3.size == LEN_FRAME_PAYLOAD_V3, FRAME_STRUCT_V3.size
CYCLE_INVALID = 0xFFFFFFFF
ROBOT_ID_INVALID = 0xFF

HB_STRUCT = struct.Struct("<I I I i")
assert HB_STRUCT.size == LEN_HB_PAYLOAD

# cal_recv payload: t_recv_local_us (u64), cal_send_rt (double), cal_seq (u32),
# type_flag (u8), pad (3B)
CAL_STRUCT = struct.Struct("<Q d I B 3s")
assert CAL_STRUCT.size == LEN_CAL_PAYLOAD

# PPS payload: tsf_us (u64) + esp_timer_us (u64)
PPS_STRUCT = struct.Struct("<Q Q")
assert PPS_STRUCT.size == LEN_PPS_PAYLOAD


CSV_HEADERS = [
    "t_rpid_recv_unix",  # RasPi kernel CLOCK_REALTIME at UART read (A 軸)
    "rx_seq", "t_local_us_lo", "rx_timestamp_us",
    "tsf_us",            # AP TSF (B 軸、中点フィット適用済み、0 = calib not ready)
    "bb_format", "rate", "channel", "rssi", "sig_len",
    "src", "dst", "fc_lo", "fc_hi", "hdr_seq", "dropped_lo",
    "cycle_count", "robot_id",   # v2/v3 追加 (v1 frame では空)
]


def mac_str(b: bytes) -> str:
    return ":".join(f"{x:02X}" for x in b)


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True)
    ap.add_argument("--baud", type=int, default=2000000)
    ap.add_argument("--duration", type=float, default=60.0)
    ap.add_argument("--out", required=True, help="Output CSV path")
    return ap.parse_args()


def main():
    args = parse_args()
    out_path = Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)

    stop = {"v": False}

    def handler(sig, frame):
        stop["v"] = True

    signal.signal(signal.SIGINT, handler)

    print(f"[i] port={args.port} baud={args.baud} duration={args.duration}s out={out_path}",
          file=sys.stderr, flush=True)

    cal_path = out_path.with_name(out_path.stem + "_cal.csv")
    pps_path = out_path.with_name(out_path.stem + "_pps.csv")
    with serial.Serial(args.port, args.baud, timeout=0.5) as ser, \
            out_path.open("w", newline="", buffering=1) as f, \
            cal_path.open("w", newline="", buffering=1) as cf, \
            pps_path.open("w", newline="", buffering=1) as pf:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADERS)
        cwriter = csv.writer(cf)
        cwriter.writerow(["t_rpid_recv_unix", "t_recv_local_us", "cal_send_rt_unix",
                          "cal_seq", "type_flag"])
        pwriter = csv.writer(pf)
        pwriter.writerow(["t_rpid_recv_unix", "tsf_us", "esp_us"])

        buf = bytearray()
        n_frames = 0
        n_hb = 0
        n_cal = 0
        n_pps = 0
        last_dropped_total = 0
        last_captured_total = 0
        t_start = time.monotonic()
        last_report = t_start

        while not stop["v"] and (time.monotonic() - t_start) < args.duration:
            # in_waiting ベースの即時 read (gtnlv_rpid と同じ修正)。read(4096)+
            # timeout=0.5 だと t_rpid_recv が 0.5s batch に量子化されていた。
            navail = ser.in_waiting
            chunk = ser.read(navail if navail > 0 else 1)
            if chunk:
                buf.extend(chunk)

            while True:
                # Skip until SYNC
                idx = buf.find(SYNC)
                if idx < 0:
                    # Keep at most 1 byte in case it's the start of a sync marker
                    if len(buf) > 1:
                        del buf[:-1]
                    break
                if idx > 0:
                    # Garbage; could be a '#' boot-message line or framing slippage
                    drop_bytes = bytes(buf[:idx])
                    # Print interesting boot lines
                    try:
                        text = drop_bytes.decode("utf-8", errors="replace")
                        for line in text.splitlines():
                            if line.startswith("#"):
                                print(f"[fw] {line}", file=sys.stderr, flush=True)
                    except UnicodeDecodeError:
                        pass
                    del buf[:idx]

                # Need at least 4 bytes for header
                if len(buf) < 4:
                    break
                rtype = buf[2]
                rlen = buf[3]
                total = 4 + rlen
                if rtype == TYPE_FRAME and rlen not in (
                        LEN_FRAME_PAYLOAD, LEN_FRAME_PAYLOAD_V2, LEN_FRAME_PAYLOAD_V3):
                    del buf[:2]; continue
                if rtype == TYPE_HB and rlen != LEN_HB_PAYLOAD:
                    del buf[:2]; continue
                if rtype == TYPE_CAL and rlen != LEN_CAL_PAYLOAD:
                    del buf[:2]; continue
                if rtype == TYPE_PPS and rlen != LEN_PPS_PAYLOAD:
                    del buf[:2]; continue
                if rtype not in (TYPE_FRAME, TYPE_HB, TYPE_CAL, TYPE_PPS):
                    del buf[:2]; continue
                if len(buf) < total:
                    break
                payload = bytes(buf[4:total])
                del buf[:total]

                if rtype == TYPE_FRAME:
                    t_rpid_recv = time.time()
                    cycle_count = ""
                    robot_id = ""
                    if rlen == LEN_FRAME_PAYLOAD_V3:
                        (rx_seq, t_local_us_lo, rx_timestamp_us, tsf_us,
                         bb_format, rate, channel, rssi, sig_len, hdr_seq,
                         src, dst, fc_lo, fc_hi, cyc, rid, dropped_lo) = FRAME_STRUCT_V3.unpack(payload)
                        if cyc != CYCLE_INVALID: cycle_count = cyc
                        if rid != ROBOT_ID_INVALID: robot_id = rid
                    elif rlen == LEN_FRAME_PAYLOAD_V2:
                        (rx_seq, t_local_us_lo, rx_timestamp_us, tsf_us,
                         bb_format, rate, channel, rssi, sig_len, hdr_seq,
                         src, dst, fc_lo, fc_hi, cyc, dropped_lo) = FRAME_STRUCT_V2.unpack(payload)
                        if cyc != CYCLE_INVALID: cycle_count = cyc
                    else:
                        (rx_seq, t_local_us_lo, rx_timestamp_us, tsf_us,
                         bb_format, rate, channel, rssi, sig_len, hdr_seq,
                         src, dst, fc_lo, fc_hi, dropped_lo) = FRAME_STRUCT.unpack(payload)
                    writer.writerow([
                        f"{t_rpid_recv:.6f}",
                        rx_seq, t_local_us_lo, rx_timestamp_us, tsf_us,
                        bb_format, rate, channel, rssi, sig_len,
                        mac_str(src), mac_str(dst),
                        fc_lo, fc_hi, hdr_seq, dropped_lo,
                        cycle_count, robot_id,
                    ])
                    n_frames += 1
                elif rtype == TYPE_HB:
                    captured, dropped, t_now_us_lo, rssi_now = HB_STRUCT.unpack(payload)
                    delta_cap = captured - last_captured_total
                    delta_drop = dropped - last_dropped_total
                    print(f"[hb] captured={captured} (+{delta_cap})  "
                          f"dropped={dropped} (+{delta_drop})  rssi={rssi_now}",
                          file=sys.stderr, flush=True)
                    last_captured_total = captured
                    last_dropped_total = dropped
                    n_hb += 1
                elif rtype == TYPE_CAL:
                    t_recv_local_us, cal_send_rt, cal_seq, type_flag, _pad = \
                        CAL_STRUCT.unpack(payload)
                    t_rpid_recv = time.time()
                    cwriter.writerow([f"{t_rpid_recv:.6f}", t_recv_local_us,
                                      f"{cal_send_rt:.6f}", cal_seq, type_flag])
                    n_cal += 1
                    if n_cal <= 5 or n_cal % 50 == 0:
                        print(f"[cal] #{n_cal} seq={cal_seq} type={type_flag}",
                              file=sys.stderr, flush=True)
                elif rtype == TYPE_PPS:
                    tsf_us, esp_us = PPS_STRUCT.unpack(payload)
                    t_rpid_recv = time.time()
                    pwriter.writerow([f"{t_rpid_recv:.6f}", tsf_us, esp_us])
                    n_pps += 1
                    if n_pps <= 3 or n_pps % 10 == 0:
                        print(f"[pps] #{n_pps} tsf={tsf_us} (tsf%1e6={tsf_us % 1_000_000})",
                              file=sys.stderr, flush=True)

            # Per-second rate report
            now = time.monotonic()
            if now - last_report >= 5.0:
                last_report = now
                elapsed = now - t_start
                print(f"[i] frames={n_frames} hb={n_hb} elapsed={elapsed:.1f}s "
                      f"avg={n_frames/elapsed:.0f} fps", file=sys.stderr, flush=True)

    elapsed = time.monotonic() - t_start
    print(f"[done] frames={n_frames}  hb={n_hb}  cal={n_cal}  pps={n_pps}  duration={elapsed:.1f}s  "
          f"avg={n_frames/elapsed:.1f} fps  → {out_path}  (+ {cal_path}, {pps_path})",
          file=sys.stderr, flush=True)


if __name__ == "__main__":
    main()
