#!/usr/bin/env python3
# pps_diff.py の dt.csv を読み、Δt 分布と時系列の要約を出す。
import argparse
import csv
import math
import os
import sys


def pct(arr, p):
    n = len(arr)
    if n == 0:
        return float("nan")
    k = max(0, min(n - 1, int(round((p / 100.0) * (n - 1)))))
    return arr[k]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("csv_path")
    ap.add_argument("--hist-bin-us", type=float, default=2.0,
                    help="ヒストグラム bin 幅 [us]")
    ap.add_argument("--hist-range", type=float, default=80.0,
                    help="±この値 [us] の範囲で bin")
    args = ap.parse_args()

    rows = []
    with open(args.csv_path) as f:
        r = csv.DictReader(f)
        for row in r:
            try:
                dt = float(row["dt_us"]) if row["dt_us"] else None
            except ValueError:
                dt = None
            if dt is not None:
                rows.append({
                    "t": float(row["t_unix"]),
                    "dt": dt,
                    "ch1_pk": float(row["ch1_max_v"]),
                    "ch2_pk": float(row["ch2_max_v"]),
                })

    n = len(rows)
    print(f"# csv: {args.csv_path}")
    print(f"# n_event = {n}")
    if n == 0:
        sys.exit(0)
    t0 = rows[0]["t"]; t1 = rows[-1]["t"]
    print(f"# elapsed = {t1 - t0:.1f} s")

    dts = sorted(r["dt"] for r in rows)
    mean = sum(dts) / n
    var = sum((x - mean) ** 2 for x in dts) / max(n - 1, 1)
    sd = math.sqrt(var)
    print()
    print("## 全イベント分布 [Δt = HID(ch2) - sniffer(ch1)] us")
    print(f"  min     = {dts[0]:+.3f}")
    print(f"  p01     = {pct(dts, 1):+.3f}")
    print(f"  p05     = {pct(dts, 5):+.3f}")
    print(f"  p10     = {pct(dts, 10):+.3f}")
    print(f"  p25     = {pct(dts, 25):+.3f}")
    print(f"  p50     = {pct(dts, 50):+.3f}   <-- median")
    print(f"  p75     = {pct(dts, 75):+.3f}")
    print(f"  p90     = {pct(dts, 90):+.3f}")
    print(f"  p95     = {pct(dts, 95):+.3f}")
    print(f"  p99     = {pct(dts, 99):+.3f}")
    print(f"  max     = {dts[-1]:+.3f}")
    print(f"  mean    = {mean:+.3f}")
    print(f"  sd      = {sd:.3f}")
    print(f"  p95-p05 = {pct(dts, 95) - pct(dts, 5):.3f}  (90% range)")
    print(f"  p99-p01 = {pct(dts, 99) - pct(dts, 1):.3f}  (98% range)")

    # 外れ値判定: median ± 5*MAD
    med = pct(dts, 50)
    abs_dev = sorted(abs(x - med) for x in dts)
    mad = pct(abs_dev, 50)
    thr = max(20.0, 5 * 1.4826 * mad)
    out = [(r["t"], r["dt"]) for r in rows if abs(r["dt"] - med) > thr]
    print()
    print(f"## 外れ値 (|dt - median| > {thr:.2f} us)  {len(out)} / {n} = {len(out)/n*100:.2f}%")
    for t, dt in out[:20]:
        print(f"  t+{t - t0:7.1f}s  dt={dt:+.3f} us  (deviation={dt - med:+.3f})")
    if len(out) > 20:
        print(f"  ... (残 {len(out) - 20} 件)")

    # 時間バケット (60 秒ごと) の median
    print()
    print("## 60s バケット median / sd / count")
    bucket = {}
    for r in rows:
        b = int((r["t"] - t0) // 60)
        bucket.setdefault(b, []).append(r["dt"])
    for b in sorted(bucket):
        arr = sorted(bucket[b])
        bm = pct(arr, 50)
        avg = sum(arr) / len(arr)
        bv = math.sqrt(sum((x - avg) ** 2 for x in arr) / max(len(arr) - 1, 1))
        print(f"  bucket {b:02d}m  n={len(arr):3d}  med={bm:+.3f}  "
              f"mean={avg:+.3f}  sd={bv:.3f}  "
              f"[{arr[0]:+.2f}, {arr[-1]:+.2f}]")

    # ヒストグラム (text)
    print()
    bw = args.hist_bin_us
    lo = -args.hist_range; hi = args.hist_range
    nb = int((hi - lo) / bw) + 1
    counts = [0] * nb
    over = 0; under = 0
    for x in dts:
        i = int((x - lo) / bw)
        if i < 0:
            under += 1
        elif i >= nb:
            over += 1
        else:
            counts[i] += 1
    cmax = max(counts) if counts else 1
    print(f"## ヒストグラム (bin={bw:.1f}us)  under={under}  over={over}")
    for i in range(nb):
        if counts[i] == 0:
            continue
        x = lo + i * bw
        bar = "#" * int(counts[i] / cmax * 60)
        print(f"  {x:+6.1f}〜{x+bw:+6.1f}  {counts[i]:4d}  {bar}")


if __name__ == "__main__":
    main()
