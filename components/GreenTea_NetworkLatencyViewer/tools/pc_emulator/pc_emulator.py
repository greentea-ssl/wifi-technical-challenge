#!/usr/bin/env python3
# pc_emulator — AI-equivalent downlink packet sender for testing the
# GreenTea NetworkLatencyViewer measurement pipeline without a live AI.
#
# Sends 64-byte UDP packets to robot<id>.local:40000+id (mDNS-resolved) or a
# direct IP, matching the "AI 指令用 packet" defined in
# robot_comm_spec/downlink_command.md.
#
# Optionally listens on:
#   - 50000+id  (uplink_telemetry, production CU-origin uplink JSON)
#   - 51000+id  (hid_bridge, HID-origin CAN telemetry JSON)
#   - 52000+id  (radio_metrics, HID-origin rx_dl / tx_ul / hb JSON)
# All listeners just raw-dump received packets for verification; OWD
# computation is gtnlv-rpid's job.
#
# Notes on the 64-byte format:
#   - byte  0..1: HEADER_1 0xFF, HEADER_2 0xC3
#   - byte  2:    robot_id in low nibble
#   - byte 35:    flag byte with literal bits "d01e fg10" → default 0x22
#                 (literal-1 bits at positions 5 and 1)
#   - byte 37:    "pq00 0000" → default 0x00 (all flags off)
#   - byte 38..45: unix_time (double LE, IEEE 754)
#                 [byte 46 reserved/dummy — doc range "38-46" is off by one,
#                  dev branch lessons §7.1 confirms 8-byte double at 38-45]
#   - byte 62:    checksum = XOR over bytes 2..61
#   - byte 63:    byte 62 XOR 0xFF

import argparse
import socket
import struct
import sys
import threading
import time


PKT_LEN = 64


def build_downlink_packet(
    robot_id: int,
    unix_time: float,
    cycle_count: int = 0,
    *,
    robot_x_mm: int = 0,
    robot_y_mm: int = 0,
    robot_theta_u16: int = 0,
    pos_cmd_x_mm: int = 0,
    pos_cmd_y_mm: int = 0,
    pos_cmd_theta_u16: int = 0,
    cmd_vx_mms: int = 0,
    cmd_vy_mms: int = 0,
    cmd_omega_q10: int = 0,
    cmd_ax_mms2: int = 0,
    cmd_ay_mms2: int = 0,
    cmd_aomega_q10: int = 0,
    limit_velocity_mms: int = 0,
    limit_omega_q10: int = 0,
    flags_byte_35: int = 0x22,
    dribble_power: int = 0,
    esys_flags_byte_37: int = 0,
    esys_target_x_mm: int = 0,
    esys_target_y_mm: int = 0,
    kick_speed_q5: int = 0,
) -> bytes:
    """Build a 64-byte AI-downlink packet per downlink_command.md."""
    pkt = bytearray(PKT_LEN)
    pkt[0] = 0xFF
    pkt[1] = 0xC3
    pkt[2] = robot_id & 0x0F
    struct.pack_into("<h", pkt, 3, robot_x_mm)
    struct.pack_into("<h", pkt, 5, robot_y_mm)
    struct.pack_into("<H", pkt, 7, robot_theta_u16 & 0xFFFF)
    now_ms = int((unix_time % 86400.0) * 1000) & 0xFFFF
    struct.pack_into("<H", pkt, 9, now_ms)
    struct.pack_into("<H", pkt, 11, now_ms)
    struct.pack_into("<h", pkt, 13, pos_cmd_x_mm)
    struct.pack_into("<h", pkt, 15, pos_cmd_y_mm)
    struct.pack_into("<H", pkt, 17, pos_cmd_theta_u16 & 0xFFFF)
    struct.pack_into("<h", pkt, 19, cmd_vx_mms)
    struct.pack_into("<h", pkt, 21, cmd_vy_mms)
    struct.pack_into("<h", pkt, 23, cmd_omega_q10)
    struct.pack_into("<h", pkt, 25, cmd_ax_mms2)
    struct.pack_into("<h", pkt, 27, cmd_ay_mms2)
    struct.pack_into("<h", pkt, 29, cmd_aomega_q10)
    struct.pack_into("<h", pkt, 31, limit_velocity_mms)
    struct.pack_into("<h", pkt, 33, limit_omega_q10)
    pkt[35] = flags_byte_35 & 0xFF
    pkt[36] = dribble_power & 0xFF
    pkt[37] = esys_flags_byte_37 & 0xFF
    # Bytes 38..45 = unix_time (double LE)
    struct.pack_into("<d", pkt, 38, float(unix_time))
    # offset は downlink_command.md に厳密一致 (旧実装は 1 byte ずれていた):
    #   46-47 esys_target_x (int16), 48-49 esys_target_y (int16),
    #   50 kick_speed_cmd (uint8), 51-53 cycle_count (24bit LE), 54-61 dummy
    struct.pack_into("<h", pkt, 46, esys_target_x_mm)
    struct.pack_into("<h", pkt, 48, esys_target_y_mm)
    pkt[50] = kick_speed_q5 & 0xFF
    cc = cycle_count & 0xFFFFFF  # 24bit (0-16,777,215, wrap-around)
    pkt[51] = cc & 0xFF
    pkt[52] = (cc >> 8) & 0xFF
    pkt[53] = (cc >> 16) & 0xFF
    # 54-61 は dummy (0 のまま)
    chk = 0
    for b in pkt[2:62]:
        chk ^= b
    pkt[62] = chk & 0xFF
    pkt[63] = (chk ^ 0xFF) & 0xFF
    return bytes(pkt)


def listener_thread(port: int, label: str, stop_evt: threading.Event, log_every_n: int = 100):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    sock.bind(("0.0.0.0", port))
    sock.settimeout(0.5)
    n = 0
    print(f"[{label}] listening UDP {port}", flush=True)
    while not stop_evt.is_set():
        try:
            data, addr = sock.recvfrom(4096)
        except socket.timeout:
            continue
        n += 1
        if n <= 5 or n % log_every_n == 0:
            if data[:1] == b"{":
                preview = data.decode("utf-8", errors="replace").rstrip()
            else:
                preview = data[:32].hex()
            print(f"[{label}] #{n} from {addr[0]}:{addr[1]} size={len(data)}  {preview}", flush=True)
    sock.close()
    print(f"[{label}] stopped, {n} packets total", flush=True)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--robot-id", type=int, default=1, help="robot ID 0..15 (default 1)")
    ap.add_argument("--robot-ids", default=None,
                    help="comma-separated robot IDs for multi-target flood "
                         "(e.g. '1,2,3,...,10')。--robot-id を上書き、単一周期ループが --rate Hz で回り "
                         "1 周期で全 robot へ共通 cycle_count で送出 (実機 AI 送信タスク相当)。"
                         "宛先 IP は --target-base から base.split('.')[:3] + '.' + str(110+id) で組立 "
                         "(--target-base 192.168.4.0 → robot 1→.111, 2→.112, ...)")
    ap.add_argument("--target", default=None,
                    help="destination IP (single-robot mode). default: resolve robot<id>.local via mDNS")
    ap.add_argument("--target-base", default="192.168.4.0",
                    help="multi-target mode の base IP (default 192.168.4.0)。robot N → base/24 + (110+N)")
    ap.add_argument("--port", type=int, default=None,
                    help="destination UDP port. default 40000 + robot_id (multi mode は各 robot 別 port)")
    ap.add_argument("--rate", type=float, default=60.0,
                    help="send rate Hz。single mode: robot への送出周期。multi mode: AI 送信タスクの "
                         "周期レート (cycle_count が +1 する頻度)。各 robot は rate pkts/s、合計 rate × N")
    ap.add_argument("--duration", type=float, default=30.0,
                    help="seconds; 0 = until Ctrl-C (default 30)")
    ap.add_argument("--listen-uplink", action="store_true",
                    help="also listen on 50000+id and dump raw")
    ap.add_argument("--listen-bridge", action="store_true",
                    help="also listen on 51000+id (hid_bridge) and dump raw")
    ap.add_argument("--listen-metrics", action="store_true",
                    help="also listen on 52000+id (radio_metrics) and dump raw")
    args = ap.parse_args()

    # === multi-robot mode ===
    if args.robot_ids:
        ids = [int(x.strip()) for x in args.robot_ids.split(',') if x.strip()]
        base_octets = args.target_base.split('.')[:3]
        targets = []
        for rid in ids:
            ip = '.'.join(base_octets + [str(110 + rid)])
            port = args.port if args.port is not None else (40000 + rid)
            targets.append((rid, ip, port))

        print(f"[i] multi-robot mode: {len(ids)} robots、周期レート {args.rate} Hz "
              f"(共通 cycle_count)、合計 {args.rate * len(ids):.0f} pkts/s", flush=True)
        for rid, ip, port in targets:
            print(f"    robot{rid} → {ip}:{port}", flush=True)
        print(f"[i] duration {args.duration}s", flush=True)

        # 単一周期ループ: 1 周期ごとに cycle_count を +1 し、その周期で全 robot へ
        # 共通 cycle_count で送出する (実機 AI の送信処理タスク相当、同時送信の
        # 複数 robot は共通値)。
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sent_counter = {rid: 0 for rid in ids}
        cycle = 0
        period = 1.0 / args.rate
        t_start = time.monotonic()
        next_send = t_start
        try:
            while True:
                now_unix = time.time()
                for rid, ip, port in targets:
                    pkt = build_downlink_packet(rid, now_unix, cycle_count=cycle)
                    try:
                        sock.sendto(pkt, (ip, port))
                    except OSError:
                        pass  # ARP fail 等で sendto エラー出ても継続
                    sent_counter[rid] += 1
                cycle = (cycle + 1) & 0xFFFFFF  # 周期ごと +1、24bit wrap
                next_send += period
                now_mono = time.monotonic()
                if args.duration > 0 and (now_mono - t_start) >= args.duration:
                    break
                sleep_dur = next_send - now_mono
                if sleep_dur > 0:
                    time.sleep(sleep_dur)
                else:
                    next_send = now_mono  # 追いつかない時は catch-up しない
        except KeyboardInterrupt:
            pass
        finally:
            sock.close()
        elapsed = time.monotonic() - t_start
        total = sum(sent_counter.values())
        print(f"[i] total sent {total} pkts in {elapsed:.1f}s "
              f"({cycle} cycles, avg {total/elapsed:.1f} pkts/s)", flush=True)
        for rid in ids:
            print(f"    robot{rid}: {sent_counter[rid]} pkts", flush=True)
        return

    # === single-robot mode (legacy) ===
    target_port = args.port if args.port is not None else (40000 + args.robot_id)
    target_ip = args.target
    if target_ip is None:
        try:
            name = f"robot{args.robot_id}.local"
            target_ip = socket.gethostbyname(name)
            print(f"[i] mDNS {name} -> {target_ip}", flush=True)
        except socket.gaierror as e:
            sys.exit(f"[err] mDNS resolve failed for robot{args.robot_id}.local: {e}\n"
                     f"[err] pass --target IP_ADDRESS explicitly")

    stop_evt = threading.Event()
    listeners = []
    if args.listen_uplink:
        listeners.append(threading.Thread(target=listener_thread,
                                          args=(50000 + args.robot_id, "uplink", stop_evt),
                                          daemon=True))
    if args.listen_bridge:
        listeners.append(threading.Thread(target=listener_thread,
                                          args=(51000 + args.robot_id, "bridge", stop_evt),
                                          daemon=True))
    if args.listen_metrics:
        listeners.append(threading.Thread(target=listener_thread,
                                          args=(52000 + args.robot_id, "metrics", stop_evt),
                                          daemon=True))
    for t in listeners:
        t.start()

    print(f"[i] target {target_ip}:{target_port}  rate {args.rate} Hz", flush=True)
    if args.duration > 0:
        print(f"[i] duration {args.duration}s (Ctrl-C to stop early)", flush=True)
    else:
        print(f"[i] duration unlimited (Ctrl-C to stop)", flush=True)

    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    period = 1.0 / args.rate
    sent = 0
    t_start = time.monotonic()
    next_send = t_start
    try:
        while True:
            now_unix = time.time()
            pkt = build_downlink_packet(args.robot_id, now_unix, cycle_count=sent)
            sock.sendto(pkt, (target_ip, target_port))
            sent += 1
            if sent <= 3 or sent % 200 == 0:
                print(f"[tx] #{sent} unix_time={now_unix:.6f} cycle_count={sent-1}", flush=True)
            next_send += period
            now_mono = time.monotonic()
            if args.duration > 0 and (now_mono - t_start) >= args.duration:
                break
            sleep_dur = next_send - now_mono
            if sleep_dur > 0:
                time.sleep(sleep_dur)
            else:
                next_send = now_mono  # don't try to catch up
    except KeyboardInterrupt:
        pass
    finally:
        elapsed = time.monotonic() - t_start
        rate = sent / elapsed if elapsed > 0 else 0.0
        print(f"[i] sent {sent} pkts in {elapsed:.1f}s (avg {rate:.1f} Hz)", flush=True)
        stop_evt.set()
        time.sleep(0.6)
        sock.close()


if __name__ == "__main__":
    main()
