#!/usr/bin/env python3
# measure.py — 起動時間 (Startup Time, 要件#5) 計測。
# XIAO C5 HID を esptool hard_reset で cold boot → boot banner を host タイムスタンプで捕捉し
# reset→"WiFi Got IP"(通信可能) を測る。全6台×3試行。phase3_findings.md §2.23。
# 使い方: python3 measure.py  (RasPi5、robots は USB-JTAG by-id 接続)。結果 /tmp/startup.txt。
import time, serial, subprocess, re, statistics
MACS={1:'09:10',2:'20:48',3:'23:74',4:'2C:0C',5:'32:A8',6:'41:28'}
ESP="/home/gochiuma/.local/bin/esptool"
def dev(rid): return f"/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_38:44:BE:A5:{MACS[rid]}-if00"
RES=open("/tmp/startup.txt","w",buffering=1)
def log(m): print(m,flush=True); RES.write(m+"\n")
def trial(rid):
    d=dev(rid)
    subprocess.run([ESP,"--port",d,"--after","hard_reset","--connect-attempts","1","chip-id"],
                   stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,timeout=30)
    try: s=serial.Serial(d,115200,timeout=0.1)
    except Exception: return None
    buf=b""; t0=None; t_ip=None; ndisc=0
    end=time.monotonic()+12
    while time.monotonic()<end:
        x=s.read(256)
        if x:
            now=time.monotonic()
            if t0 is None: t0=now
            buf+=x
            while b"\n" in buf:
                ln,buf=buf.split(b"\n",1); txt=ln.decode("utf-8","replace")
                if "Disconnected" in txt: ndisc+=1
                if "Got IP" in txt: t_ip=now; end=now+0.4
    s.close()
    return {"boot_to_ip":(t_ip-t0),"ndisc":ndisc} if (t0 and t_ip) else None
log("########## 起動時間 全6台×3試行 ##########")
allv=[]
for rid in range(1,7):
    for k in range(3):
        r=trial(rid)
        if r: allv.append(r["boot_to_ip"]); log(f"  r{rid} #{k+1}: {r['boot_to_ip']:.2f}s (disconnect {r['ndisc']}回)")
        else: log(f"  r{rid} #{k+1}: 失敗")
        time.sleep(0.5)
log("=== 集計 cold boot→WiFi associated (TEAM_SSID/WPA2) ===")
s=sorted(allv)
log(f"  n={len(s)} mean={statistics.mean(s):.2f}s median={s[len(s)//2]:.2f}s min={s[0]:.2f}s max={s[-1]:.2f}s sd={statistics.pstdev(s):.2f}s")
clean=[v for v in s if v<4.5]; retry=[v for v in s if v>=4.5]
log(f"  クリーン(<4.5s): n={len(clean)} mean={statistics.mean(clean):.2f}s" if clean else "  クリーン: 0")
log(f"  再接続有(>=4.5s): n={len(retry)} mean={statistics.mean(retry):.2f}s" if retry else "  再接続有: 0")
log("########## 完了 ##########")
