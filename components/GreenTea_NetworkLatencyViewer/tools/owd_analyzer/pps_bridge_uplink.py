"""上り OWD (HID→AP-air): rx_dl(t_hid_tx_tsf_us) ↔ sniffer(tsf_us, dst=broadcast) を
(robot_id, hid_seq=cycle_count) join。両者 AP TSF なので直接差分 (unix/PHC/bridge 不要)。"""
import duckdb, statistics
def uplink(run, hz):
    con=duckdb.connect()
    rx=f"read_parquet('{run}/rx_dl/*.parquet', union_by_name=true)"
    sf=f"read_parquet('{run}/sniffer_frame/*.parquet')"
    print(f"\n=== {hz} 上り OWD (HID送信→AP空中rebroadcast) ({run.split('/')[-1]}) ===")
    print("  robot | 上りOWD med/p95/p99/max (ms) | mean | var(ms^2) | n_join")
    meds=[]; means=[]
    for rid in range(1,7):
        rows=con.execute(f"""
          SELECT r.t_hid_tx_tsf_us, s.tsf_us FROM {rx} r
          JOIN (SELECT cycle_count, min(tsf_us) tsf_us FROM {sf} WHERE robot_id={rid} AND dst='FF:FF:FF:FF:FF:FF' GROUP BY cycle_count) s
            ON s.cycle_count=r.hid_seq
          WHERE r.robot_id={rid} AND r.t_hid_tx_tsf_us IS NOT NULL""").fetchall()
        ow=[(s - t)/1000.0 for t,s in rows]   # ms, AP TSF 直接差
        ow=[v for v in ow if -5<v<2000]
        if len(ow)<100: print(f"   r{rid}  | join不足 ({len(ow)})"); continue
        s=sorted(ow); n=len(s); med=s[n//2]; p95=s[int(n*0.95)]; p99=s[int(n*0.99)]; mx=s[-1]
        meds.append(med); means.append(statistics.mean(ow))
        print(f"   r{rid}  | {med:6.3f}/{p95:6.2f}/{p99:6.2f}/{mx:7.2f} | {statistics.mean(ow):6.3f} | {statistics.pvariance(ow):7.2f} | {len(ow)}")
    con.close()
    if meds: print(f"  [全robot平均] 上りOWD median={statistics.mean(meds):.3f}ms mean={statistics.mean(means):.3f}ms")
uplink("/mnt/nas/gtnlv/runs/longrun_v208_0622_0938","100Hz")
uplink("/mnt/nas/gtnlv/runs/longrun_v208_60hz_0622_1038","60Hz")
