#!/usr/bin/env python3
# GreenTea Network Latency Viewer — FastAPI + SSE バックエンド。
#
# Streamlit (app.py) を置き換える push 型 WebUI。full rerun を廃し、
# サーバが active run の CSV tail を ~1s ごとに集計して SSE で push する。
# フロントは uPlot (時系列/ヒスト) + inline SVG (網図) で差分描画。
#
# 起動:
#   uvicorn server:app --host 0.0.0.0 --port 8501   (tools/dashboard/ で)
#   または tools/dashboard/run_dashboard.sh
#
# データ源は現行 gtnlv-rpid の tmpfs/out CSV (datasource.py)。将来
# SQLite live store に移す場合も datasource.py 差し替えのみ。
# 録画制御は control.py 経由で daemon A の unix socket (未実装なら graceful)。

from __future__ import annotations

import asyncio
import json
import subprocess
from pathlib import Path

from fastapi import FastAPI, Query, Request
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles

import control
import datasource as ds

STATIC_DIR = Path(__file__).resolve().parent / "static"
SSE_INTERVAL_S = 1.0  # live 集計 push 周期

app = FastAPI(title="GreenTea Network Latency Viewer")


@app.middleware("http")
async def _no_cache(request: Request, call_next):
    # ブラウザの古い index.html/JS キャッシュで Live が更新されない事象を防ぐ
    resp = await call_next(request)
    resp.headers["Cache-Control"] = "no-store, must-revalidate"
    return resp


@app.get("/")
async def index() -> FileResponse:
    return FileResponse(STATIC_DIR / "index.html",
                        headers={"Cache-Control": "no-store, must-revalidate"})


@app.get("/api/runs")
async def api_runs() -> JSONResponse:
    runs = await asyncio.to_thread(ds.list_runs)
    active = await asyncio.to_thread(ds.find_active_run)
    return JSONResponse({"runs": runs, "active": active})


@app.get("/api/summary")
async def api_summary(run: str = Query(...)) -> JSONResponse:
    data = await asyncio.to_thread(ds.compute_summary, run)
    return JSONResponse(data)


@app.get("/api/csv_files")
async def api_csv_files(run: str = Query(...)) -> JSONResponse:
    return JSONResponse({"files": await asyncio.to_thread(ds.list_csv_files, run)})


@app.get("/api/live_summary")
async def api_live_summary() -> JSONResponse:
    """live SQLite store の 7 項目相当 + PPS bridge 精度 (Stage 5 解析)。"""
    return JSONResponse(await asyncio.to_thread(ds.live_summary_sqlite))


@app.get("/api/live_per_robot")
async def api_live_per_robot() -> JSONResponse:
    """robot_id 別の下り OWD (PPS bridge/approx) と損失 (直近窓)。1台毎表示用。"""
    return JSONResponse(await asyncio.to_thread(ds.live_per_robot_sqlite))


@app.get("/api/stream/live")
async def api_stream_live(request: Request) -> StreamingResponse:
    """active run の Live 集計を SSE で push。クライアント切断で停止。"""
    async def gen():
        was_active = False
        while True:
            if await request.is_disconnected():
                break
            active = await asyncio.to_thread(ds.live_db_active)
            if not active:
                payload = {"active": None}
                was_active = False
            else:
                live = await asyncio.to_thread(ds.compute_live_sqlite)
                payload = {"active": "live (SQLite)", "reset": not was_active, "live": live}
                was_active = True
            yield f"data: {json.dumps(payload)}\n\n"
            await asyncio.sleep(SSE_INTERVAL_S)

    return StreamingResponse(
        gen(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# ----- 録画制御 (daemon A unix socket、未実装でも graceful) -----
# 録画開始時に ADALM2000 PPS skew ロガーも連動起動し、録画 run dir へ pps_skew.csv を出す。
# libm2k は system python3 + root(libiio USB) 必須なので sudo python3 で spawn。ADALM2000
# 不在ならロガーは即終了 (録画本体は継続)。
_PPS_LOGGER = Path(__file__).resolve().parent / "pps_skew_logger.py"
_pps_proc = None


def _start_pps_logger(record_dir: str):
    global _pps_proc
    _stop_pps_logger()
    if not record_dir or not _PPS_LOGGER.exists():
        return
    try:
        _pps_proc = subprocess.Popen(
            ["sudo", "python3", str(_PPS_LOGGER), record_dir],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        _pps_proc = None


def _stop_pps_logger():
    global _pps_proc
    # sudo の子プロセスは sudo pkill で確実に止める ([p] で自己マッチ回避)
    try:
        subprocess.run(["sudo", "pkill", "-9", "-f", "[p]ps_skew_logger.py"],
                       timeout=5, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass
    _pps_proc = None


@app.post("/api/record/start")
async def api_record_start(request: Request) -> JSONResponse:
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    resp = await asyncio.to_thread(control.start_record, body.get("tag"))
    # 録画開始成功時に PPS skew ロガーも連動 (record_dir へ pps_skew.csv)。
    # body.pps が false なら ADALM2000 ロガーは起動しない (UI トグル)。
    pps_enabled = body.get("pps", True)
    if isinstance(resp, dict) and resp.get("recording") and resp.get("record_dir"):
        if pps_enabled:
            await asyncio.to_thread(_start_pps_logger, resp["record_dir"])
            resp["pps_logger"] = True
        else:
            resp["pps_logger"] = False
    return JSONResponse(resp)


@app.post("/api/record/stop")
async def api_record_stop() -> JSONResponse:
    await asyncio.to_thread(_stop_pps_logger)
    return JSONResponse(await asyncio.to_thread(control.stop_record))


@app.get("/api/record/status")
async def api_record_status() -> JSONResponse:
    return JSONResponse(await asyncio.to_thread(control.status))


# ----- sniffer 対象 AP (SSID) 切替 (daemon ctrl socket → UART → sniffer 再 associate) -----
@app.post("/api/sniffer/ssid")
async def api_sniffer_ssid(request: Request) -> JSONResponse:
    body = {}
    try:
        body = await request.json()
    except Exception:
        pass
    ssid = (body.get("ssid") or "").strip()
    if not ssid:
        return JSONResponse({"ok": False, "error": "ssid 必須"}, status_code=400)
    return JSONResponse(await asyncio.to_thread(
        control.sniffer_cfg, ssid, body.get("password") or ""))


# 静的アセット (/static/...)
app.mount("/static", StaticFiles(directory=STATIC_DIR), name="static")
