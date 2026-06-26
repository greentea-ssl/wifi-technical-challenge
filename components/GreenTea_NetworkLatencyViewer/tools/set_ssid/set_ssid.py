#!/usr/bin/env python3
# set_ssid — radio_metrics 計測時に HID 群の接続先 WiFi を一括切替する診断ヘルパ。
#
# 背景: HID 既定は TEAM_SSID (WPA2)。WPA2 だと sniffer が unicast/ToDS の payload
# (cycle_count/meta) を読めず air 区間分解 (leg ①②③) ができない。計測時のみ
# TEAM_SSID_OPEN (非暗号、hidden) へ切替えると sniffer が全フレームを平文で観測でき、
# leg 分解が成立する。set_ssid は揮発 (HID 再起動で既定に戻る) なので測定セッション毎に叩く。
#
# robot_comm_spec hid_bridge.md (v2.1.0) の downlink `set_ssid` を port 41000+id へ送る。
#
# 使い方:
#   python3 set_ssid.py --robots 0,1 --mode open     # 計測前: Open へ
#   python3 set_ssid.py --robots 0,1 --mode normal   # 計測後: TEAM_SSID へ戻す
#   python3 set_ssid.py --robots 0 --ssid Foo --password bar  # 任意指定
#
# 宛先 IP は robot{id}.local を mDNS 解決 (--target で個別上書き、--robots と同数)。
# 切替後 HID は所属ネットワークが変わるが、1 系統合 (192.168.1.x) なので同一 subnet で
# 引き続き reachable。

import argparse
import json
import socket
import sys

# mode プリセット (ssid, password)。password 空文字 = オープン AP。
MODES = {
    "open":   ("TEAM_SSID_OPEN", ""),
    "normal": ("TEAM_SSID", ""),
}


def resolve(robot_id: int, override: str | None) -> str:
    if override:
        return override
    return socket.gethostbyname(f"robot{robot_id}.local")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--robots", required=True,
                    help="comma-separated robot_ids (例 '0,1,2')")
    ap.add_argument("--mode", choices=sorted(MODES), default="open",
                    help="open=TEAM_SSID_OPEN(計測用) / normal=TEAM_SSID(既定に戻す)")
    ap.add_argument("--ssid", help="SSID を直接指定 (--mode を上書き)")
    ap.add_argument("--password", help="パスフレーズ (省略/空=オープン)")
    ap.add_argument("--targets", help="宛先 IP を robot と同順でカンマ指定 (省略=mDNS robot{id}.local)")
    ap.add_argument("--port-base", type=int, default=41000,
                    help="hid_bridge downlink port base (default 41000、実 port=base+id)")
    args = ap.parse_args()

    ids = [int(x) for x in args.robots.split(",") if x.strip()]
    if args.ssid is not None:
        ssid, password = args.ssid, (args.password or "")
    else:
        ssid, password = MODES[args.mode]
        if args.password is not None:
            password = args.password

    targets = args.targets.split(",") if args.targets else [None] * len(ids)
    if len(targets) != len(ids):
        sys.exit("[err] --targets の数が --robots と一致しません")

    msg = json.dumps({"type": "set_ssid", "ssid": ssid, "password": password}).encode()
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    print(f"[i] set_ssid -> ssid='{ssid}' password={'(open)' if not password else '****'}")
    for rid, ovr in zip(ids, targets):
        try:
            ip = resolve(rid, ovr)
        except socket.gaierror as e:
            print(f"    robot{rid}: 解決失敗 ({e}) — skip")
            continue
        port = args.port_base + rid
        sock.sendto(msg, (ip, port))
        print(f"    robot{rid} -> {ip}:{port}  sent")
    sock.close()
    print("[i] 完了。切替には数秒～十数秒。揮発 (HID 再起動で既定に戻る)。")


if __name__ == "__main__":
    main()
