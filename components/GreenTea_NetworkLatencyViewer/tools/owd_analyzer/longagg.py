"""2h run 集計: 下り区間(SPAN基準) + 上りHID→air + 通算ロス + host→SPAN(参考)。"""
import duckdb, statistics, sys
exec(open("bridge_owd.py").read().split("\ndef owd")[0])  # build_bridge / offset_at / build_bridge_windowed

def st(v):
    v = [x for x in v if -50 < x < 20000]; s = sorted(v); n = len(s)
    if n < 30: return None
    return dict(n=n, median=s[n//2], mean=statistics.mean(v),
                p95=s[int(n*0.95)], p99=s[int(n*0.99)], max=s[-1])

def f(d):
    return (f"med={d['median']:.2f} mean={d['mean']:.2f} p95={d['p95']:.2f} "
            f"p99={d['p99']:.2f} max={d['max']:.1f} (n={d['n']})") if d else "—"

def analyze(run, label, win_s=600.0):
    # 長時間 run は窓分割 bridge (区間線形) で drift 非線形分を吸収
    wb = build_bridge_windowed(run, win_s)
    b1 = build_bridge(run)  # 参考: 単一fit 残差
    con = duckdb.connect()
    rx = f"read_parquet('{run}/rx_dl/*.parquet', union_by_name=true)"
    wr = f"read_parquet('{run}/wire/*.parquet', union_by_name=true)"
    sf = f"read_parquet('{run}/sniffer_frame/*.parquet')"
    def off(t): return offset_at_win(wb, t)
    print(f"\n=== {label} ({run.split('/')[-1]}) "
          f"bridge残差sd: 窓{int(win_s)}s={wb['resid_sd']:.1f}us / 単一fit={b1['resid_sd']:.0f}us "
          f"drift={b1['drift_ppm']:.2f}ppm ===", flush=True)

    # cycle_count は 24bit (sender カウンタ)。長時間 run で 16,777,215→0 を跨ぐと同一値が
    # run 内の離れた 2 時刻に現れる。min(t_wire_phc) が早い tw を拾い rx_dl の遅い TSF と誤
    # join → 巨大 OWD アーティファクト。同一 cycle の wire 捕捉が >0.1s に跨るものは wrap 衝突
    # として除外 (802.11 再送は同一 TXOP 内 ms スケールなので保持)。閾値 0.1s 実証:
    # <60s だと衝突が >500ms に 1110 件漏れ、<0.1s で 0 件・max 370ms に収束 (6h W52 run)。
    wmin = (f"(SELECT robot_id,cycle_count,min(t_wire_phc) tw,max(t_tx_unix) tx FROM {wr} "
            f"WHERE t_wire_phc>1e9 GROUP BY robot_id,cycle_count "
            f"HAVING max(t_wire_phc)-min(t_wire_phc) < 0.1)")
    sdl = (f"(SELECT robot_id,cycle_count,min(tsf_us) air FROM {sf} WHERE dst LIKE '38:44%' "
           f"GROUP BY robot_id,cycle_count)")
    sul = (f"(SELECT robot_id,cycle_count,min(tsf_us) air FROM {sf} WHERE src LIKE '38:44%' "
           f"GROUP BY robot_id,cycle_count)")

    sh = [(h/1e6+off(tw)-tw)*1000 for tw, h in con.execute(
        f"SELECT w.tw,r.t_hid_rx_tsf_us FROM {wmin} w JOIN {rx} r "
        f"ON w.robot_id=r.robot_id AND w.cycle_count=r.cycle_count "
        f"WHERE r.t_hid_rx_tsf_us IS NOT NULL").fetchall()]
    ah = [(h-a)/1000.0 for h, a in con.execute(
        f"SELECT r.t_hid_rx_tsf_us,s.air FROM {rx} r JOIN {sdl} s "
        f"ON r.robot_id=s.robot_id AND r.cycle_count=s.cycle_count "
        f"WHERE r.t_hid_rx_tsf_us IS NOT NULL").fetchall()]
    hs = [(tw-tx)*1000 for tw, tx in con.execute(
        f"SELECT tw,tx FROM {wmin} WHERE tx>1e9").fetchall()]
    ua = [(a-tt)/1000.0 for tt, a in con.execute(
        f"SELECT r.t_hid_tx_tsf_us,s.air FROM {rx} r JOIN {sul} s "
        f"ON r.robot_id=s.robot_id AND s.cycle_count=r.hid_seq "
        f"WHERE r.t_hid_tx_tsf_us IS NOT NULL").fetchall()]
    dsh, dah, dua = st(sh), st(ah), st(ua)
    dwell = (dsh["median"]-dah["median"]) if (dsh and dah) else None

    # 通算ロス (per robot, cycle_count span vs distinct)。
    # cycle_count 24bit wrap を window 関数で unwrap してから span を取る (naive な
    # max-min は wrap 跨ぎで全域 16.7M に化け損失を過大計上する)。減少ジャンプ >8M を
    # wrap とみなし累積 2^24 を加算。
    losses = []
    for rid, span, dist in con.execute(
        f"WITH base AS (SELECT robot_id, cycle_count, t_hid_rx_tsf_us, "
        f"  CASE WHEN cycle_count < lag(cycle_count) OVER w - 8000000 THEN 1 ELSE 0 END wf "
        f"  FROM {rx} WHERE cycle_count IS NOT NULL AND t_hid_rx_tsf_us IS NOT NULL "
        f"  WINDOW w AS (PARTITION BY robot_id ORDER BY t_hid_rx_tsf_us)), "
        f"uw AS (SELECT robot_id, cycle_count + 16777216 * "
        f"  (SUM(wf) OVER (PARTITION BY robot_id ORDER BY t_hid_rx_tsf_us)) uc FROM base) "
        f"SELECT robot_id, max(uc)-min(uc)+1, count(DISTINCT uc) "
        f"FROM uw GROUP BY robot_id ORDER BY robot_id").fetchall():
        lp = (span-dist)/span*100 if span > 0 else 0
        losses.append((rid, round(lp, 4)))

    print(f"  下り遅延 SPAN→HID(全母集団) : {f(dsh)}   ← 報告値", flush=True)
    print(f"    内訳 Air→HID(無線)        : {f(dah)}", flush=True)
    print(f"    内訳 AP滞留(=総−無線,med)  : {('%.2f ms'%dwell) if dwell is not None else '—'}", flush=True)
    print(f"  上り HID→air               : {f(dua)}", flush=True)
    print(f"  [参考] host→SPAN(機間,再同期後): {f(st(hs))}", flush=True)
    print(f"  通算ロス%/robot: {losses}", flush=True)
    mx = max(l for _, l in losses) if losses else 0
    print(f"    → max loss = {mx}%", flush=True)
    con.close()

if __name__ == "__main__":
    for a in sys.argv[1:]:
        run, lab = a.split("=", 1) if "=" in a else (a, a.split("/")[-1])
        try: analyze(run, lab)
        except Exception as e: print(f"\n=== {lab} 失敗: {e} ===", flush=True)
