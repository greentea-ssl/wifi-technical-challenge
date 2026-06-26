#!/usr/bin/env python3
# GreenTea Network Latency Viewer — stdlib のみの HTTP/SSE サーバ。
#
# fastapi/uvicorn が無い環境 (RasPi system python 等) でも動くよう、
# http.server だけで server.py 相当の API を提供する。datasource.py /
# control.py は stdlib のみなので追加 install 不要。
#
# 起動 (RasPi 上、tmpfs SQLite を読むので RasPi で動かす):
#   python3 tools/dashboard/serve.py --port 8501
# ブラウザ: http://<host>:8501  (tailscale 100.85.248.111:8501 等)

from __future__ import annotations

import argparse
import json
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse, parse_qs

import control
import datasource as ds

STATIC = Path(__file__).resolve().parent / "static"
SSE_INTERVAL_S = 1.0
_CTYPE = {".html": "text/html; charset=utf-8", ".js": "application/javascript",
          ".css": "text/css", ".json": "application/json"}


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def _send(self, code, ctype, body: bytes):
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code=200):
        self._send(code, "application/json", json.dumps(obj).encode())

    def do_GET(self):
        u = urlparse(self.path)
        path, q = u.path, parse_qs(u.query)
        if path == "/":
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path == "/api/runs":
            return self._json({"runs": ds.list_runs(), "active": ds.find_active_run()})
        if path == "/api/summary":
            return self._json(ds.compute_summary(q.get("run", [""])[0]))
        if path == "/api/live_summary":
            return self._json(ds.live_summary_sqlite())
        if path == "/api/csv_files":
            return self._json({"files": ds.list_csv_files(q.get("run", [""])[0])})
        if path == "/api/record/status":
            return self._json(control.status())
        if path == "/api/stream/live":
            return self._sse()
        self._send(404, "text/plain", b"not found")

    def do_POST(self):
        path = urlparse(self.path).path
        ln = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(ln) if ln else b""
        try:
            data = json.loads(raw) if raw else {}
        except ValueError:
            data = {}
        if path == "/api/record/start":
            return self._json(control.start_record(data.get("tag")))
        if path == "/api/record/stop":
            return self._json(control.stop_record())
        self._send(404, "text/plain", b"not found")

    def _static(self, rel):
        p = (STATIC / rel).resolve()
        if not str(p).startswith(str(STATIC)) or not p.is_file():
            return self._send(404, "text/plain", b"not found")
        self._send(200, _CTYPE.get(p.suffix.lower(), "application/octet-stream"), p.read_bytes())

    def _sse(self):
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("Connection", "keep-alive")
        self.end_headers()
        was_active = False
        try:
            while True:
                if ds.live_db_active():
                    payload = {"active": "live (SQLite)", "reset": not was_active,
                               "live": ds.compute_live_sqlite()}
                    was_active = True
                else:
                    payload = {"active": None}
                    was_active = False
                self.wfile.write(f"data: {json.dumps(payload)}\n\n".encode())
                self.wfile.flush()
                time.sleep(SSE_INTERVAL_S)
        except (BrokenPipeError, ConnectionResetError):
            return


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--host", default="0.0.0.0")
    ap.add_argument("--port", type=int, default=8501)
    args = ap.parse_args()
    srv = ThreadingHTTPServer((args.host, args.port), Handler)
    print(f"[gui] http://{args.host}:{args.port}  static={STATIC}", file=sys.stderr)
    print(f"[gui] live DB = {ds.LIVE_DB}", file=sys.stderr)
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
