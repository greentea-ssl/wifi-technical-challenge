#!/usr/bin/env python3
# SSID 切替時間測定: set_ssid で open<->normal をトグルし、各切替の per-robot outage
# (rx_dl 受信ギャップ) と 欠落 cycle 数を live DB から測る。
import sqlite3,time,sys,subprocess,statistics
LIVE="/dev/shm/gtnlv_live.db"; IDS=[1,2,3,4,5,6]
SETSSID="/home/gochiuma/GreenTea_NetworkLatencyViewer/tools/set_ssid/set_ssid.py"
def trigger(mode):
    subprocess.run(["python3",SETSSID,"--robots",",".join(map(str,IDS)),"--mode",mode],
                   timeout=15,capture_output=True)
def outage(c,rid,t_sw,win=12.0,rate=60.0):
    # cycle_count(sender 単調カウンタ)の最大ギャップ÷rate = 真の outage(batch flush 非依存)
    rows=[r[0] for r in c.execute("SELECT cycle_count FROM rx_dl WHERE robot_id=? AND t_rpid_recv_unix BETWEEN ? AND ? AND cycle_count IS NOT NULL ORDER BY t_rpid_recv_unix",(rid,t_sw-3,t_sw+win))]
    if len(rows)<5: return None,None
    cs=sorted(set(rows))
    gaps=[(cs[i]-cs[i-1]) for i in range(1,len(cs))]
    miss=max(gaps)-1
    return miss/rate, miss
def main():
    seq=sys.argv[1:] or ["normal","open","normal","open"]
    print(f"########## SSID 切替時間 (seq={seq}) ##########",flush=True)
    print("switch_to | "+" | ".join(f"r{i}" for i in IDS)+" | mean | max",flush=True)
    res={}
    for mode in seq:
        t_sw=time.time(); trigger(mode); time.sleep(14)
        c=sqlite3.connect(LIVE)
        outs=[]
        for rid in IDS:
            g,_=outage(c,rid,t_sw); outs.append(g)
        c.close()
        ok=[o for o in outs if o is not None]
        line=" | ".join(f"{o:.2f}" if o is not None else "--" for o in outs)
        m=statistics.mean(ok) if ok else float('nan'); mx=max(ok) if ok else float('nan')
        print(f"->{mode} | {line} | {m:.2f} | {mx:.2f}",flush=True)
        res.setdefault(mode,[]).extend(ok)
    print("--- 集計 (切替先別 outage 秒) ---",flush=True)
    for mode,vals in res.items():
        if vals: print(f"->{mode}: n={len(vals)} mean={statistics.mean(vals):.2f}s max={max(vals):.2f}s"+(f" sd={statistics.pstdev(vals):.2f}" if len(vals)>1 else ""),flush=True)
    print("done",flush=True)
main()
