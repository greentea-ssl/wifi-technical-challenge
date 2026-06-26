"""下り区間分解 (SPAN 基準):
  host→SPAN  = t_wire_phc(RasPi5 sw RX) − t_tx_unix(RasPi4 stamp)  ← 機間クロック依存、参考値 (遅延に含めない)
  SPAN→Air   = air_unix(bridge) − t_wire_phc                       ← AP 滞留 (RasPi5 基準同士、正確)
  Air→HID    = (t_hid_rx_tsf − air_tsf)/1000                        ← 無線リンク (AP-TSF 直接、正確)
  下り遅延    = SPAN→Air + Air→HID                                  (host→SPAN は除外)
join: (robot_id, cycle_count)。wire/sniffer は同cycleの最初の観測 (min) を採用。
"""
import duckdb, statistics, sys
exec(open("bridge_owd.py").read().split("\ndef owd")[0])  # build_bridge / offset_at

def st(v):
    v = [x for x in v if -50 < x < 5000]; s = sorted(v); n = len(s)
    if n < 30: return None
    return dict(n=n, median=s[n//2], mean=statistics.mean(v),
                p95=s[int(n*0.95)], p99=s[int(n*0.99)], max=s[-1])

def fmt(d):
    return (f"med={d['median']:.2f} mean={d['mean']:.2f} p95={d['p95']:.2f} "
            f"p99={d['p99']:.2f} max={d['max']:.1f} (n={d['n']})") if d else "—"

def analyze(run, label):
    b = build_bridge(run); con = duckdb.connect()
    rx = f"read_parquet('{run}/rx_dl/*.parquet', union_by_name=true)"
    wr = f"read_parquet('{run}/wire/*.parquet', union_by_name=true)"
    sf = f"read_parquet('{run}/sniffer_frame/*.parquet')"
    inter, slope, x0 = b["inter"], b["slope"], b["x0"]
    def off(t): return inter + slope*(t - x0)
    print(f"\n=== {label} ({run.split('/')[-1]}) bridge残差sd={b['resid_sd']:.0f}us ===")

    # wire を (robot,cycle) で最初の観測に集約 (AP 再送で重複するため min)
    wmin = (f"(SELECT robot_id, cycle_count, min(t_wire_phc) twire, "
            f"max(t_tx_unix) ttx FROM {wr} WHERE t_wire_phc>1e9 "
            f"GROUP BY robot_id, cycle_count)")
    smin = (f"(SELECT robot_id, cycle_count, min(tsf_us) air FROM {sf} "
            f"WHERE dst LIKE '38:44%' GROUP BY robot_id, cycle_count)")

    # host→SPAN (参考): t_tx_unix が有効 (>0) な cycle のみ
    hs = []
    for twire, ttx in con.execute(
            f"SELECT twire, ttx FROM {wmin} WHERE ttx>1e9").fetchall():
        hs.append((twire - ttx) * 1000)

    # SPAN→Air (AP 滞留): wire × sniffer(dst=HID)
    sa = []
    for twire, air in con.execute(
            f"SELECT w.twire, s.air FROM {wmin} w JOIN {smin} s "
            f"ON w.robot_id=s.robot_id AND w.cycle_count=s.cycle_count").fetchall():
        sa.append((air/1e6 + off(twire) - twire) * 1000)

    # Air→HID (無線): rx_dl × sniffer(dst=HID)、TSF 直接
    ah = []
    for hidrx, air in con.execute(
            f"SELECT r.t_hid_rx_tsf_us, s.air FROM {rx} r JOIN {smin} s "
            f"ON r.robot_id=s.robot_id AND r.cycle_count=s.cycle_count "
            f"WHERE r.t_hid_rx_tsf_us IS NOT NULL").fetchall():
        ah.append((hidrx - air) / 1000.0)

    # SPAN→HID (下り遅延 total = SPAN→Air+Air→HID と一致するはず): rx_dl × wire
    sh = []
    for twire, hidrx in con.execute(
            f"SELECT w.twire, r.t_hid_rx_tsf_us FROM {wmin} w JOIN {rx} r "
            f"ON w.robot_id=r.robot_id AND w.cycle_count=r.cycle_count "
            f"WHERE r.t_hid_rx_tsf_us IS NOT NULL").fetchall():
        sh.append((hidrx/1e6 + off(twire) - twire) * 1000)

    dsa, dah, dsh, dhs = st(sa), st(ah), st(sh), st(hs)
    # AP 滞留は全母集団ベースで導出 (直接 SPAN→Air は sniffer 捕捉が低遅延に偏るため過小)。
    # Air→HID は無線リンクでレート非依存=代表性あり。AP滞留 = 総下り − 無線。
    dwell_med = (dsh["median"] - dah["median"]) if (dsh and dah) else None
    # 捕捉率は distinct (robot,cycle) 比で算出 (rx_dl は 1 cycle 約2行の重複があり
    # raw 行数比だと過小に見える)。smin = sniffer 下り捕捉の distinct (robot,cycle)。
    sn_dist = con.execute(f"SELECT count(*) FROM {smin}").fetchone()[0]
    rx_dist = con.execute(
        f"SELECT count(*) FROM (SELECT DISTINCT robot_id,cycle_count FROM {rx} "
        f"WHERE t_hid_rx_tsf_us IS NOT NULL)").fetchone()[0]
    cover = (100.0 * sn_dist / rx_dist) if rx_dist else 0.0
    print(f"  下り遅延 SPAN→HID (全母集団) : {fmt(dsh)}   ← 報告値")
    print(f"    内訳 Air→HID (無線)        : {fmt(dah)}")
    print(f"    内訳 AP滞留 (=SPAN→HID−Air→HID, median): "
          f"{('%.2f ms' % dwell_med) if dwell_med is not None else '—'}")
    bias_note = ("≈AP滞留 (捕捉≈全数で信頼可)" if cover >= 90
                 else "※捕捉バイアスで過小、参考のみ (AP滞留は導出値を使用)")
    print(f"  直接 SPAN→Air (sniffer捕捉{cover:.0f}%): {fmt(dsa)}  {bias_note}")
    print(f"  [参考] host→SPAN (機間)      : {fmt(dhs)}  ※RasPi4クロック依存、遅延に含めない")
    con.close()
    return {"label": label, "span_hid": dsh, "air_hid": dah,
            "dwell_med": dwell_med, "span_air_direct": dsa, "cover_pct": cover}

if __name__ == "__main__":
    for a in sys.argv[1:]:
        run, lab = a.split("=", 1) if "=" in a else (a, a.split("/")[-1])
        try: analyze(run, lab)
        except Exception as e: print(f"\n=== {lab} 解析失敗: {e} ===")
