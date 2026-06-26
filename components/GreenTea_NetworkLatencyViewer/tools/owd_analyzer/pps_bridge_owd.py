"""真の下り OWD (PPS TSF bridge): pps_uart↔pps_gpio を時刻近接でペア → 線形drift除去した
bridge_offset(t) → rx_dl の t_hid_rx_tsf に適用。per-robot OWD を 100Hz/60Hz で算出。"""
import duckdb, bisect, statistics, sys

def build_bridge(run):
    con=duckdb.connect()
    g=sorted(r[0] for r in con.execute(f"SELECT unix_assert FROM read_parquet('{run}/pps_gpio/*.parquet')").fetchall())
    u=con.execute(f"SELECT t_rpid_recv_unix, tsf_us FROM read_parquet('{run}/pps_uart/*.parquet') ORDER BY t_rpid_recv_unix").fetchall()
    con.close()
    pairs=[]  # (unix_assert, offset, uart_delay)
    for trecv, tsf in u:
        # t_recv に最も近い gpio assert (UART遅延ぶん t_recv は assert よりやや後)
        i=bisect.bisect_left(g, trecv)
        cand=[g[j] for j in (i-1,i) if 0<=j<len(g)]
        if not cand: continue
        ua=min(cand, key=lambda x:abs(x-trecv))
        delay=trecv-ua
        if abs(delay)>0.3: continue          # 0.3s 超は別エッジ誤対応 → 除外
        pairs.append((ua, ua - tsf/1e6, delay))
    if len(pairs)<30: return None
    # 線形 drift 除去 (offset vs unix_assert)
    xs=[p[0] for p in pairs]; ys=[p[1] for p in pairs]; x0=xs[0]
    xr=[x-x0 for x in xs]; n=len(xr)
    sx=sum(xr); sy=sum(ys); sxx=sum(x*x for x in xr); sxy=sum(x*y for x,y in zip(xr,ys))
    slope=(n*sxy-sx*sy)/(n*sxx-sx*sx); inter=(sy-slope*sx)/n
    resid=[(y-(inter+slope*x))*1e6 for x,y in zip(xr,ys)]  # us
    # 外れ値(>3sd)除去して再fit
    m=statistics.mean(resid); sd=statistics.pstdev(resid)
    keep=[(x,y) for x,y,r in zip(xr,ys,resid) if abs(r-m)<3*sd]
    xr2=[k[0] for k in keep]; ys2=[k[1] for k in keep]; n2=len(xr2)
    sx=sum(xr2); sy=sum(ys2); sxx=sum(x*x for x in xr2); sxy=sum(x*y for x,y in zip(xr2,ys2))
    slope=(n2*sxy-sx*sy)/(n2*sxx-sx*sx); inter=(sy-slope*sx)/n2
    resid2=[(y-(inter+slope*x))*1e6 for x,y in zip(xr2,ys2)]
    return {"x0":x0,"slope":slope,"inter":inter,"resid_sd":statistics.pstdev(resid2),
            "n":n2,"drift_ppm":slope*1e6,"uart_delay_med_ms":statistics.median([p[2] for p in pairs])*1000}

def offset_at(b, t): return b["inter"] + b["slope"]*(t-b["x0"])

def _bridge_pairs(run):
    """build_bridge と同一の pps_uart↔pps_gpio 近接ペアリング (再利用用)。
    返り値: [(unix_assert, offset_sec, uart_delay_sec), ...] 時刻順。"""
    con=duckdb.connect()
    g=sorted(r[0] for r in con.execute(f"SELECT unix_assert FROM read_parquet('{run}/pps_gpio/*.parquet')").fetchall())
    u=con.execute(f"SELECT t_rpid_recv_unix, tsf_us FROM read_parquet('{run}/pps_uart/*.parquet') ORDER BY t_rpid_recv_unix").fetchall()
    con.close()
    pairs=[]
    for trecv, tsf in u:
        i=bisect.bisect_left(g, trecv)
        cand=[g[j] for j in (i-1,i) if 0<=j<len(g)]
        if not cand: continue
        ua=min(cand, key=lambda x:abs(x-trecv)); delay=trecv-ua
        if abs(delay)>0.3: continue
        pairs.append((ua, ua - tsf/1e6, delay))
    return pairs

def _fit(xr, ys):
    """線形 fit + 3sd 外れ値除去 + 再fit。返り値 (inter, slope, resid_sd_us, n)。"""
    n=len(xr)
    if n<10: return None
    sx=sum(xr); sy=sum(ys); sxx=sum(x*x for x in xr); sxy=sum(x*y for x,y in zip(xr,ys))
    den=n*sxx-sx*sx
    if den==0: return None
    slope=(n*sxy-sx*sy)/den; inter=(sy-slope*sx)/n
    resid=[(y-(inter+slope*x))*1e6 for x,y in zip(xr,ys)]
    m=statistics.mean(resid); sd=statistics.pstdev(resid)
    keep=[(x,y) for x,y,r in zip(xr,ys,resid) if abs(r-m)<3*sd]
    if len(keep)<10: keep=list(zip(xr,ys))
    xr2=[k[0] for k in keep]; ys2=[k[1] for k in keep]; n2=len(xr2)
    sx=sum(xr2); sy=sum(ys2); sxx=sum(x*x for x in xr2); sxy=sum(x*y for x,y in zip(xr2,ys2))
    den=n2*sxx-sx*sx
    slope=(n2*sxy-sx*sy)/den; inter=(sy-slope*sx)/n2
    resid2=[(y-(inter+slope*x))*1e6 for x,y in zip(xr2,ys2)]
    return inter, slope, statistics.pstdev(resid2), n2

def build_bridge_windowed(run, win_s=600.0):
    """区間線形 bridge: pairs を win_s 秒窓に分割し各窓で線形 fit。
    crystal drift の非線形分を窓ごとに吸収し、長時間 run の残差を縮める。
    返り値: {"segs":[(t_start,t_end,inter,slope,x0),...], "win_s":win_s,
             "resid_sd":(全窓 concat residual sd), "per_win":[(t_off,n,sd_us,ppm),...]}"""
    pairs=_bridge_pairs(run)
    if len(pairs)<30: return None
    t0=pairs[0][0]
    segs=[]; per_win=[]; all_resid=[]
    import collections
    buckets=collections.defaultdict(list)
    for ua,off,_ in pairs: buckets[int((ua-t0)//win_s)].append((ua,off))
    for b in sorted(buckets):
        ps=buckets[b]; x0=ps[0][0]; xr=[p[0]-x0 for p in ps]; ys=[p[1] for p in ps]
        f=_fit(xr, ys)
        if not f: continue
        inter, slope, sd, n2 = f
        segs.append((ps[0][0], ps[-1][0], inter, slope, x0))
        per_win.append((b*win_s, n2, sd, slope*1e6))
        all_resid.append(sd)
    overall=statistics.mean(all_resid) if all_resid else None  # 窓平均 sd
    return {"segs":segs, "win_s":win_s, "resid_sd":overall, "per_win":per_win}

def offset_at_win(wb, t):
    """windowed bridge の offset (t を含む/最近傍の窓の線形式)。"""
    best=None; bd=None
    for ts,te,inter,slope,x0 in wb["segs"]:
        if ts<=t<=te: return inter+slope*(t-x0)
        d=min(abs(t-ts),abs(t-te))
        if bd is None or d<bd: bd=d; best=(inter,slope,x0)
    return best[0]+best[1]*(t-best[2]) if best else 0.0

def owd(run, hz):
    b=build_bridge(run)
    print(f"\n=== {hz} ({run.split('/')[-1]}) ===")
    if not b:
        print("  bridge 構築不可"); return
    print(f"  bridge: drift={b['drift_ppm']:.2f}ppm  残差sd={b['resid_sd']:.1f}us (計測精度)  uart_delay_med={b['uart_delay_med_ms']:.2f}ms  pairs={b['n']}")
    con=duckdb.connect()
    rx=f"read_parquet('{run}/rx_dl/*.parquet', union_by_name=true)"
    print("  robot | bridge OWD med/p95/p99/max (ms) | mean | var(ms^2)")
    meds=[]
    for rid in range(1,7):
        rows=con.execute(f"SELECT corr_unix_time, t_hid_rx_tsf_us FROM {rx} WHERE robot_id={rid} AND t_hid_rx_tsf_us IS NOT NULL AND corr_unix_time IS NOT NULL").fetchall()
        ow=[]
        for corr,tsf in rows:
            v=(tsf/1e6 + offset_at(b,corr) - corr)*1000  # ms
            if abs(v)<2000: ow.append(v)
        if not ow: continue
        s=sorted(ow); nn=len(s)
        med=s[nn//2]; p95=s[int(nn*0.95)]; p99=s[int(nn*0.99)]; mx=s[-1]
        mean=statistics.mean(ow); var=statistics.pvariance(ow)
        meds.append(med)
        print(f"   r{rid}  | {med:6.2f}/{p95:6.2f}/{p99:6.2f}/{mx:7.2f} | {mean:6.2f} | {var:8.1f}")
    con.close()
    print(f"  [全robot平均] bridge OWD median={statistics.mean(meds):.2f}ms")
    return b, statistics.mean(meds)

owd("/mnt/nas/gtnlv/runs/longrun_v208_0622_0938","100Hz")
owd("/mnt/nas/gtnlv/runs/longrun_v208_60hz_0622_1038","60Hz")
