"""区間別 OWD: 下り(total bridge / air→HID / host→air) + 上り(HID→air)。
join: 下り=(robot_id,cycle_count) rx_dl×sniffer(dst=HID), 上り=(robot_id,hid_seq) rx_dl×sniffer(src=HID,ToDS)."""
import duckdb, bisect, statistics
exec(open("bridge_owd.py").read().split("\ndef owd")[0])  # build_bridge/offset_at

def st(v):
    v=[x for x in v if -50<x<3000]; s=sorted(v); n=len(s)
    if n<50: return None
    return dict(n=n, median=s[n//2], mean=statistics.mean(v), p95=s[int(n*0.95)], p99=s[int(n*0.99)], max=s[-1], var=statistics.pvariance(v))

def fmt(d): return f"med={d['median']:.2f} mean={d['mean']:.2f} p99={d['p99']:.2f} max={d['max']:.1f} var={d['var']:.0f} (n={d['n']})" if d else "—"

def analyze(run, hz):
    b=build_bridge(run); con=duckdb.connect()
    rx=f"read_parquet('{run}/rx_dl/*.parquet',union_by_name=true)"
    sf=f"read_parquet('{run}/sniffer_frame/*.parquet')"
    inter,slope,x0=b["inter"],b["slope"],b["x0"]
    print(f"\n=== {hz} ({run.split('/')[-1]}) bridge残差sd={b['resid_sd']:.0f}us ===")
    # --- 下り ---
    tot=[]; ah=[]   # total(bridge), air→HID
    rows=con.execute(f"""SELECT r.corr_unix_time, r.t_hid_rx_tsf_us, s.tsf_us
       FROM {rx} r JOIN (SELECT robot_id,cycle_count,min(tsf_us) tsf_us FROM {sf} WHERE dst LIKE '38:44%' GROUP BY robot_id,cycle_count) s
         ON s.robot_id=r.robot_id AND s.cycle_count=r.cycle_count
       WHERE r.t_hid_rx_tsf_us IS NOT NULL""").fetchall()
    for corr,hidrx,air in rows:
        off=inter+slope*(corr-x0)
        tot.append((hidrx/1e6+off-corr)*1000)      # total host→HID
        ah.append((hidrx-air)/1000.0)              # air→HID (AP空中→HID, AP TSF直接)
    # host→air = total - air→HID
    hostair=[t-a for t,a in zip(tot,ah)]
    print(f"  [下り] total(host→HID): {fmt(st(tot))}")
    print(f"  [下り] host→air      : {fmt(st(hostair))}")
    print(f"  [下り] air→HID       : {fmt(st(ah))}")
    # --- 上り HID→air ---
    ua=[]
    rows=con.execute(f"""SELECT r.t_hid_tx_tsf_us, s.tsf_us
       FROM {rx} r JOIN (SELECT robot_id,cycle_count,min(tsf_us) tsf_us FROM {sf} WHERE src LIKE '38:44%' GROUP BY robot_id,cycle_count) s
         ON s.robot_id=r.robot_id AND s.cycle_count=r.hid_seq
       WHERE r.t_hid_tx_tsf_us IS NOT NULL""").fetchall()
    for hidtx,air in rows:
        ua.append((air-hidtx)/1000.0)              # HID→air (HID送信→空中原送信観測)
    print(f"  [上り] HID→air       : {fmt(st(ua))}")
    con.close()

analyze("/mnt/nas/gtnlv/runs/legdecomp_100hz_0622_1330","100Hz")
analyze("/mnt/nas/gtnlv/runs/legdecomp_60hz_0622_1400","60Hz")
