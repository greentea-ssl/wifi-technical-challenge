#!/usr/bin/env python3
# duckdb_analyze — 外部化 Parquet 録画を任意マシンから解析する OWD アナライザ。
#
# RecordSink(parquet) が NAS に吐いた run ディレクトリ (各 stream が
# <run>/<table>/*.parquet) を DuckDB で読み、OWD bridge / 損失 / data rate を算出する。
# DB サーバ不要 (DuckDB は埋め込み)、RasPi 以外の解析マシンで動く。
#
# 使い方:
#   python3 duckdb_analyze.py /mnt/nas/gtnlv/runs/<run>            # 全 robot
#   python3 duckdb_analyze.py /mnt/nas/gtnlv/runs/<run> --robot 0
#
# 依存: pip install duckdb   (pandas 等不要)

from __future__ import annotations
import argparse
import glob
import os
import duckdb


def q1(con, sql, params=None):
    r = con.execute(sql, params or []).fetchone()
    return r[0] if r else None


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("run_dir", help="録画 run ディレクトリ (<run>/<table>/*.parquet)")
    ap.add_argument("--robot", type=int, default=None, help="robot_id で絞る")
    args = ap.parse_args()

    rx_glob = os.path.join(args.run_dir, "rx_dl", "*.parquet")
    if not glob.glob(rx_glob):
        raise SystemExit(f"rx_dl Parquet が無い: {rx_glob}")
    con = duckdb.connect()
    con.execute(f"CREATE VIEW rx_dl AS SELECT * FROM read_parquet('{rx_glob}', union_by_name=true)")

    where = "" if args.robot is None else f"WHERE robot_id = {args.robot}"
    n = q1(con, f"SELECT count(*) FROM rx_dl {where}")
    print(f"=== rx_dl: {n} rows {'(robot '+str(args.robot)+')' if args.robot is not None else '(全robot)'} ===")

    # 下り OWD (host-receive 近似)
    print("\n[下り OWD owd_dl_approx_us] (参考: host clock 近似)")
    for lbl, expr in (("mean", "avg"), ("median", "median"), ("p95", "quantile_cont"),
                      ("p99", "quantile_cont"), ("max", "max"), ("stddev", "stddev_samp")):
        if expr == "quantile_cont":
            qv = 0.95 if lbl == "p95" else 0.99
            v = q1(con, f"SELECT quantile_cont(owd_dl_approx_us, {qv}) FROM rx_dl {where}")
        else:
            v = q1(con, f"SELECT {expr}(owd_dl_approx_us) FROM rx_dl {where}")
        print(f"  {lbl:7} = {v/1000:.3f} ms" if v is not None else f"  {lbl}: -")

    # cycle_count 損失 (robot 別、span = max-min+1)
    print("\n[下り損失 cycle_count]")
    rows = con.execute(
        f"SELECT robot_id, count(DISTINCT cycle_count) d, max(cycle_count)-min(cycle_count)+1 span "
        f"FROM rx_dl {where} GROUP BY robot_id ORDER BY robot_id").fetchall()
    for rid, d, span in rows:
        loss = 100*(span-d)/span if span else 0
        print(f"  robot{rid}: delivered={d} expected={span} loss={loss:.4f}%")

    # data rate (delivered × 64B × 8)
    span_s = q1(con, f"SELECT (max(t_rpid_recv_unix)-min(t_rpid_recv_unix)) FROM rx_dl {where}")
    if span_s and span_s > 0:
        kbps = n * 64 * 8 / span_s / 1000
        print(f"\n[Data Rate] {kbps:.1f} kbps over {span_s:.0f}s")

    # PPS bridge OWD (pps があれば: tsf_us/1e6 + offset - corr_unix_time)
    pps_g = os.path.join(args.run_dir, "pps_gpio", "*.parquet")
    pps_u = os.path.join(args.run_dir, "pps_uart", "*.parquet")
    if glob.glob(pps_g) and glob.glob(pps_u):
        print("\n[PPS bridge OWD] pps_gpio/pps_uart あり → sniffer_bridge.py で絶対OWD算出推奨")
    else:
        print("\n[PPS bridge OWD] pps データ無し (この run では未記録)")
    con.close()


if __name__ == "__main__":
    main()
