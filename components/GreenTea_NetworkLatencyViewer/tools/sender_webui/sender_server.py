#!/usr/bin/env python3
# sender_server — RasPi4 上で動く独立 sender 制御 WebUI (FastAPI + uvicorn)。
#
# 目的: 実環境では「いつ・どのロボット宛ての送信が始まるか」が不定。本 UI で
# 送信先ロボットを動的に join/leave させ、計測系 (RasPi5) に対してロボットの
# 増減・負荷変動を任意に与える load generator。
#
# 実機 AI のセマンティクスを忠実に再現:
#   - 単一の「送信処理タスク」が周期 (cycle) ごとに回る。cycle_count は
#     パケットごとではなく **周期ごとに +1**。
#   - 1 周期でアクティブな全ロボットへ送信し、それらは **共通の cycle_count** を持つ
#     (downlink offset 51-53、24bit LE)。
#   - rate はグローバル (AI 送信タスクの周期レート)。ロボット別レートは「同時送信=
#     共通 cycle_count」と両立しないため設けない。
#   - cycle_count はサーバ起動から連続して進む (ロボット 0 体でも進行、実機ループと同じ)。
#     途中 join したロボットはその時点の cycle_count から受信開始。
#
# downlink packet は pc_emulator.build_downlink_packet を再利用
# (robot_comm_spec/downlink_command.md 準拠の 64B)。
#
# 起動:
#   ~/.venv-sender/bin/uvicorn sender_server:app --host 0.0.0.0 --port 8502
# (tools/sender_webui/ または配備先で。pc_emulator.py を import path に置くこと)

from __future__ import annotations

import asyncio
import socket
import sys
import threading
import time
from pathlib import Path

from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse, JSONResponse

# pc_emulator.build_downlink_packet を再利用。repo レイアウト (../pc_emulator) と
# フラット配備 (同一ディレクトリ) の両方に対応。
_HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(_HERE))
sys.path.insert(0, str(_HERE.parent / "pc_emulator"))
from pc_emulator import build_downlink_packet  # noqa: E402


def resolve_target(robot_id: int, spec: str | None) -> str:
    """送信先 IP を決める。spec が None/空なら robot{id}.local を mDNS 解決、
    それ以外は spec をそのまま (IP or ホスト名) 解決する。"""
    if spec and spec.strip():
        s = spec.strip()
        try:
            socket.inet_aton(s)
            return s  # 既に IPv4
        except OSError:
            return socket.gethostbyname(s)  # ホスト名
    return socket.gethostbyname(f"robot{robot_id}.local")


class Robot:
    """送信対象 1 体ぶんの宛先情報と送出統計。"""

    def __init__(self, robot_id: int, target_ip: str, port: int, target_spec: str | None):
        self.robot_id = robot_id
        self.target_ip = target_ip
        self.port = port
        self.target_spec = target_spec or "auto"
        self.sent = 0
        self.last_error: str | None = None
        self.joined_at = time.time()
        self.first_cycle: int | None = None  # join 時点の cycle_count

    def snapshot(self, now: float) -> dict:
        return {
            "robot_id": self.robot_id,
            "target": self.target_ip,
            "target_spec": self.target_spec,
            "port": self.port,
            "sent": self.sent,
            "uptime_s": round(now - self.joined_at, 1),
            "first_cycle": self.first_cycle,
            "last_error": self.last_error,
        }


class SenderManager:
    """単一のグローバル送信ループ。周期ごとに cycle_count を +1 し、その時点で
    アクティブな全ロボットへ共通 cycle_count で送る (実機 AI 送信タスク相当)。"""

    def __init__(self, rate: float = 100.0):
        self._robots: dict[int, Robot] = {}
        self._lock = threading.Lock()
        self._rate = rate
        self._cycle = 0  # グローバル cycle_count (周期ごと +1、24bit wrap)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._stop = threading.Event()
        self._thread = threading.Thread(target=self._run, daemon=True, name="send-cycle")

    def start(self) -> None:
        self._thread.start()

    def shutdown(self) -> None:
        self._stop.set()

    # ---- ロボット集合の操作 ----
    def add_robot(self, robot_id: int, *, target_spec: str | None, port: int | None) -> dict:
        with self._lock:
            if robot_id in self._robots:
                raise ValueError(f"robot {robot_id} は既に join 済 (remove してから再追加)")
            target_ip = resolve_target(robot_id, target_spec)
            p = port if port is not None else (40000 + robot_id)
            r = Robot(robot_id, target_ip, p, target_spec)
            r.first_cycle = self._cycle
            self._robots[robot_id] = r
            return r.snapshot(time.time())

    def remove_robot(self, robot_id: int) -> None:
        with self._lock:
            if robot_id not in self._robots:
                raise ValueError(f"robot {robot_id} は join していない")
            del self._robots[robot_id]

    def update_robot(self, robot_id: int, *, target_spec: str | None, port: int | None) -> dict:
        with self._lock:
            r = self._robots.get(robot_id)
            if r is None:
                raise ValueError(f"robot {robot_id} は join していない")
            if target_spec is not None and target_spec.strip():
                r.target_ip = resolve_target(robot_id, target_spec)
                r.target_spec = target_spec
            if port is not None:
                r.port = port
            return r.snapshot(time.time())

    def remove_all(self) -> int:
        with self._lock:
            n = len(self._robots)
            self._robots.clear()
            return n

    def set_rate(self, rate: float) -> None:
        if rate <= 0:
            raise ValueError("rate は正の値")
        with self._lock:
            self._rate = rate

    def state(self) -> dict:
        now = time.time()
        with self._lock:
            robots = [r.snapshot(now) for r in self._robots.values()]
            rate = self._rate
            cycle = self._cycle
        robots.sort(key=lambda r: r["robot_id"])
        return {
            "rate": rate,
            "cycle_count": cycle,
            "n_active": len(robots),
            "robots": robots,
            "pkts_per_s": rate * len(robots),  # 周期レート × ロボット数
            "now": now,
        }

    # ---- 送信ループ (1 周期ごと共通 cycle_count) ----
    def _run(self) -> None:
        next_send = time.monotonic()
        while not self._stop.is_set():
            with self._lock:
                rate = self._rate
                cc = self._cycle
                robots = list(self._robots.values())
            now_unix = time.time()
            for r in robots:
                pkt = build_downlink_packet(r.robot_id, now_unix, cycle_count=cc)
                try:
                    self._sock.sendto(pkt, (r.target_ip, r.port))
                    r.sent += 1
                except OSError as e:
                    r.last_error = str(e)  # ARP fail 等。継続
            with self._lock:
                self._cycle = (self._cycle + 1) & 0xFFFFFF  # 周期ごと +1、24bit wrap
            period = 1.0 / rate if rate > 0 else 1.0
            next_send += period
            now_mono = time.monotonic()
            sleep_dur = next_send - now_mono
            if sleep_dur > 0:
                self._stop.wait(sleep_dur)  # 割り込み可能 sleep
            else:
                next_send = now_mono  # 追いつかない時は catch-up しない


MANAGER = SenderManager(rate=100.0)
app = FastAPI(title="GreenTea Sender Control")


@app.on_event("startup")
async def _on_startup() -> None:
    MANAGER.start()


@app.get("/api/state")
async def api_state() -> JSONResponse:
    return JSONResponse(MANAGER.state())


@app.post("/api/add")
async def api_add(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        rid = int(body["robot_id"])
        target = body.get("target") or None
        port = int(body["port"]) if body.get("port") not in (None, "") else None
        snap = await asyncio.to_thread(
            MANAGER.add_robot, rid, target_spec=target, port=port)
        return JSONResponse({"ok": True, "robot": snap})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/remove")
async def api_remove(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        await asyncio.to_thread(MANAGER.remove_robot, int(body["robot_id"]))
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/update")
async def api_update(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        rid = int(body["robot_id"])
        target = body.get("target") if body.get("target") not in (None, "") else None
        port = int(body["port"]) if body.get("port") not in (None, "") else None
        snap = await asyncio.to_thread(
            MANAGER.update_robot, rid, target_spec=target, port=port)
        return JSONResponse({"ok": True, "robot": snap})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/rate")
async def api_rate(request: Request) -> JSONResponse:
    body = await request.json()
    try:
        await asyncio.to_thread(MANAGER.set_rate, float(body["rate"]))
        return JSONResponse({"ok": True})
    except Exception as e:
        return JSONResponse({"ok": False, "error": str(e)}, status_code=400)


@app.post("/api/remove_all")
async def api_remove_all() -> JSONResponse:
    n = await asyncio.to_thread(MANAGER.remove_all)
    return JSONResponse({"ok": True, "removed": n})


@app.get("/")
async def index() -> HTMLResponse:
    return HTMLResponse(INDEX_HTML)


INDEX_HTML = """<!DOCTYPE html>
<html lang="ja"><head>
<meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1">
<title>GreenTea Sender Control (RasPi4)</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 1.5rem; background:#0f1419; color:#d8dee4; }
  h1 { font-size: 1.3rem; } h2 { font-size: 1.05rem; margin-top:1.5rem; }
  table { border-collapse: collapse; width: 100%; margin-top:.5rem; }
  th, td { border: 1px solid #2a3340; padding: .4rem .6rem; text-align: right; }
  th { background:#1a2230; } td.l, th.l { text-align: left; }
  input { background:#1a2230; color:#d8dee4; border:1px solid #2a3340; border-radius:4px;
          padding:.3rem .4rem; width:8rem; }
  button { background:#2563eb; color:#fff; border:0; border-radius:4px; padding:.35rem .7rem;
           cursor:pointer; margin-left:.2rem; }
  button.rm { background:#dc2626; } button.upd { background:#0891b2; }
  button.allstop { background:#991b1b; }
  .form { margin-top:.6rem; padding:.7rem; background:#161d27; border-radius:6px; display:flex;
          gap:.5rem; align-items:flex-end; flex-wrap:wrap; }
  .form label { font-size:.85rem; display:flex; flex-direction:column; gap:.15rem; }
  .summary { margin-top:.6rem; font-size:.95rem; }
  .summary b { color:#60a5fa; }
  .err { color:#f87171; font-size:.85rem; min-height:1.1rem; margin-top:.3rem; }
  .mono { font-family: ui-monospace, monospace; }
</style></head>
<body>
<h1>GreenTea Sender Control <span style="font-size:.8rem;color:#7b8794">(RasPi4 load generator)</span></h1>

<div class="form">
  <label>送信周期レート (Hz, グローバル)<input id="g_rate" type="number" value="100" step="1"></label>
  <button onclick="setRate()">レート適用</button>
</div>
<div class="summary">アクティブ: <b id="nactive">0</b> robots
  &nbsp;|&nbsp; 周期レート: <b id="grate">0</b> Hz
  &nbsp;|&nbsp; 合計 <b id="pps">0</b> pkts/s
  &nbsp;|&nbsp; cycle_count: <b id="cc" class="mono">0</b>
  <button class="allstop" onclick="removeAll()">全 leave</button></div>
<div class="err" id="err"></div>

<h2>アクティブなロボット</h2>
<table><thead><tr>
  <th>robot_id</th><th class="l">送信先</th><th>port</th>
  <th>送出数</th><th>join時cc</th><th>稼働(s)</th><th class="l">操作</th>
</tr></thead><tbody id="rows"></tbody></table>

<h2>ロボット追加 (join)</h2>
<div class="form">
  <label>robot_id<input id="f_id" type="number" min="0" max="15" value="0"></label>
  <label>送信先 (空=mDNS robot{id}.local)<input id="f_target" placeholder="auto (mDNS)"></label>
  <label>port (空=40000+id)<input id="f_port" type="number" placeholder="auto"></label>
  <button onclick="addRobot()">join</button>
</div>

<script>
async function api(path, body){
  const opt = { method:'POST', headers:{'Content-Type':'application/json'} };
  if (body) opt.body = JSON.stringify(body);
  const r = await fetch(path, body ? opt : undefined);
  return await r.json();
}
function setErr(m){ document.getElementById('err').textContent = m || ''; }

async function setRate(){
  setErr('');
  const r = await api('/api/rate', {rate: document.getElementById('g_rate').value});
  if(!r.ok) setErr('レート変更失敗: '+r.error); refresh();
}
async function addRobot(){
  setErr('');
  const id = document.getElementById('f_id').value;
  const target = document.getElementById('f_target').value;
  const port = document.getElementById('f_port').value;
  const r = await api('/api/add', {robot_id:id, target, port});
  if(!r.ok) setErr('join 失敗: '+r.error);
  refresh();
}
async function removeRobot(id){ const r = await api('/api/remove', {robot_id:id});
  if(!r.ok) setErr('leave 失敗: '+r.error); refresh(); }
async function updRobot(id){
  setErr('');
  const target = document.getElementById('tgt_'+id).value;
  const r = await api('/api/update', {robot_id:id, target});
  if(!r.ok) setErr('更新失敗: '+r.error); refresh();
}
async function removeAll(){ await api('/api/remove_all', {}); refresh(); }

async function refresh(){
  let s;
  try { s = await (await fetch('/api/state')).json(); }
  catch(e){ return; }
  document.getElementById('nactive').textContent = s.n_active;
  document.getElementById('grate').textContent = s.rate;
  document.getElementById('pps').textContent = s.pkts_per_s;
  document.getElementById('cc').textContent = s.cycle_count;
  const rows = s.robots.map(r => `
    <tr>
      <td>${r.robot_id}</td>
      <td class="l mono"><input id="tgt_${r.robot_id}" value="${r.target}" style="width:9rem"></td>
      <td>${r.port}</td>
      <td class="mono">${r.sent}</td>
      <td class="mono">${r.first_cycle}</td>
      <td>${r.uptime_s}</td>
      <td class="l">
        <button class="upd" onclick="updRobot(${r.robot_id})">更新</button>
        <button class="rm" onclick="removeRobot(${r.robot_id})">leave</button>
        ${r.last_error?('<span style="color:#f87171">'+r.last_error+'</span>'):''}
      </td>
    </tr>`).join('');
  document.getElementById('rows').innerHTML = rows ||
    '<tr><td colspan="7" class="l" style="opacity:.5">アクティブなロボットなし (cycle は進行中)</td></tr>';
}
refresh();
setInterval(refresh, 1000);
</script>
</body></html>
"""
