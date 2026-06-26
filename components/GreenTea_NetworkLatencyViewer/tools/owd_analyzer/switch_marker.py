#!/usr/bin/env python3
# HID の SWMARK(recv=set_ssid受信, conn=GOT_IP) を ttyACM から読み、
# set_ssid トグルで recv->conn の on-device millis 差 = 切替時間を測る。
import serial,threading,time,subprocess,re,statistics,sys
SET="/home/gochiuma/GreenTea_NetworkLatencyViewer/tools/set_ssid/set_ssid.py"
PORTS={1:"/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_38:44:BE:A5:09:10-if00",
       2:"/dev/serial/by-id/usb-Espressif_USB_JTAG_serial_debug_unit_38:44:BE:A5:20:48-if00"}
events={r:[] for r in PORTS}  # (host_t, kind, dev_ms)
stop=threading.Event()
def rdr(r,p):
    try: s=serial.Serial(p,115200,timeout=0.3)
    except Exception as e: print(f"r{r} open失敗 {e}"); return
    buf=b""
    while not stop.is_set():
        d=s.read(512)
        if d: buf+=d
        while b"\n" in buf:
            ln,buf=buf.split(b"\n",1)
            m=re.search(rb"SWMARK (recv|conn) (\d+)",ln)
            if m: events[r].append((time.time(),m.group(1).decode(),int(m.group(2))))
    s.close()
for r,p in PORTS.items(): threading.Thread(target=rdr,args=(r,p),daemon=True).start()
time.sleep(2)
seq=sys.argv[1:] or ["normal","open","normal","open","normal","open"]
print(f"seq={seq}",flush=True)
for mode in seq:
    subprocess.run(["python3",SET,"--robots","1,2,3,4,5,6","--mode",mode],timeout=15,capture_output=True)
    time.sleep(14)
stop.set(); time.sleep(0.5)
# pair: 各 recv の後の最初の conn (dev_ms 差)
print("robot | switch# | recv->conn (ms, on-device)",flush=True)
res={r:[] for r in PORTS}
for r in PORTS:
    ev=events[r]; i=0; sw=0
    while i<len(ev):
        if ev[i][1]=="recv":
            rms=ev[i][2]
            j=i+1
            while j<len(ev) and ev[j][1]!="conn": j+=1
            if j<len(ev):
                d=ev[j][2]-rms; sw+=1
                if 0<d<60000: res[r].append(d); print(f"r{r} | {sw} | {d}",flush=True)
                i=j
        i+=1
print("--- 集計 (recv->conn ms) ---",flush=True)
alld=[]
for r in PORTS:
    if res[r]:
        alld+=res[r]; print(f"r{r}: n={len(res[r])} mean={statistics.mean(res[r]):.0f} max={max(res[r])} min={min(res[r])}"+(f" sd={statistics.pstdev(res[r]):.0f}" if len(res[r])>1 else ""),flush=True)
if alld: print(f"全体: n={len(alld)} mean={statistics.mean(alld):.0f}ms max={max(alld)}ms min={min(alld)}ms",flush=True)
print("done",flush=True)
