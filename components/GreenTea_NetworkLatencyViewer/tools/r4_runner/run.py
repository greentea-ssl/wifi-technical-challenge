#!/usr/bin/env python3
# R4 host runner — collect ESP32-C5 esp_timer↔TSF samples and fit a line.
#
# Usage:
#   python3 run.py --port /dev/ttyACM1 --label xiao  --duration 1800
#   python3 run.py --port /dev/ttyUSB0 --label devkit --duration 1800
#
# Pairs with: tools/esp_firmware/r4_calib_test/r4_calib_test.ino
# Goal: phase0_runbook §1.1 — residual p99 ≤ 50us on a static AP environment.

import argparse
import csv
import math
import signal
import statistics
import sys
import time
from pathlib import Path

try:
    import serial
except ImportError:
    sys.stderr.write("pyserial not installed; pip install pyserial\n")
    sys.exit(2)


def parse_args():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", required=True, help="Serial port (/dev/ttyACM1 for XIAO, /dev/ttyUSB0 for devkit)")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--duration", type=float, default=1800.0, help="Capture duration in seconds (default 30min)")
    ap.add_argument("--label", default="run", help="Tag used in output filename")
    ap.add_argument("--out", default=None, help="Raw CSV output path (default: ./r4_<label>_<unix>.csv)")
    ap.add_argument("--window", type=int, default=64, help="Sample count per windowed-fit slice (default 64 = ~6.4s)")
    return ap.parse_args()


def fit_line(xs, ys):
    n = len(xs)
    if n < 2:
        return None
    mean_x = sum(xs) / n
    mean_y = sum(ys) / n
    sxx = sum((x - mean_x) ** 2 for x in xs)
    sxy = sum((xs[i] - mean_x) * (ys[i] - mean_y) for i in range(n))
    if sxx == 0:
        return None
    a = sxy / sxx
    b = mean_y - a * mean_x
    residuals = [ys[i] - (a * xs[i] + b) for i in range(n)]
    return a, b, residuals


def summarize(residuals):
    n = len(residuals)
    if n == 0:
        return {"n": 0, "rms_us": 0.0, "p50_us": 0.0, "p99_us": 0.0}
    abs_r = sorted(abs(r) for r in residuals)
    rms = math.sqrt(sum(r * r for r in residuals) / n)
    p99 = abs_r[min(int(n * 0.99), n - 1)]
    p50 = abs_r[n // 2]
    return {"n": n, "rms_us": rms, "p50_us": p50, "p99_us": p99}


def main():
    args = parse_args()
    out_path = Path(args.out) if args.out else Path(f"./r4_{args.label}_{int(time.time())}.csv")

    stop = {"v": False}

    def handler(sig, frame):
        stop["v"] = True

    signal.signal(signal.SIGINT, handler)

    print(f"[i] port={args.port} baud={args.baud} duration={args.duration}s out={out_path}", flush=True)
    print("[i] press Ctrl-C to stop early", flush=True)

    samples = []
    # line-buffered CSV file (buffering=1) so rows hit disk row-by-row.
    with serial.Serial(args.port, args.baud, timeout=1) as ser, out_path.open("w", newline="", buffering=1) as f:
        writer = csv.writer(f)
        writer.writerow(["seq", "esp_timer_us", "tsf_us", "read_dur_us", "tsf_delta_us", "rssi"])

        t_start = time.monotonic()
        last_report = t_start
        last_flush = t_start
        while not stop["v"] and (time.monotonic() - t_start) < args.duration:
            line = ser.readline().decode("utf-8", errors="replace").strip()
            if not line:
                continue
            if line.startswith("#"):
                print(f"[fw] {line}", flush=True)
                continue
            if not line.startswith("R4,"):
                continue
            parts = line.split(",")
            if len(parts) != 7:
                print(f"[w] parse fail (field count): {line}", flush=True)
                continue
            try:
                seq = int(parts[1])
                t_us = int(parts[2])
                tsf_us = int(parts[3])
                rd = int(parts[4])
                dtsf = int(parts[5])
                rssi = int(parts[6])
            except ValueError:
                print(f"[w] parse fail (numeric): {line}", flush=True)
                continue
            samples.append((t_us, tsf_us, rd, dtsf, rssi))
            writer.writerow([seq, t_us, tsf_us, rd, dtsf, rssi])

            now = time.monotonic()
            if now - last_report >= 5.0:
                last_report = now
                print(f"[i] samples={len(samples)} elapsed={now - t_start:.1f}s last_tsf={tsf_us}", flush=True)
            if now - last_flush >= 5.0:
                last_flush = now
                f.flush()

    print(f"[i] collected {len(samples)} samples → {out_path}")
    if len(samples) < 32:
        print("[w] too few samples for meaningful fit")
        return

    # Drop pre-association samples where TSF=0.
    samples = [s for s in samples if s[1] > 0]
    if len(samples) < 32:
        print("[w] not enough post-association samples")
        return

    # samples: (t_a_us, tsf_us, read_dur_us, tsf_delta_us, rssi)
    # Use midpoint of the TSF readout window as the esp_timer reference.
    # See phase0_runbook §1.1: this drops residual p99 ~3.5x vs. using t_a alone.
    xs_a   = [s[0] for s in samples]
    xs_mid = [s[0] + s[2] // 2 for s in samples]
    ys     = [s[1] for s in samples]

    for label, xs in [("t_a (raw)", xs_a), ("t_mid (recommended)", xs_mid)]:
        fit = fit_line(xs, ys)
        if not fit:
            print(f"[w] global fit failed for {label}")
            continue
        a, b, resid = fit
        stats = summarize(resid)
        ppm = (a - 1.0) * 1e6
        print()
        print(f"=== Global linear fit: tsf_us = a * esp_timer + b  [ref={label}] ===")
        print(f"  a       = {a:.12f}  ({ppm:+.3f} ppm vs. 1.0)")
        print(f"  b       = {b:.3f} us")
        print(f"  n       = {stats['n']}")
        print(f"  |resid| p50 / RMS / p99 = {stats['p50_us']:.1f} / {stats['rms_us']:.1f} / {stats['p99_us']:.1f} us")

    # Outlier filter: drop top-5% read_dur, refit using midpoint
    rd = [s[2] for s in samples]
    thr = sorted(rd)[int(len(rd) * 0.95)]
    keep = [i for i, s in enumerate(samples) if s[2] <= thr]
    if len(keep) >= 32:
        xs2 = [xs_mid[i] for i in keep]
        ys2 = [ys[i] for i in keep]
        fit = fit_line(xs2, ys2)
        if fit:
            _, _, resid = fit
            s = summarize(resid)
            print()
            print(f"=== Filtered fit: drop read_dur > p95 ({thr}us), midpoint ref ===")
            print(f"  n={s['n']}  |resid| p50 / RMS / p99 = {s['p50_us']:.1f} / {s['rms_us']:.1f} / {s['p99_us']:.1f} us")

    # For windowed analysis below, use midpoint refs.
    xs = xs_mid

    W = args.window
    if len(samples) >= 2 * W:
        win_rmss, win_p99s, win_ppms = [], [], []
        for i in range(0, len(samples) - W + 1, W):
            wf = fit_line(xs[i:i + W], ys[i:i + W])
            if not wf:
                continue
            wa, _, wresid = wf
            ws = summarize(wresid)
            win_rmss.append(ws["rms_us"])
            win_p99s.append(ws["p99_us"])
            win_ppms.append((wa - 1.0) * 1e6)
        if win_rmss:
            print()
            print(f"=== Windowed fits (W={W} samples ≈ {W * 0.1:.1f}s) ===")
            print(f"  n windows = {len(win_rmss)}")
            print(f"  RMS  min/med/max = {min(win_rmss):.1f} / {statistics.median(win_rmss):.1f} / {max(win_rmss):.1f} us")
            print(f"  p99  min/med/max = {min(win_p99s):.1f} / {statistics.median(win_p99s):.1f} / {max(win_p99s):.1f} us")
            print(f"  ppm  min/med/max = {min(win_ppms):+.3f} / {statistics.median(win_ppms):+.3f} / {max(win_ppms):+.3f}")

    print()
    print(f"[done] raw CSV: {out_path}")
    print("[done] R4 合格ライン: 残差 p99 <= 50us (phase0_runbook §1.1)")


if __name__ == "__main__":
    main()
