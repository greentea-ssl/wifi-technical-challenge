# Live 計測パイプライン + WebUI (v2 実装まとめ)

`docs/measurement_pipeline_v2.md` の設計を実装し、**SQLite live store + cycle_count キー +
PPS bridge 解析 + FastAPI/SSE ダッシュボード**で、下り (AIPC→HID) OWD を**区間分解して
リアルタイム表示**するところまでを実機検証した。本書は構成・起動手順・区間定義・知見の
運用リファレンス。

## 1. 構成

```
[AIPC(.4.160)] ──UDP downlink (cycle_count)──→ [Xikestor SW] ─→ [LN6001 AP ch112] ─air→ [HID]
  pc_emulator (AI模擬)        │ SPAN mirror                              │
                              ↓                                          │ air 観測
                   [RasPi5 .4.212]                                       │
                     ├ eth0 (SPAN受信, AF_PACKET, t_wire)  ← WireReader   │
                     ├ /dev/ttyUSB0 sniffer C5 (PROMIS, tsf_us=t_air) ←───┘
                     ├ /dev/pps0 (sniffer GPIO PPS → TSF↔unix bridge)
                     └ daemon(gtnlv_rpid) → SQLite live store(/dev/shm) → FastAPI GUI(:8501)
[HID] = devkit C5 に SanRei_HID firmware (robotId 0, 40000 listen / 52000 rx_dl broadcast)
```

- **daemon A** (`tools/rpi_daemon/gtnlv_rpid.py`): 収集 + 記録。各 source thread (MetricsListener
  52000+id / SnifferReader UART / PpsGpioReader / UplinkListener / **WireReader** eth0) → `store.py`
  の Recorder → **LiveSink(SQLite tmpfs ring)** + **RecordSink(永続CSV)**。watchdog / 録画制御 socket 付き。
- **GUI** (`tools/dashboard/`): FastAPI(`server.py`, uvicorn) + SSE。`datasource.py` が SQLite live を
  直読みし PPS bridge + cycle join で区間 OWD を算出。フロントは uPlot(時系列) + Mermaid(網図)。
  fastapi 不在環境向けに stdlib 版 `serve.py` も同梱。

## 2. 起動手順 (RasPi `gochiuma@192.168.4.212`)

```bash
# daemon (sudo: --wire の AF_PACKET と --iface の SO_BINDTODEVICE に root 必要)。tmux 常駐。
tmux new -d -s rpid 'sudo python3 -u ~/gtnlv/gtnlv_rpid.py \
  --robot-ids 0 --duration 0 --live --live-db /dev/shm/gtnlv_live.db \
  --ctrl-sock /tmp/gtnlv.sock --sniffer-port /dev/ttyUSB0 --pps-device /dev/pps0 \
  --wire eth0 > /tmp/gtnlv_daemon.log 2>&1'

# GUI (venv に fastapi/uvicorn 導入済 = ~/.venv-dashboard)。tmux 常駐。
tmux new -d -s gui 'cd ~/gtnlv/dashboard && GTNLV_CTRL_SOCK=/tmp/gtnlv.sock \
  ~/.venv-dashboard/bin/uvicorn server:app --host 0.0.0.0 --port 8501 > /tmp/gui.log 2>&1'
```
```bash
# AIPC (host) で AI 模擬送出 (robotId に一致させる、HID=robotId0→port40000)
python3 tools/pc_emulator/pc_emulator.py --robot-id 0 --target 192.168.4.111 \
  --port 40000 --rate 100 --duration 0
```
ブラウザ: `http://192.168.4.212:8501` (LAN) / `http://100.85.248.111:8501` (tailscale) /
Cloudflare Tunnel。**停止**: `pkill -f gtnlv_rpid; pkill -f 'uvicorn server:app'` + host の pc_emulator。

## 3. 計測区間の定義

時刻点 (すべて cycle_count + robot_id で join):

| 記号 | 取得元 | 意味 |
|---|---|---|
| `t_tx` (corr_unix) | payload offset 38-45 (AIPC clock) | AI が送出した時刻 |
| `t_wire` | RasPi eth0 SPAN, SO_TIMESTAMPING(SW) = CLOCK_REALTIME | フレームが有線で AP に到達した時刻 |
| `t_air` | sniffer の AP-TSF (`tsf_us`) → PPS bridge で unix 化 | AP が air へ送出した時刻 (sniffer 観測) |
| `t_hid` | HID rx の AP-TSF (`t_rx_tsf_us`) → PPS bridge で unix 化 | HID が受信記録した時刻 |

区間:

| 区間 | 式 | 意味 | 注意 |
|---|---|---|---|
| **①host+有線** | `t_wire − t_tx` | AI送出→AP有線到達 | **AIPC↔RasPi クロック比較**。sub-ms で整合精度(±0.2ms)未満、符号がぶれる(負値あり) |
| **②AP滞留** | `t_air − t_wire` | 有線到達→air送出 = AP内queue | RasPi内部完結。**sniffer cb/TSF ジッタも混入** |
| **③air→HID** | `t_hid − t_air` | air送出→HID受信記録 = **HID内部rx処理** (PHY→app/metrics) | t_air(sniffer)依存 |
| **total** | `t_hid − t_wire` (=②+③) | **①除外、sniffer非依存(t_air相殺)で最も確実** | GUI の total はこれ。①含む値は `total_full` に保持 |

### 3.1 上り OWD 区間分解 (HID → air → wire、2026-06-01)

下りと対称に、上りも radio_metrics の `meta`(hid_seq) で多点 join して分解する。

| 区間 | 式 | 意味 |
|---|---|---|
| **① HID→air** | `t_air − t_hid` | HID 生成 (hb=`t_now_tsf_us` / tx_ul=`t_tx_tsf_us`) → 空中送出。sniffer が **HID の ToDS 原送信** (802.11 addr2=HID) を観測 |
| **② air→wire** | `t_wire − t_air` | 空中送出 → AP 有線到達 (SPAN)。**AP 受信処理+転送** (正値) |
| **total** | `t_wire − t_hid` | = ①+② |

- **join key = `hid_seq`** (`meta`、payload offset 9 固定 HEX、`radio_metrics.md` §3.0)。sniffer / WireReader が offset 9 を parse して `cycle_count` 列に格納し、socket 受信 JSON と突合 (`datasource._compute_uplink_legs`)。
- **hb (1Hz) は CU 無しでも流れる**ので本番 SanRei_HID のまま測定可。`tx_ul` は CU 接続時のみ。datasource は tx_ul 不在でも hb から legs 算出。
- **sniffer は ToDS 原送信を使う**こと: AP の FromDS 再送 (addr2=BSSID) を air にすると AP egress のため `air→wire` が負値になる (`sniffer.ino` stage3/4 を双方向化済)。
- 実測 (SanRei_HID hb 経由、idle): **① 0.53ms / ② +0.35ms / total 0.86ms**。
- DL と UL の差: DL は air/wire が AP を挟む (=AP滞留) が、UL は直列で `air→wire`=AP 受信+転送。

## 4. クロック・計測上の注意

- **AIPC↔RasPi 時刻同期**: AIPC chrony source = RasPi (`/etc/chrony/sources.d/*.sources` の
  `server 192.168.4.212`、RasPi IP 変更時は要更新)。未同期だと total/①が ~数十ms ずれる
  (実際に .103 stale で 20ms ずれた事例)。`makestep` でステップ補正される。
- **① は参考値**: host+有線は sub-ms で AIPC↔RasPi 整合精度より小さく、符号がぶれる。
  精密化には eth0 PHC ハードタイムスタンプ + AIPC 側 PTP が要る。**total は ①除外(②+③)** が既定。
- **② は sniffer ジッタ込み**: `t_air` は sniffer の promiscuous cb / TSF 取得時刻。確実な総和は
  `total=②+③=t_hid−t_wire` (t_air 相殺で sniffer 非依存)。
- **③ を純 HID 内部処理で測る**には HID の `t_hid_rx_esp_us` (esp_timer) 利用余地 (未実装、§6.1)。

## 5. 知見 (実機)

- **AP滞留がビーコン周期で倍増**: total(sniffer非依存)の自己相関が lag≈11/22/33 cycle (100Hz→
  ~102ms) に強ピーク → **WiFi ビーコン間隔(102.4ms=100TU)同期で AP がフレームを溜めてバースト送出**。
  実 AP queue であり計測ミスではない。②③は弱い正相関(+0.24、t_air が②③で逆符号なので正相関=実バーストの証拠)。
- **SanRei_HID で ③(HID内部rx)短縮**: reflector mock 0.84ms → 実HID ~0.5ms (FreeRTOS task 化 commit
  44494c0 の効果が実測で確認)。
- **二重受信は NIC bind で解消**: wlan0 を計測 subnet に載せると broadcast を eth1+wlan0 で 2x 受信
  (1.99x)。daemon `--iface eth1` (SO_BINDTODEVICE) で 1.00x。
- **sniffer cycle_count parse 負荷**: dst filter 後ろに parse を置き、1000Hz/captured16万でも dropped=0。
- **混雑下完全性**: iperf3 120Mbps 並走でも dropped=0 / loss 0% / cycle 一致 100%。

## 6. GUI 機能

- **🔴 Live**: Mermaid 網図 (AIPC→[SPAN受信→AP→sniffer観測]→HID、subgraph で「②AP滞留=t_air−t_wire」
  を SPAN-sniff 取得として明示) + **総遅延+区間 重ねチャート** (total/②/③ を同一時間軸、uPlot、
  **カーソルで該当 cycle_count+robot_id を下部表示**) + ①参考チャート + パケット送信状況 + 録画制御。
- **📊 Overview**: 過去 run (record CSV) の 7項目 KPI。 **🔁 Raw**: run 内 CSV 一覧。
- SSE push (1s)。`Cache-Control: no-store` + アセット `?v=` で Cloudflare エッジキャッシュ対策
  (asset 更新時は `?v=` を上げる)。

## 7. 主要ファイル

| file | 役割 |
|---|---|
| `tools/rpi_daemon/store.py` | LiveSink(SQLite)/RecordSink(CSV)/Recorder facade |
| `tools/rpi_daemon/gtnlv_rpid.py` | daemon: 収集 thread + WireReader + watchdog + 録画 socket。`--live/--record/--iface/--wire/--ctrl-sock` |
| `tools/dashboard/server.py` | FastAPI: runs/summary/live_summary/SSE/録画制御 |
| `tools/dashboard/datasource.py` | SQLite live 読み + PPS bridge + cycle join 区間算出 (`_compute_legs`) |
| `tools/dashboard/serve.py` | stdlib フォールバック HTTP/SSE サーバ |
| `tools/dashboard/static/{index.html,app.js,charts.js,style.css}` | フロント (uPlot/Mermaid) |
| `tools/dashboard/static/components/{uPlot,mermaid}` | 同梱 (offline) |
| `SanRei_HID/src/ESP32C5Controller/metrics_radio.cpp` | rx_dl を cycle_count(51-53) 対応 (別リポジトリ) |

## 8. 既知の制約・今後

- **① host+有線の精密化**: eth0 PHC ハードタイムスタンプ利用 (現状 SW timestamp)。
- **③の純HID内部化**: HID `t_hid_rx_esp_us` (esp_timer) で sniffer 非依存に。
- **reflector→SanRei_HID 差替に伴う robotId**: flash で NVS 初期化され robotId=0 (40000/52000)。
- **Mermaid live 再描画**: 1s ごと再 render。重い場合はラベルのみ DOM 更新に変更余地。
- **Streamlit `app.py`**: FastAPI 版に置換済。ブラウザ確認完了後に撤去予定。
- AP滞留のビーコン要因の切り分け: HID `WiFi.setSleep(false)` で DTIM/省電力か A-MPDU かを判別可。

## 関連
- 設計: `docs/measurement_pipeline_v2.md` / `docs/measurement_architecture.md` / `docs/pps_sync_design.md`
- 実績/数値: `docs/phase3_findings.md` / `docs/rebuild_overnight_plan.md`
