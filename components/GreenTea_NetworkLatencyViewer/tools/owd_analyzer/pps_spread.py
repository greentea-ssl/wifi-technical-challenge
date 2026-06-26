#!/usr/bin/env python3
# 6台 HID PPS(metrics_raw type=pps t_pps_tsf_us)+ sniffer PPS(pps_uart.tsf_us)の
# 位相ジッタ φ=tsf-round(tsf/1e6)*1e6 を同一 AP-TSF 基準で比較。
import sqlite3,statistics,json,sys
LIVE="/dev/shm/gtnlv_live.db"
win=float(sys.argv[1]) if len(sys.argv)>1 else 120.0
c=sqlite3.connect(LIVE)
mx=c.execute("SELECT MAX(ingest_unix) FROM pps_uart").fetchone()[0]
lo,hi=mx-win,mx
def phi(tsf): 
    f=tsf-round(tsf/1e6)*1e6
    return f if abs(f)<200 else None
def stat(name,vals):
    v=[x for x in vals if x is not None]
    if len(v)<5: print(f"  {name}: n={len(v)} 標本不足"); return None
    print(f"  {name}: n={len(v)} mean={statistics.mean(v):+.1f} sd={statistics.pstdev(v):.1f} min/max={min(v):+.0f}/{max(v):+.0f} us")
    return v
print(f"=== PPS 位相ジッタ φ (直近{win:.0f}s, ±200us clip) ===")
# sniffer
sn=stat("sniffer ", [phi(r[0]) for r in c.execute("SELECT tsf_us FROM pps_uart WHERE ingest_unix BETWEEN ? AND ?",(lo,hi))])
# HID 6台 (metrics_raw type=pps)
hid={}
for rid,js in c.execute("SELECT robot_id,json FROM metrics_raw WHERE ingest_unix BETWEEN ? AND ?",(lo,hi)):
    if '"pps"' not in js: continue
    try: m=json.loads(js)
    except: continue
    if m.get("type")!="pps": continue
    t=m.get("t_pps_tsf_us")
    if t is not None: hid.setdefault(rid,[]).append(phi(t))
allh=[]
for rid in sorted(hid):
    v=stat(f"HID r{rid} ",hid[rid])
    if v: allh+=v
# 機間 Δ: 各 HID φ の sd と sniffer φ の sd を比較、全体 spread
print("--- まとめ ---")
if sn: print(f"  sniffer φ sd = {statistics.pstdev(sn):.1f} us")
if allh: print(f"  HID 全台 φ sd(プール) = {statistics.pstdev(allh):.1f} us")
c.close()
