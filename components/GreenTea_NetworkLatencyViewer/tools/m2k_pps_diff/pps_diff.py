#!/usr/bin/env python3
# ADALM2000 (libm2k) で sniffer/HID の PPS 立ち上がりエッジ Δt を継続測定する。
# CH1 (1+/1-) = sniffer PPS、CH2 (2+/2-) = HID PPS を前提とするが、--swap で入れ替え可。
# 出力: stdout に各 PPS の Δt、CSV に全イベント、最後に統計サマリ。

import argparse
import csv
import math
import os
import signal
import sys
import time
from datetime import datetime

import libm2k

SR_DEFAULT = 10_000_000          # 10 MS/s = 0.1 us 分解能
NSAMP_DEFAULT = 20_000           # 2 ms ウィンドウ (PPS 50us パルス余裕)
PRE_TRIG = 2_000                 # 200 us pre-trigger
THRESH_V_DEFAULT = 1.0           # ESP32C5 3.3V GPIO の半分


def find_rising(samples, threshold, sr, search_from=0, search_to=None):
    """閾値クロスを探し、線形補間で sub-sample 位置を返す。秒で返却。
    search_from/to で探索範囲をサンプル単位に絞れる。
    """
    n = len(samples)
    lo = max(1, search_from)
    hi = n if search_to is None else min(n, search_to)
    for i in range(lo, hi):
        if samples[i - 1] < threshold <= samples[i]:
            y0 = samples[i - 1]
            y1 = samples[i]
            if y1 == y0:
                frac = 0.0
            else:
                frac = (threshold - y0) / (y1 - y0)
            return (i - 1 + frac) / sr
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out-dir", default=None,
                    help="default: out/m2k_pps_diff_<YYYYmmdd_HHMM>")
    ap.add_argument("--sr", type=float, default=SR_DEFAULT)
    ap.add_argument("--nsamp", type=int, default=NSAMP_DEFAULT)
    ap.add_argument("--threshold", type=float, default=THRESH_V_DEFAULT)
    ap.add_argument("--range", choices=["high", "low"], default="low",
                    help="low=PLUS_MINUS_25V (3.3V GPIO 推奨)、high=PLUS_MINUS_2_5V")
    ap.add_argument("--duration", type=float, default=0.0,
                    help="秒。0 で無限ループ (Ctrl+C で停止)")
    ap.add_argument("--swap", action="store_true",
                    help="CH1 と CH2 を入れ替え (Δt 符号反転と等価)")
    ap.add_argument("--label-ch1", default="sniffer")
    ap.add_argument("--label-ch2", default="HID")
    ap.add_argument("--selftest", action="store_true",
                    help="AWG1→AIN1 / AWG2→AIN2 で 3.3V パルスを内部生成し配線無しで動作確認")
    args = ap.parse_args()

    out_dir = args.out_dir or os.path.join(
        "out", "m2k_pps_diff_" + datetime.now().strftime("%Y%m%d_%H%M"))
    os.makedirs(out_dir, exist_ok=True)
    csv_path = os.path.join(out_dir, "dt.csv")
    log_path = os.path.join(out_dir, "run.log")
    print(f"# out_dir={out_dir}", flush=True)

    # ADALM2000 open
    ctxs = libm2k.getAllContexts()
    if not ctxs:
        print("ERROR: ADALM2000 が見つかりません", file=sys.stderr)
        sys.exit(2)
    m2k = libm2k.m2kOpen(ctxs[0])
    if m2k is None:
        print("ERROR: m2kOpen 失敗", file=sys.stderr)
        sys.exit(2)
    print(f"# FW {m2k.getFirmwareVersion()} ctx={ctxs[0]}", flush=True)

    m2k.calibrateADC()

    ain = m2k.getAnalogIn()
    trig = ain.getTrigger()

    range_const = libm2k.PLUS_MINUS_25V if args.range == "low" else libm2k.PLUS_MINUS_2_5V
    ain.enableChannel(libm2k.ANALOG_IN_CHANNEL_1, True)
    ain.enableChannel(libm2k.ANALOG_IN_CHANNEL_2, True)
    ain.setRange(libm2k.ANALOG_IN_CHANNEL_1, range_const)
    ain.setRange(libm2k.ANALOG_IN_CHANNEL_2, range_const)
    ain.setSampleRate(args.sr)
    actual_sr = ain.getSampleRate()
    print(f"# sr_set={args.sr:.0f} actual_sr={actual_sr:.0f} nsamp={args.nsamp} "
          f"pre={PRE_TRIG} thr={args.threshold}V range={args.range}", flush=True)

    # トリガ: CH1 立ち上がり、analog mode (PPS の到来を待つ block 取得)
    trig.reset()
    trig.setAnalogSource(libm2k.CHANNEL_1)
    trig.setAnalogMode(libm2k.CHANNEL_1, libm2k.ANALOG)
    trig.setAnalogCondition(libm2k.CHANNEL_1, libm2k.RISING_EDGE_ANALOG)
    trig.setAnalogLevel(libm2k.CHANNEL_1, args.threshold)
    trig.setAnalogHysteresis(libm2k.CHANNEL_1, 0.1)  # 100 mV
    trig.setAnalogDelay(-PRE_TRIG)
    trig.setAnalogStreamingFlag(False)

    # --selftest: AWG で内部 PPS を作る (W1→AIN CH1、W2→AIN CH2 をジャンパで接続)
    aout = None
    if args.selftest:
        aout = m2k.getAnalogOut()
        aout.enableChannel(0, True)
        aout.enableChannel(1, True)
        aout_sr = 750_000  # 0.75 MS/s
        aout.setSampleRate(0, aout_sr)
        aout.setSampleRate(1, aout_sr)
        # 1 秒周期で 50 us の 3.3V パルス。ch2 は ch1 より 35 us 遅らせる。
        import numpy as np
        N = aout_sr  # 1 秒分
        buf1 = np.zeros(N)
        buf2 = np.zeros(N)
        pulse_samples = int(50e-6 * aout_sr)  # 37 samples
        shift = int(35e-6 * aout_sr)  # 26 samples = +35 us (ch2 遅い)
        buf1[0:pulse_samples] = 3.3
        buf2[shift:shift + pulse_samples] = 3.3
        aout.setCyclic(True)
        aout.push([buf1.tolist(), buf2.tolist()])
        print("# selftest: AWG W1=ch1 pulse, W2=ch2 pulse (+35us 遅らせ)、"
              "AWG W1→AIN CH1+、W2→AIN CH2+ をジャンパで接続して試験",
              flush=True)

    fields = ["t_unix", "dt_us", "edge1_us", "edge2_us",
              "ch1_max_v", "ch1_min_v", "ch2_max_v", "ch2_min_v",
              "miss_ch1", "miss_ch2"]
    f_csv = open(csv_path, "w", newline="")
    w = csv.writer(f_csv)
    w.writerow(fields)
    f_csv.flush()

    f_log = open(log_path, "w")

    stop = {"flag": False}

    def on_sigint(sig, frame):
        stop["flag"] = True
        print("# stop requested", flush=True)
    signal.signal(signal.SIGINT, on_sigint)

    dt_us_list = []
    n_event = 0
    n_miss = 0
    t_start = time.time()
    sr = actual_sr

    try:
        while not stop["flag"]:
            if args.duration > 0 and (time.time() - t_start) >= args.duration:
                break
            try:
                ain.startAcquisition(args.nsamp)
                samples = ain.getSamples(args.nsamp)
            except Exception as e:
                print(f"# acq error: {e}", flush=True)
                time.sleep(0.2)
                continue
            finally:
                try:
                    ain.stopAcquisition()
                except Exception:
                    pass

            ch1 = samples[0]
            ch2 = samples[1]
            if args.swap:
                ch1, ch2 = ch2, ch1

            # ch1 立ち上がりはトリガ点 (PRE_TRIG idx) 付近を探す。
            e1 = find_rising(ch1, args.threshold, sr,
                             search_from=max(1, PRE_TRIG - 200),
                             search_to=PRE_TRIG + 1000)
            # ch2 は ch1 トリガから ±200 us の窓に絞り、最も近い立ち上がりを採用。
            #  HID が先行する場合 (前回 -35 us) は pre-trigger 内、
            #  逆順だと post-trigger になる。
            win_from = max(1, PRE_TRIG - 2000)   # -200 us
            win_to = min(args.nsamp, PRE_TRIG + 2000)  # +200 us
            e2 = find_rising(ch2, args.threshold, sr,
                             search_from=win_from, search_to=win_to)
            now = time.time()

            ch1_max = max(ch1); ch1_min = min(ch1)
            ch2_max = max(ch2); ch2_min = min(ch2)

            miss1 = 1 if e1 is None else 0
            miss2 = 1 if e2 is None else 0
            if e1 is None or e2 is None:
                n_miss += 1
                w.writerow([f"{now:.6f}", "", "" if e1 is None else f"{e1*1e6:.4f}",
                            "" if e2 is None else f"{e2*1e6:.4f}",
                            f"{ch1_max:.4f}", f"{ch1_min:.4f}",
                            f"{ch2_max:.4f}", f"{ch2_min:.4f}",
                            miss1, miss2])
                f_csv.flush()
                print(f"# miss (ch1={'-' if miss1 else 'OK'} "
                      f"ch2={'-' if miss2 else 'OK'}) "
                      f"max ch1={ch1_max:.2f}V ch2={ch2_max:.2f}V", flush=True)
                continue

            dt_us = (e2 - e1) * 1e6  # ch2 - ch1
            dt_us_list.append(dt_us)
            n_event += 1
            w.writerow([f"{now:.6f}", f"{dt_us:.4f}",
                        f"{e1*1e6:.4f}", f"{e2*1e6:.4f}",
                        f"{ch1_max:.4f}", f"{ch1_min:.4f}",
                        f"{ch2_max:.4f}", f"{ch2_min:.4f}",
                        0, 0])
            f_csv.flush()

            # 10 イベント毎に rolling 統計を出す
            if n_event % 10 == 0:
                arr = dt_us_list[-100:]
                med = sorted(arr)[len(arr)//2]
                mean = sum(arr) / len(arr)
                var = sum((x - mean) ** 2 for x in arr) / max(len(arr) - 1, 1)
                sd = math.sqrt(var)
                lo = min(arr); hi = max(arr)
                print(f"[{n_event:5d}] dt={dt_us:+8.3f} us  "
                      f"(roll100 med={med:+.3f} mean={mean:+.3f} sd={sd:.3f} "
                      f"min={lo:+.3f} max={hi:+.3f}) miss={n_miss}",
                      flush=True)
            else:
                print(f"[{n_event:5d}] dt={dt_us:+8.3f} us "
                      f"(ch1_pk={ch1_max:.2f}V ch2_pk={ch2_max:.2f}V)",
                      flush=True)
    finally:
        try:
            ain.stopAcquisition()
        except Exception:
            pass
        libm2k.contextClose(m2k)
        f_csv.close()
        # サマリ
        summary_lines = []
        if dt_us_list:
            arr = sorted(dt_us_list)
            n = len(arr)
            mean = sum(arr) / n
            var = sum((x - mean) ** 2 for x in arr) / max(n - 1, 1)
            sd = math.sqrt(var)
            def pct(p):
                k = max(0, min(n - 1, int(round((p / 100.0) * (n - 1)))))
                return arr[k]
            summary_lines = [
                f"n_event={n}  n_miss={n_miss}  elapsed={time.time()-t_start:.1f}s",
                f"ch2 - ch1 (= {args.label_ch2} - {args.label_ch1}) in us:",
                f"  min={arr[0]:+.4f}  p01={pct(1):+.4f}  p05={pct(5):+.4f}  "
                f"p50={pct(50):+.4f}  p95={pct(95):+.4f}  p99={pct(99):+.4f}  "
                f"max={arr[-1]:+.4f}",
                f"  mean={mean:+.4f}  sd={sd:.4f}",
            ]
        else:
            summary_lines = [f"n_event=0  n_miss={n_miss}"]
        print("\n=== SUMMARY ===")
        for ln in summary_lines:
            print(ln)
            f_log.write(ln + "\n")
        f_log.close()
        print(f"# CSV: {csv_path}")
        print(f"# log: {log_path}")


if __name__ == "__main__":
    main()
