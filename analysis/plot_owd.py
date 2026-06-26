import os, argparse, duckdb, bisect, json, collections, numpy as np
import matplotlib; matplotlib.use("Agg"); import matplotlib.pyplot as plt
# --- repo-relative paths (self-contained; runs after `git clone` without editing) ---
HERE=os.path.dirname(os.path.abspath(__file__))
REPO=os.path.dirname(HERE)
ap=argparse.ArgumentParser(description="OWD up/down stats + figure from data/ (README_JP §0/§6)")
ap.add_argument("--data", default=os.path.join(REPO,"data","batch_6r_060hz_6h_w52ch36"),
                help="run directory containing the parquet tables")
ap.add_argument("--out", default=os.path.join(REPO,"results","owd_updown.png"),
                help="output figure path")
A=ap.parse_args()
D=A.data
WIN=120.0
c=duckdb.connect()
# ---- bridge (windowed 120s, 3σ reject) ----
g=sorted(r[0] for r in c.execute(f"SELECT unix_assert FROM read_parquet('{D}/pps_gpio.parquet')").fetchall())
u=c.execute(f"SELECT t_rpid_recv_unix,tsf_us FROM read_parquet('{D}/pps_uart.parquet') ORDER BY 1").fetchall()
P=[]
for tr,tsf in u:
    i=bisect.bisect_left(g,tr); cand=[g[j] for j in (i-1,i) if 0<=j<len(g)]
    if cand:
        a=min(cand,key=lambda x:abs(x-tr))
        if abs(tr-a)<=0.3: P.append((a,a-tsf/1e6))
t0=P[0][0]; buck=collections.defaultdict(list)
for a,off in P: buck[int((a-t0)//WIN)].append((a,off))
seg={}
for b,ps in buck.items():
    x=np.array([p[0] for p in ps]); y=np.array([p[1] for p in ps])
    if len(x)<10: continue
    s,i0=np.polyfit(x-x[0],y,1); r=(y-(i0+s*(x-x[0])))*1e6; m,sd=r.mean(),r.std(); k=np.abs(r-m)<3*sd
    if k.sum()>=10: s,i0=np.polyfit((x-x[0])[k],y[k],1)
    seg[b]=(i0,s,x[0])
keys=np.array(sorted(seg))
def off_vec(t):
    bk=np.floor((t-t0)/WIN).astype(int); idx=np.clip(np.searchsorted(keys,bk),0,len(keys)-1)
    left=np.clip(idx-1,0,len(keys)-1)
    use=np.where(np.abs(keys[idx]-bk)<=np.abs(keys[left]-bk),idx,left); sel=keys[use]
    I=np.array([seg[k][0] for k in sel]);S=np.array([seg[k][1] for k in sel]);X=np.array([seg[k][2] for k in sel])
    return I+S*(t-X)
# ---- 下り SPAN->HID ----
rx=f"read_parquet('{D}/rx_dl.parquet')"; wr=f"read_parquet('{D}/wire.parquet')"
wmin=(f"(SELECT robot_id,cycle_count,min(t_wire_phc) tw FROM {wr} WHERE t_wire_phc>1e9 AND dst<>'192.168.4.255' "
      f"GROUP BY robot_id,cycle_count HAVING max(t_wire_phc)-min(t_wire_phc)<0.1)")
d=c.execute(f"SELECT w.tw,r.t_hid_rx_tsf_us FROM {wmin} w JOIN {rx} r ON w.robot_id=r.robot_id AND w.cycle_count=r.cycle_count WHERE r.t_hid_rx_tsf_us IS NOT NULL").fetchnumpy()
tw=np.asarray(d["tw"],float); h=np.asarray(d["t_hid_rx_tsf_us"],float)
dl=(h/1e6+off_vec(tw)-tw)*1000; dl=dl[(dl>-50)&(dl<20000)]
# ---- 上り HID->wire ----
mr=c.execute(f"SELECT robot_id,json FROM read_parquet('{D}/metrics_raw.parquet') WHERE json LIKE '%\"rx_dlb\"%'").fetchall()
tx={}
for rid,js in mr:
    try:
        o=json.loads(js); tx[(rid,int(o['meta'][10:18],16))]=o['tx']
    except: pass
ulb=c.execute(f"SELECT robot_id,cycle_count,min(t_wire_phc) tw FROM {wr} WHERE dst='192.168.4.255' AND cycle_count IS NOT NULL AND t_wire_phc>1e9 GROUP BY robot_id,cycle_count").fetchall()
c.close()
utw=[];utx=[]
for rid,hs,twv in ulb:
    v=tx.get((rid,hs))
    if v is not None: utw.append(twv);utx.append(v)
utw=np.array(utw,float);utx=np.array(utx,float)
ul=(utw-(utx/1e6+off_vec(utw)))*1000; ul=ul[(ul>-50)&(ul<20000)]

def stt(a): s=np.sort(a); n=len(s); return s[n//2],s[int(n*0.99)],s[-1],a.mean()
dm,dp99,dmax,dme=stt(dl); um,up99,umax,ume=stt(ul)

# ---- README_JP §0/§6 の報告統計を再現出力（全データ + <p99 外れ値除去） ----
def report(name,a):
    p99=np.percentile(a,99); sub=a[a<p99]          # §6.1: <p99 = p99 閾値未満の部分集合
    print(f"[{name}] 全データ      n={len(a):>9,}  Mean {a.mean():6.3f}  Var {a.var():7.3f}  SD {a.std():5.3f}  "
          f"median {np.median(a):6.3f}  p99 {p99:6.2f}  Max {a.max():7.2f}  (>1s {(a>1000).sum()} 件)")
    print(f"[{name}] <p99 除去     n={len(sub):>9,}  Mean {sub.mean():6.3f}  Var {sub.var():7.3f}  SD {sub.std():5.3f}  "
          f"median {np.median(sub):6.3f}")
    # 参考: §6 が定義する p95/p99/p99.9 の 3 段階閾値
    p95,p999=np.percentile(a,95),np.percentile(a,99.9)
    print(f"[{name}] 閾値: p95 {p95:.2f} / p99 {p99:.2f} / p99.9 {p999:.2f} ms")
report("下り SPAN->HID",dl)
report("上り HID->wire",ul)

fig,ax=plt.subplots(1,2,figsize=(11,4))
# (a) histogram bulk 0-20ms
bins=np.linspace(0,20,81)
ax[0].hist(dl,bins=bins,density=True,alpha=.55,color='#2a6fb0',label=f'downlink (SPAN→HID)  med {dm:.2f} / mean {dme:.2f} ms')
ax[0].hist(ul,bins=bins,density=True,alpha=.55,color='#d2691e',label=f'uplink (HID→wire)  med {um:.2f} / mean {ume:.2f} ms')
ax[0].set_xlabel('one-way delay [ms]'); ax[0].set_ylabel('density'); ax[0].set_xlim(0,20)
ax[0].set_title('(a) OWD distribution (6台60Hz・W52・6h)'.replace('台','-robot ').replace('・',' / ')); ax[0].legend(fontsize=8); ax[0].grid(alpha=.3)
# (b) CCDF log-y
def ccdf(a):
    s=np.sort(a); y=1.0-np.arange(len(s))/len(s); return s,y
xs,ys=ccdf(dl); ax[1].plot(xs,ys,color='#2a6fb0',label=f'downlink  p99 {dp99:.1f} / max {dmax:.0f} ms')
xs,ys=ccdf(ul); ax[1].plot(xs,ys,color='#d2691e',label=f'uplink  p99 {up99:.1f} / max {umax:.0f} ms')
ax[1].set_yscale('log'); ax[1].set_xlim(0,60); ax[1].set_ylim(1e-6,1)
for q,lab in [(0.5,'p50'),(0.01,'p99'),(0.001,'p99.9')]:
    ax[1].axhline(q,color='gray',ls=':',lw=.7); ax[1].text(58,q,lab,fontsize=7,va='center',ha='right',color='gray')
ax[1].set_xlabel('one-way delay [ms]'); ax[1].set_ylabel('CCDF  P(OWD > x)')
ax[1].set_title('(b) tail (CCDF, log-y)  downlink max 370 ms は範囲外'.replace('は範囲外',' off-chart')); ax[1].legend(fontsize=8); ax[1].grid(alpha=.3,which='both')
plt.tight_layout()
os.makedirs(os.path.dirname(A.out),exist_ok=True)
plt.savefig(A.out,dpi=130); print("saved",A.out)
