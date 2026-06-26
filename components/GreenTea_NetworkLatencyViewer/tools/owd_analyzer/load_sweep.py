#!/usr/bin/env python3
# rx_dl/rx_dlb × 負荷スイープ: 各レートで 下りOWD(SPAN→HID) + PPS位相ジッタ を live DB から測る。
# 使い方: sudo python3 load_sweep.py <mode_label> [rate1 rate2 ...]
import sqlite3,bisect,statistics,math,time,sys,json,urllib.request
LIVE="/dev/shm/gtnlv_live.db"; SENDER="http://192.168.4.217:8502"
def post(u,d): 
    urllib.request.urlopen(urllib.request.Request(u,data=json.dumps(d).encode(),
        headers={"Content-Type":"application/json"},method="POST"),timeout=10).read()
def bridge(c,lo,hi):
    g=sorted(r[0] for r in c.execute("SELECT unix_assert FROM pps_gpio WHERE ingest_unix BETWEEN ? AND ?",(lo-8,hi+8)))
    u=c.execute("SELECT t_rpid_recv_unix,tsf_us FROM pps_uart WHERE ingest_unix BETWEEN ? AND ? ORDER BY 1",(lo-8,hi+8)).fetchall()
    P=[]
    for tr,tsf in u:
        i=bisect.bisect_left(g,tr); cd=[g[j] for j in (i-1,i) if 0<=j<len(g)]
        if cd and abs(tr-min(cd,key=lambda x:abs(x-tr)))<=0.3:
            a=min(cd,key=lambda x:abs(x-tr)); P.append((a,a-tsf/1e6))
    if len(P)<8: return None
    xs=[p[0] for p in P];ys=[p[1] for p in P];x0=xs[0];xr=[x-x0 for x in xs];n=len(xr)
    sx=sum(xr);sy=sum(ys);sxx=sum(x*x for x in xr);sxy=sum(x*y for x,y in zip(xr,ys))
    sl=(n*sxy-sx*sy)/(n*sxx-sx*sx);it=(sy-sl*sx)/n
    return lambda t: it+sl*(t-x0)
def measure(c,lo,hi,off):
    wm={}
    for rid,cc,tw in c.execute("SELECT robot_id,cycle_count,min(t_wire_phc) FROM wire WHERE ingest_unix BETWEEN ? AND ? AND t_wire_phc>1e9 AND cycle_count IS NOT NULL GROUP BY robot_id,cycle_count",(lo,hi)):
        wm[(rid,cc)]=tw
    ow=[]
    for rid,cc,trx in c.execute("SELECT robot_id,cycle_count,t_hid_rx_tsf_us FROM rx_dl WHERE ingest_unix BETWEEN ? AND ? AND t_hid_rx_tsf_us IS NOT NULL AND cycle_count IS NOT NULL",(lo,hi)):
        tw=wm.get((rid,cc))
        if tw is None: continue
        v=(trx/1e6+off(tw)-tw)*1000
        if -50<v<20000: ow.append(v)
    # PPS 位相ジッタ
    ph=[]
    for (tsf,) in c.execute("SELECT tsf_us FROM pps_uart WHERE ingest_unix BETWEEN ? AND ?",(lo,hi)):
        f=tsf-round(tsf/1e6)*1e6
        if abs(f)<200: ph.append(f)   # ±200µs クリップ(再同期飛び除外)
    return ow,ph
def main():
    mode=sys.argv[1] if len(sys.argv)>1 else "?"
    rates=[int(x) for x in sys.argv[2:]] or [60,100,200,300]
    print(f"########## load sweep mode={mode} ##########",flush=True)
    print("rate | DL n | OWD mean | var | med | p99 | maxms | PPS n | sd(phi)us",flush=True)
    for r in rates:
        post(SENDER+"/api/rate",{"rate":r}); time.sleep(5)
        t0=time.time(); time.sleep(30); t1=time.time()
        c=sqlite3.connect(LIVE); off=bridge(c,t0+2,t1)
        if off is None: print(f"{r} | bridge不可"); c.close(); continue
        ow,ph=measure(c,t0+2,t1,off); c.close()
        if len(ow)<30: print(f"{r} | OWD標本不足 n={len(ow)}"); continue
        s=sorted(ow);n=len(s)
        sdphi=statistics.pstdev(ph) if len(ph)>5 else float('nan')
        print(f"{r} | {n} | {statistics.mean(ow):.2f} | {statistics.variance(ow):.2f} | {s[n//2]:.2f} | {s[int(n*0.99)]:.2f} | {s[-1]:.0f} | {len(ph)} | {sdphi:.1f}",flush=True)
    post(SENDER+"/api/rate",{"rate":60})
    print("done",flush=True)
main()
