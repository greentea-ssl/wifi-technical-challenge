#!/usr/bin/env python3
# ADALM2000 で sniffer 基準の PPS skew を毎秒ログ (WebUI 録画ボタンから起動)。
# 出力 dir を引数で受ける: python3 pps_skew_logger.py <out_dir>
# libm2k 必須 + root (libiio USB)。ADALM2000 不在なら即終了 (録画本体は継続)。
#
# 基準 = sniffer PPS (LA ch6)。sniffer は TSF↔unix bridge の生成源なので、
# 各ロボットの PPS を sniffer 基準で見た skew = bridge の実効同期精度に相当する。
# (旧版は robot1 基準だった。2026-06-23 に sniffer PPS を LA に接続し基準化)
import sys, os, time, csv

OUT_DIR = sys.argv[1] if len(sys.argv) > 1 else "/tmp"
SR = 10_000_000           # 10MHz → 0.1us/sample
NS = 4000                 # 400us 窓 (trigger 中心 ±200us、skew は数十us なので十分)
SNIFFER_CH = 6            # sniffer PPS = 基準 (trigger 兼)
CH2ROBOT = {0: 3, 1: 2, 2: 4, 3: 6, 4: 5, 5: 1}   # LA ch → robot (2026-06-22 マッピング)
NCH = 7                   # ch0-5 (robot) + ch6 (sniffer)

try:
    import libm2k
except Exception as e:
    print(f"libm2k 不可: {e}", file=sys.stderr); sys.exit(0)

os.makedirs(OUT_DIR, exist_ok=True)
csvpath = os.path.join(OUT_DIR, "pps_skew.csv")
robots = sorted(CH2ROBOT.values())
new = not os.path.exists(csvpath)
f = open(csvpath, "a", newline="", buffering=1)
w = csv.writer(f)
if new:
    # skew = robot_edge - sniffer_edge (us)。正 = robot が sniffer より遅い
    w.writerow(["unix_time", "ref"] + [f"r{r}_skew_us" for r in robots])

def open_m2k():
    ctx = libm2k.m2kOpen()
    if ctx is None:
        return None, None
    dig = ctx.getDigital(); dig.setSampleRateIn(SR)
    for ch in range(NCH):
        dig.setDirection(ch, libm2k.DIO_INPUT); dig.enableChannel(ch, True)
    dig.setKernelBuffersCountIn(1)
    trig = dig.getTrigger()
    for ch in range(NCH):
        trig.setDigitalCondition(ch, libm2k.NO_TRIGGER_DIGITAL)
    trig.setDigitalCondition(SNIFFER_CH, libm2k.RISING_EDGE_DIGITAL)
    trig.setDigitalMode(libm2k.DIO_OR)
    try:
        # sniffer より先に発火する robot も拾えるよう trigger 前方も広めに取得
        trig.setDigitalDelay(-NS // 2)
    except Exception:
        pass
    return ctx, dig

ctx, dig = open_m2k()
if ctx is None:
    print("ADALM2000 不在 → PPS skew ログ無し (録画本体は継続)", file=sys.stderr); sys.exit(0)
print(f"PPS skew logger (基準=sniffer ch{SNIFFER_CH}) → {csvpath}", flush=True)
n = 0
while True:
    try:
        d = dig.getSamples(NS)
        first = {}
        for ch in range(NCH):
            p = 0
            for i, ww in enumerate(d):
                b = (int(ww) >> ch) & 1
                if b and not p:
                    first[ch] = i; break
                p = b
        # sniffer (基準) が取れた時のみ記録。robot は取れたものだけ skew、欠落は空欄
        if SNIFFER_CH in first:
            ref = first[SNIFFER_CH]
            row = {}
            for ch in range(6):
                if ch in first:
                    row[CH2ROBOT[ch]] = round((first[ch] - ref) * 0.1, 2)
            w.writerow([round(time.time(), 3), "sniffer"]
                       + [row.get(r, "") for r in robots])
            n += 1
    except Exception as e:
        print(f"m2k err: {e} → 再接続", flush=True)
        try:
            libm2k.contextClose(ctx)
        except Exception:
            pass
        time.sleep(2)
        ctx, dig = open_m2k()
        if ctx is None:
            time.sleep(3)
    time.sleep(0.8)
