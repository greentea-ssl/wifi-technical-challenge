#!/usr/bin/env python3
# daemon A (gtnlv-rpid) の録画制御 unix socket client。
#
# WebUI の Start/Stop Recording ボタンが叩く。daemon 側 socket は
# docs/measurement_pipeline_v2.md §5.6 の P1 で実装予定。socket が
# 不在でも graceful に {"available": false} を返し、UI を壊さない。
#
# プロトコル: 1 行 JSON request → 1 行 JSON response。
#   {"cmd": "start_record", "tag": "..."} / {"cmd": "stop_record"} / {"cmd": "status"}

from __future__ import annotations

import json
import os
import socket

# daemon A が listen する unix socket。env で上書き可。
SOCK_PATH = os.environ.get("GTNLV_CTRL_SOCK", "/run/gtnlv-rpid.sock")
TIMEOUT_S = 20.0


def _send(req: dict) -> dict:
    if not os.path.exists(SOCK_PATH):
        return {"available": False, "error": f"control socket not found: {SOCK_PATH}"}
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as s:
            s.settimeout(TIMEOUT_S)
            s.connect(SOCK_PATH)
            s.sendall((json.dumps(req) + "\n").encode())
            buf = b""
            while b"\n" not in buf:
                chunk = s.recv(4096)
                if not chunk:
                    break
                buf += chunk
        resp = json.loads(buf.decode().splitlines()[0]) if buf.strip() else {}
        resp.setdefault("available", True)
        return resp
    except (OSError, ValueError) as e:
        return {"available": False, "error": str(e)}


def start_record(tag: str | None = None) -> dict:
    return _send({"cmd": "start_record", "tag": tag})


def stop_record() -> dict:
    return _send({"cmd": "stop_record"})


def status() -> dict:
    return _send({"cmd": "status"})


def sniffer_cfg(ssid: str, password: str = "") -> dict:
    """sniffer の捕捉対象 AP (SSID) を切替える。daemon → UART → sniffer 再 associate。"""
    return _send({"cmd": "sniffer_cfg", "ssid": ssid, "password": password})
