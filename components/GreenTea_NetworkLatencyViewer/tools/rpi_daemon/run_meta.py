#!/usr/bin/env python3
# run_meta — 録画 run ディレクトリに「測定条件マニフェスト」run_meta.json を残す。
#
# 目的: NAS 上の測定データ (Parquet/CSV) と、その時の条件 (いつ・どの robot・どの AP/ch・
# firmware 版・同期状態・トポロジ・自由記述メモ) を**同じ run dir に併置**し、後解析時に
# 再現条件を一意に辿れるようにする。daemon 起動時 (--record 指定時) に best-effort で 1 回書く。
#
# 全項目 best-effort: 取得失敗してもラン自体は止めない (例外は握る)。

from __future__ import annotations
import json
import os
import shutil
import subprocess
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]   # tools/rpi_daemon/run_meta.py → repo root
SANREI = Path.home() / "SanRei_HID"


def _run(cmd, cwd=None, timeout=8):
    try:
        out = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True,
                             timeout=timeout)
        return out.stdout.strip()
    except Exception:
        return None


def _git_commit(repo: Path):
    if not (repo / ".git").exists():
        return None
    line = _run(["git", "log", "-1", "--format=%h %ci %s"], cwd=str(repo))
    return line or None


def _chrony():
    txt = _run(["chronyc", "tracking"])
    if not txt:
        return None
    d = {}
    for ln in txt.splitlines():
        if ":" in ln:
            k, v = ln.split(":", 1)
            k = k.strip()
            if k in ("Stratum", "Last offset", "RMS offset", "Frequency", "Reference ID"):
                d[k] = v.strip()
    return d or None


def _wifi_link(iface="wlan0"):
    """現在の wlan0 アソシエーション (管理IF)。軽量 (scan しない)。"""
    txt = _run(["iw", "dev", iface, "link"])
    if not txt or "Not connected" in txt:
        return None
    d = {}
    for ln in txt.splitlines():
        ln = ln.strip()
        if ln.startswith("SSID:"):
            d["ssid"] = ln.split(":", 1)[1].strip()
        elif ln.startswith("freq:"):
            d["freq_mhz"] = ln.split(":", 1)[1].strip()
        elif ln.startswith("Connected to"):
            d["bssid"] = ln.split()[2]
        elif "signal:" in ln:
            d["signal"] = ln.split("signal:", 1)[1].strip()
    return d or None


def _ip_addr():
    out = {}
    for ifc in ("eth0", "eth1", "wlan0", "wwan0"):
        line = _run(["ip", "-br", "-4", "addr", "show", ifc])
        if line:
            parts = line.split()
            out[ifc] = parts[2] if len(parts) > 2 else parts[-1]
    return out


def gather(args=None, sniffer_ssid=None, extra_notes=None):
    """測定条件を収集して dict で返す。全 best-effort。"""
    meta = {
        "schema": "gtnlv-run-meta/1",
        "captured_at": _run(["date", "-Iseconds"]) or "",
        "captured_at_unix": int(time.time()) if hasattr(time, "time") else None,
        "host": {
            "hostname": _run(["hostname"]),
            "kernel": _run(["uname", "-r"]),
        },
        "git": {
            "GreenTea_NetworkLatencyViewer": _git_commit(REPO),
            "SanRei_HID": _git_commit(SANREI),
        },
        "sync": {
            "chrony": _chrony(),
        },
        "network": {
            "wlan0_link": _wifi_link("wlan0"),
            "sniffer_ssid": sniffer_ssid,   # 計測対象 AP (sniffer が追従中の SSID)
            "interfaces": _ip_addr(),
        },
    }
    if args is not None:
        # daemon CLI 条件 (robot 数・wire/SPAN 有無・録画形式 等)
        a = vars(args) if not isinstance(args, dict) else args
        meta["daemon"] = {
            "robot_ids": a.get("robot_ids"),
            "wire_span": a.get("wire"),
            "sniffer_port": a.get("sniffer_port"),
            "sniffer_baud": a.get("sniffer_baud"),
            "pps_device": a.get("pps_device"),
            "record_format": a.get("record_format"),
            "record_dir": a.get("record"),
            "duration": a.get("duration"),
        }
    if extra_notes:
        meta["conditions_note"] = extra_notes   # 自由記述 (--run-note): AP機種/ch/hub段数/負荷条件等
    return meta


def write_run_meta(record_dir, args=None, sniffer_ssid=None, extra_notes=None):
    """run_meta.json を record_dir 直下に書く (NAS マウント先でも可)。best-effort。"""
    try:
        rd = Path(record_dir)
        rd.mkdir(parents=True, exist_ok=True)
        meta = gather(args=args, sniffer_ssid=sniffer_ssid, extra_notes=extra_notes)
        # tmpfs に書いてから move (NAS への部分書き込み回避)
        tmp = Path("/dev/shm") / f".run_meta_{os.getpid()}.json"
        tmp.write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        try:
            shutil.move(str(tmp), str(rd / "run_meta.json"))
        except OSError:
            (rd / "run_meta.json").write_text(json.dumps(meta, ensure_ascii=False, indent=2))
        return str(rd / "run_meta.json")
    except Exception:
        return None


if __name__ == "__main__":
    import sys
    note = sys.argv[2] if len(sys.argv) > 2 else None
    d = sys.argv[1] if len(sys.argv) > 1 else "/tmp/run_meta_test"
    p = write_run_meta(d, extra_notes=note)
    print("wrote:", p)
    print(Path(p).read_text() if p else "(failed)")
