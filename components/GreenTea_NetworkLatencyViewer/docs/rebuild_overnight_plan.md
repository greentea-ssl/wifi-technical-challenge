# パイプライン v2 リビルド — 今晩の作業・試験計画 (2026-05-30 夜)

> `docs/measurement_pipeline_v2.md` の設計を実装に落とす夜間 runbook。
> **収集+記録 (daemon A) を SQLite live store 化**し、cycle_count キー・録画制御・
> NIC bind・負荷試験までを一気に通す。各 Stage は「作業 → 検証 → 合否基準」。
> GUI (FastAPI) は下回り完成後 = 最後 / 時間が余れば。

## 0. 今晩のゴール

| 優先 | 到達点 |
|---|---|
| **必達** | Stage 1 (SQLite live store + sink 分離) + Stage 3b (NIC bind で二重受信解消) が live ランで動く |
| **必達** | Stage 2 cycle_count 配線 (pc_emulator / sniffer.ino / wire_capture) + **2e 負荷試験 dropped=0** |
| 推奨 | Stage 4 録画制御 + pre-roll が socket から動く |
| 余れば | Stage 5 計測部 daemon B (live PPS bridge / OWD)、Stage 6 GUI SQLite 化 |

## 1. 安全方針 (現 daemon を壊さない)

- **現 `gtnlv_rpid.py` は git で復帰可能に保つ**。着手前に作業ブランチ + コミット境界を切る。
- 新 sink (SQLite) は **既存 CSV writer と並行追加**し、`--live`/`--record` で選べるようにする。旧 `--out-dir`/`--keep-recent-s` は当面残し、回帰時に即旧経路へ戻せる状態を維持。
- firmware (sniffer.ino) は **flash 前に現バイナリを退避**。NG なら即 reflash で戻す。
- 提出用の計測能力を常に確保 (旧経路 = 現状の overnight 構成がいつでも走る)。

## 2. 環境・機材 (着手前チェック)

**確定構成 (今晩、これまでと不変)**: ADALM2000 = **host (AIPC) 接続** / sniffer + HID(reflector C5) = **RasPi 接続**。

| 機材 | 接続 | 確認コマンド |
|---|---|---|
| 計測 RasPi5 | `gochiuma@192.168.4.212` (eth1、sudo NOPASSWD) | `ssh gochiuma@192.168.4.212 'uname -a; ip -br a'` |
| sniffer C5 | RasPi `/dev/ttyUSB0` (2Mbps)、GPIO10 PPS → BCM18 + ADALM2000 ch1 | `ssh … 'ls -l /dev/ttyUSB*'` |
| HID (reflector C5、HID 模擬) | RasPi `/dev/ttyUSB1`、GPIO10 PPS → ADALM2000 ch2 | 同上 |
| PPS GPIO | `/dev/pps0` (sniffer GPIO10 → BCM18) | `ssh … 'cat /sys/class/pps/*/name; sudo ppstest /dev/pps0'` (1Hz event) |
| eth0 PHC | SPAN dst | `ssh … 'ethtool -T eth0'` |
| **ADALM2000** | **host (AIPC) USB**、sniffer/HID の GPIO10 PPS 2ch を観測 | `iio_info -s` / `python3 -c "import libm2k; print(libm2k.getAllContexts())"` |
| AIPC | `192.168.4.160` (br0)、pc_emulator + ADALM2000 | `python3 tools/pc_emulator/pc_emulator.py --help` |

> ⚠ pps/ttyUSB の番号は boot 順で入替あり (`docs/lessons_learned.md` §C.26)。着手前に実値を確認し、以降のコマンドへ反映。
> ADALM2000 が host 側にあるため、**負荷試験中に PPS Δt を並走測定**できる (§4.1)。

## 3. Stage 別 作業 + 検証

### Stage 1 — SQLite live store + sink 分離 (必達 / 最大の山)

**作業**
- 1a. スキーマ定義 (新規 `tools/rpi_daemon/store.py`):
  - `dl(cycle_count INT, robot_id INT, t_tx_unix, t_wire_phc, t_air_tsf, t_air_recv_unix, t_hid_rx_tsf, t_hid_rx_esp, corr_unix_time, rssi, frame_size, ingest_unix, PRIMARY KEY(cycle_count, robot_id))`
  - `ul(robot_id, ul_seq, hid_seq, tx_port, t_hid_tx_tsf, t_air_tsf, t_air_recv_unix, t_recv_unix, frame_size, ingest_unix, PRIMARY KEY(robot_id, ul_seq))`
  - `timesync(t REAL PRIMARY KEY, kind TEXT, unix_assert, tsf_us, esp_us, ...)`
  - 各表 `CREATE INDEX ... ON ...(ingest_unix)`
- 1b. `LiveSink` (SQLite, `/dev/shm/gtnlv_live.db`, WAL off / journal=memory, 1s ごと `DELETE WHERE ingest_unix < now-keep` で ring) + `RecordSink` (既存 CSV/Parquet 永続) を pluggable に
- 1c. `gtnlv_rpid` の各 writer を「統一 event → sink 振り分け」に refactor (collector/recorder 分界)
- 1d. フラグ `--record <DIR>` / `--live [--live-keep-s 300]` 追加 (両省略はエラー)

**検証**
```bash
# RasPi で 60s live ラン (sniffer + PPS)
ssh gochiuma@192.168.4.212 'cd ~/gtnlv && \
  timeout 65 python3 -u gtnlv_rpid.py --robot-ids 0 --live --live-keep-s 30 \
    --sniffer-port /dev/ttyUSB0 --pps-device /dev/pps0'
# 別端末: AIPC で pc_emulator 100Hz 並走
python3 tools/pc_emulator/pc_emulator.py --robot-id 0 --target 192.168.4.111 --port 40000 --rate 100 --duration 60
# ring 確認 (keep 30s なので件数が頭打ちになるはず)
ssh gochiuma@192.168.4.212 'for i in 1 2 3; do \
  sqlite3 /dev/shm/gtnlv_live.db "SELECT count(*) FROM dl;"; sleep 10; done'
# SD 非消費確認 (db は tmpfs のみ、SD root は増えない)
ssh gochiuma@192.168.4.212 'df -h /dev/shm /; ls -la /dev/shm/gtnlv_live.db'
```
**合否**: `dl` 件数が keep 窓 (≈30s×100Hz=3000) 付近で頭打ち / db は `/dev/shm` のみ / SD 使用量増加なし。

### Stage 2 — cycle_count 配線 + 負荷試験

**作業**
- 2a. `tools/pc_emulator/pc_emulator.py`: payload offset 51-53 に 24bit cycle_count (送出ごと +1)、aipc_seq 廃止
- 2b. `tools/esp_firmware/sniffer/sniffer.ino`: **dst filter 通過後**に payload 51-53 を抽出し binary record に追加 (詳細 §4.1)
- 2c. `tools/rpi_daemon/wire_capture.py`: mirror frame の 51-53 (cycle) + 38-45 (t_tx_unix) + PHC hwtstamp 抽出
- 2d. (後ろ倒し可) HID `metrics_radio`: rx_dl に cycle_count

**検証** (機能)
```bash
# pc_emulator が cycle_count を載せているか (wire 側で確認)
ssh gochiuma@192.168.4.212 'cd ~/gtnlv && python3 wire_capture.py --iface eth0 --count 20 --print'
# → cycle_count が +1 ずつ単調増加していること、sniffer.csv 相当に cycle 列が入ること
```
**合否**: DL の cycle_count が AIPC 送出と一致し単調増加、sniffer/wire 双方で同 cycle が取れる。

### Stage 2e — ★ sniffer cycle_count parse 負荷試験 (必達、詳細 §4.1)

### Stage 3 — robustness

**作業**
- 3a. thread watchdog: 各 reader の `last_event_t` を監視 thread が見て、無音 → log (live なら再起動)
- 3b. **★ 計測 socket を `SO_BINDTODEVICE` で `--iface eth1` 固定** (二重受信の本対策、詳細 §4.2)
- 3c. LRU dedup は **任意 fallback** (default 無効、bind 不可環境のみ)

### Stage 4 — 録画制御 + pre-roll (推奨)

**作業**
- 4a. daemon A に制御専用 thread + unix socket (`/run/gtnlv-rpid.sock`、`start_record`/`stop_record`/`status`)
- 4b. RecordSink を per-event bool で tee トグル化 (capture hot path 不変)
- 4c. pre-roll: `start_record` 時に live ring の現バッファを record へ flush

**検証** (詳細 §4.4)

### Stage 5 — 計測部 daemon B (余れば)

- 5a. `PpsBridgeEstimator` (rolling 線形回帰) / 5b. `OwdComputer` (cycle join 4 leg) / `LossAnalyzer`
- 5c. live SQLite を読み派生表へ。record path は既存 batch (analyze.py / sniffer_bridge.py) 維持

### Stage 6 — GUI SQLite 化 (時間が余れば)

- 6a. `tools/dashboard/datasource.py` を SQLite reader に (SELECT window)
- 6b. 録画ボタンを §4 socket に接続 / 6c. uPlot vendoring + ブラウザ確認、Streamlit `app.py` 撤去

---

## 4. 重点試験 (詳細)

### 4.1 sniffer cycle_count parse 負荷試験 ★

**狙い**: payload 解釈追加で promiscuous 取りこぼし (drop) が増えないことを実証。

**設計上の前提** (これを守れば負荷は計測対象トラフィックにしか乗らない):
- parse は **cb 段4 の dst filter 通過後のみ**実行 (broadcast + 許可 OUI/MAC)。ノイズ frame は parse しない。
- hot path (callback/ISR 文脈) に重処理を入れない。callback → ring (RING_N=2048) → main task emit の構成維持。
- offset は **可変長前提**で算出 (QoS data 26B + LLC/SNAP 8B + IPv4 20B + UDP 8B を飛ばし payload 起点、+51 で cycle_count)。固定 offset にしない。

**手順**
```bash
# sniffer (parse 有効 firmware) 単独 DIAG をまず確認
ssh gochiuma@192.168.4.212 'timeout 15 cat /dev/ttyUSB0 | strings | grep -E "DIAG|dropped"'
# worst case: 計測対象 broadcast を高レートで浴びせ parse 回数を最大化
#   複数 robot_id × 高 rate で対象 frame を増やす
for id in 0 1 2 3; do \
  python3 tools/pc_emulator/pc_emulator.py --robot-id $id --target 192.168.4.255 \
    --port $((40000+id)) --rate 1000 --duration 120 & done
# さらに §2.19 混雑を重畳 (RasPi 自身が iperf3 で AP を圧迫)
ssh gochiuma@192.168.4.212 'iperf3 -c 192.168.4.1 -u -b 30M -P 4 -t 120 &'
# sniffer_hb の dropped_total 推移を記録
ssh gochiuma@192.168.4.212 'cd ~/gtnlv && timeout 130 python3 -u gtnlv_rpid.py \
  --robot-ids 0,1,2,3 --live --live-keep-s 30 --sniffer-port /dev/ttyUSB0'
ssh gochiuma@192.168.4.212 'sqlite3 /dev/shm/gtnlv_live.db \
  "SELECT max(ingest_unix), count(*) FROM dl;"'
```
**ADALM2000 で PPS Δt を並走測定** (host 側にあるので可能):
```bash
# 負荷試験と同時に sniffer/HID GPIO10 PPS の Δt を連続記録
python3 tools/m2k_pps_diff/pps_diff.py --duration 130 --out /tmp/dt_parseload.csv
python3 tools/m2k_pps_diff/analyze.py /tmp/dt_parseload.csv
```
→ parse 負荷投入前後で **Δt の median / sd が悪化しないか**を確認 (§2.18 idle sd 14μs、§2.19 混雑 sd 23μs が基準)。parse が sniffer 主タスクの PPS dispatch を乱すと Δt sd が増えるため、これが「parse が firmware を律速していない」second の独立指標になる。

**合否基準**:
- sniffer `dropped_total` = 0 (運用レート、できれば 2× headroom)
- parse 無効版と `cb_total` (処理 frame 数) が同等 (parse が callback を律速していない)
- **PPS Δt sd が parse 無効時と同等** (ADALM2000、混雑時 §2.19 の 23μs 程度を超えて悪化しない)
- UART record 欠落なし、SQLite dl 書込が送出数に追随
- NG 時: parse を main task 側へ後退 / dst filter をさらに絞る / RING_N 拡大を検討

### 4.2 二重受信 NIC bind 検証 ★

**狙い**: `SO_BINDTODEVICE` で eth1 限定すれば、wlan0 が同一 subnet でも 2x にならないことを実証。

**手順**
```bash
# 二重受信を再現: wlan0 を計測 subnet (TEAM_SSID_OPEN) に載せる (raspi_setup §10)
ssh gochiuma@192.168.4.212 'nmcli dev wifi connect TEAM_SSID_OPEN; ip -br a'
# (A) --iface 無し → 2x を観測
ssh gochiuma@192.168.4.212 'cd ~/gtnlv && timeout 30 python3 -u gtnlv_rpid.py \
  --robot-ids 0 --live --sniffer-port /dev/ttyUSB0'  # 件数を記録
# (B) --iface eth1 → 1x
ssh gochiuma@192.168.4.212 'cd ~/gtnlv && timeout 30 python3 -u gtnlv_rpid.py \
  --robot-ids 0 --live --iface eth1 --sniffer-port /dev/ttyUSB0'
# 送出は AIPC 100Hz × 30s = 3000 を基準に比較
```
**合否基準**: (A) ≈ 6000 (2x) → (B) ≈ 3000 (1x、送出数一致)。
**注意**: `SO_BINDTODEVICE` は root/`CAP_NET_RAW` 必須。daemon を sudo か `setcap cap_net_raw+ep`。権限無し時は warning + skip。

### 4.3 SQLite ring / SD 非消費 検証

Stage 1 検証に同じ。加えて **長め (10 min) ラン**で db ファイルサイズが keep 窓相当で安定 (単調増加しない) ことを確認。

### 4.4 pre-roll 検証

```bash
# live (録画 off) で ring を満たす → socket で start_record → 手前 N 秒が record に入るか
ssh gochiuma@192.168.4.212 'cd ~/gtnlv && python3 -u gtnlv_rpid.py --robot-ids 0 \
  --live --live-keep-s 60 --record ~/runs/preroll_test --sniffer-port /dev/ttyUSB0 &'
sleep 30   # ring に 30s 分溜める (録画はまだ off)
ssh gochiuma@192.168.4.212 'echo "{\"cmd\":\"start_record\",\"tag\":\"preroll\"}" | \
  socat - UNIX-CONNECT:/run/gtnlv-rpid.sock'
sleep 10
# record 側の最古 timestamp が start_record より ~N 秒前か
ssh gochiuma@192.168.4.212 'head -3 ~/runs/preroll_test/owd_dl.csv'
```
**合否**: record の最古行が `start_record` 時刻より前 (= pre-roll が効いている)。

### 4.5 end-to-end 通し (live + 録画)

`--live --record --iface eth1` で 5 min、pc_emulator 100Hz + PPS。SQLite live が SELECT でき、record CSV が永続化、dropped=0、二重受信無し、を一括確認。

---

## 5. 優先順位と cut-line (部分完了でも動く状態)

| 完了ライン | 状態 |
|---|---|
| Stage 1 だけ | SQLite live は動くが cycle_count 未配線 (aipc_seq 併用)。実用可 |
| Stage 1+2+2e | **今晩の最低ライン**。cycle_count キー + 負荷実証済 |
| +3b | 二重受信解消、混雑試験を安心して実施可 |
| +4 | UI 起点録画 + pre-roll (socket 経由) |
| +5,6 | 計測 live + 新 GUI (翌日以降で可) |

各 Stage 末で **コミット境界**を切り、途中中断でも旧経路へ戻せること。

## 6. タイムライン目安 (夜間)

```
00:00  環境チェック (§2)                          15 分
00:15  Stage 1 SQLite store + sink                 90 分 ← 山
01:45  Stage 1 検証 (60s live + ring 確認)         15 分
02:00  Stage 2 cycle_count 配線 (pc_em/sniffer/wire) 60 分
03:00  Stage 2e 負荷試験 (§4.1)                     30 分
03:30  Stage 3b NIC bind + §4.2 検証               30 分
04:00  Stage 4 録画制御 + pre-roll + §4.4          45 分
04:45  §4.5 通し試験                               20 分
05:05  コミット整理 / 余れば Stage 5,6             —
```

## 7. ロールバック手順

- 旧経路復帰: `--live`/`--record` を使わず従来 `--out-dir`/`--keep-recent-s` で起動 (新 sink を bypass)。
- firmware NG: 退避した旧 sniffer バイナリを `esptool write-flash` で戻す。
- git: 各 Stage コミットを `git revert` / 作業ブランチを破棄。
- 提出計測は常に旧 overnight 構成 (`docs/phase3_findings.md` §2.20 の手順) が走る前提を崩さない。

## 進捗ログ (overnight 実績 2026-05-30 深夜)

### ✅ Stage 1 完了・実機検証済 (commit 82effd2)
- `tools/rpi_daemon/store.py` 新規: `LiveSink`(SQLite tmpfs ring) / `RecordSink`(永続CSV) / `Recorder` facade
- `gtnlv_rpid.py`: `--live`/`--live-keep-s`/`--live-db`/`--record`/`--iface` 追加、write_* を Recorder 経路化、**legacy RotatingCSVWriter は温存=即ロールバック可**、rx_dl に cycle_count/t_hid_rx_esp_us 追加、`SO_BINDTODEVICE`(`bind_to_iface`)、**ログ日本語化**
- **RasPi 60s 実機検証** (sniffer /dev/ttyUSB0 + /dev/pps0 + host pc_emulator 100Hz→.4.111):
  - SQLite ring: rx_dl ~3900 / sniffer_frame ~7900 で**頭打ち**(keep=20s)、pps_gpio/pps_uart/sniffer_hb=各 ~20 (1Hz steady)
  - record CSV 一式生成 (owd_dl 8248 / sniffer 16626 行)、**dropped_total=0**、pps_bridge.csv (n=60) 生成
  - db 2.6MB (tmpfs)、**SD root 不変=SD 非消費** ✅
  - 旧 daemon backup: RasPi `~/gtnlv/gtnlv_rpid.py.bak.*`

### ◐ Stage 2 (cycle_count 配線) — コード完了・compile 済、**flash と実機検証は朝**
- ✅ **2a pc_emulator** (commit 6bf0b26): offset を spec に厳密一致 (46-47 esys_x / 48-49 esys_y / 50 kick / 51-53 cycle_count 24bit)、aipc_seq 廃止。旧実装は 1 byte ずれていた。offset/checksum/wrap をローカル検証済
- ✅ **2b sniffer.ino + 2d metrics_radio** (commit 915eb94): metrics_radio は rx_dl を cycle_count(51-53) parse・JSON 出力。sniffer は stage4 後のみ 802.11→LLC/SNAP→IPv4→UDP を可変長で飛ばし payload 51-53 を **bounds-check 付き**で parse (Entry 42→46B、record v2=48B)。**arduino-cli compile 通過** (両 Flash 77%)
- ✅ **daemon v1/v2 後方互換** (915eb94): frame record を rlen で v1(44)/v2(48) 分岐、cycle_count 列追加。現行 flashed sniffer (v1) のままでも壊れない
- ✅ **flash + 実機 end-to-end 検証 完了** (2026-05-30 朝): sniffer/reflector を fresh merged.bin で flash (esptool v5.2.0、hash verified)、v2 daemon を RasPi deploy。robot-id 1 / 100Hz / 30s ランで:
  - **rx_dl 1523 件すべてに cycle_count** (reflector 新 metrics_radio)、連番 [0..1522] **欠落 0**
  - **sniffer_frame 6441 件に cycle_count** — ★ **sniffer は HE data frame の UDP payload を promiscuous で取得可と実証** (懸案解消)
  - **rx_dl ∩ sniffer_frame 一致率 99.9%** (1522/1523)
  - **sniffer dropped_total=0** (cycle_count parse 追加後も取りこぼし無し) → §4.1 負荷試験の前提を実証
  - record CSV (owd_dl.csv) に cycle_count 列、pps_bridge.csv (n=40) 生成
- ⚠ **flash で reflector の NVS 初期化 → robotId が 0→1 に変化** (firmware default)。reflector は今 **40001 待受 / 52001 broadcast**。`run_live.sh` の `ROBOT_ID=0` 既定や旧運用は **robot-id 1 に合わせる** か、reflector の robotId を 0 に再設定する必要あり。sniffer 側は影響なし
- 観察: LAN 上に **旧形式 (aipc_seq) の downlink 送信元が併存** している様子 (sniffer が別レンジの cycle 値を拾う)。本走前に旧 pc_emulator/旧 firmware の残存を一掃すること
- ビルド成果物の注意: `arduino-cli compile` (--output-dir 無し) は cache に出力し sketch 内 `build/` の .bin は更新されない。flash 時は **`--output-dir` で fresh ビルドを強制**してから merged.bin を焼く

### ✅ Stage 4 (録画制御 socket + pre-roll) — 実機検証済
- `store.py` に LiveSink.snapshot / RecordSink.put_force / Recorder.start_record・stop_record・status、`gtnlv_rpid.py` に ControlServer (AF_UNIX、JSON 1行) + `--ctrl-sock`/`--record-base`
- **Python 3.13 で `threading.Thread._handle` 内部属性と衝突**しメソッドが落ちるバグを発見・修正 (`_handle`→`_dispatch`、commit)。RasPi 3.13.5 で status→start_record(pre-roll 93行、`~/runs/<tag>` on-demand 生成)→stop の socket 動作を実機確認

### ✅ §4.1 sniffer parse 負荷試験 — dropped=0 実証
- 新 sniffer (cycle_count parse) + pc_emulator **1000Hz** で captured_total=162,415 を処理して **sniffer dropped_total=0**。rx_dl 15514 件全て cycle_count、**rx_dl∩sniffer 一致 100%**。parse を dst filter 後ろに置く設計が高負荷で有効と実証
- ✅ **iperf3 混雑重畳も実施** (wlan0 を TEAM_SSID_OPEN に切替): RasPi wlan0→host で **UDP 120 Mbps** (4 stream) を AP に流し込んだ状態で、HID 100Hz + sniffer 並走。結果: **sniffer dropped=0** (captured 384,367)、**rx_dl 欠落0 / loss 0.000%** (cycle [0..2391] 完全連番)、rx_dl∩sniffer 一致 100%。混雑下でも v2 パイプラインは取りこぼし・損失・cycle 不整合ゼロ
- 未実施: ADALM2000 PPS Δt の混雑下並走 (reflector の GPIO PPS 出力有無が未確認 + 時間。§2.19 で sd 23μs と既測)

### ✅ Stage 3b / §4.2 (NIC bind 二重受信) — 2x→1x 実証完了
- wlan0 を **TEAM_SSID_OPEN (.4.214)** に切替え eth1 と同 subnet にして二重受信を再現:
  - **`--iface` 無し → 1.99x** (rx_dl total 2409 / distinct cycle 1209): eth1(有線 broadcast flood) + wlan0(air) の二重受信を確認
  - **`--iface eth1` + SO_BINDTODEVICE(sudo) → 1.00x** (total=distinct=1210): 二重受信を完全解消、受信は壊れない
- → **NIC bind が二重受信の root-cause fix** と実証。LRU dedup は不要 (任意 fallback のまま)
- 試験後 **wlan0 は管理用 TEAM_SSID (.1.141) に復元済**
- 補足: `wlan0 scan` に ch112 DFS の SSID は出ないが、`nmcli dev wifi rescan` 後または SSID 直接指定の `connect` で association 可

### ✅ Stage 3a (thread watchdog) — 実機 smoke 済
- `WatchdogThread`: 収集 thread の進捗カウンタ (metrics/uplink/sniffer.n_records/pps_gpio) を 2s 周期で監視し、**一度稼働後に無音化したら warning**。観測専用 (hot path 不可侵)、`--no-watchdog` で無効化
- RasPi 12s smoke: 誤検知 ⚠ 無し、final count 全進捗 (metrics 132/uplink 120/sniffer 1487/pps 12)

### ✅ Stage 5 (live PPS bridge 解析) + Stage 6 (GUI SQLite 化) — ロジック検証済
- `datasource.py`: SQLite live store 直読み (`compute_live_sqlite`/`live_summary_sqlite`)、`_pps_bridge_offset` で pps_gpio↔pps_uart から bridge offset 推定 → **TSF→unix 換算の真の DL OWD** と **cycle_count 損失**を live 算出
- `server.py`: SSE live を SQLite 版に切替 + `/api/live_summary` 追加
- ローカル検証: cycle 欠落 (missing=1) 検出、live 読み/traffic OK (pps 無し時は raw OWD に fallback)
- **未確認**: ブラウザでの実描画 (server/datasource のロジックは検証済だが UI レンダリングは未テスト)。`app.py` (Streamlit) は browser 確認まで残置 (削除しない)

### ⏸ 残 (任意・supervised)
- ADALM2000 PPS Δt の混雑下並走 (reflector GPIO PPS 出力有無の確認)
- FastAPI GUI のブラウザ実機確認 → OK なら Streamlit `app.py` 撤去
- reflector robotId を 0 に戻す (or 運用を robot-id 1 に統一)
- daemon の `--iface eth1` + `SO_BINDTODEVICE` 実装済。ただし **現状 wlan0 は 192.168.1.141 (別 subnet) のため二重受信は発生していない**。§4.2 検証は wlan0 を TEAM_SSID_OPEN(.4.x) に載せ替え + daemon を sudo 起動 (SO_BINDTODEVICE は root 必須) して再現する。これも監督下推奨。

### deploy 済の RasPi 状態
- `~/gtnlv/store.py` (新)、`~/gtnlv/gtnlv_rpid.py` (Stage1 版) を配置済・compile 確認済。`/dev/shm/gtnlv_live.db` に検証ランの残骸あり (tmpfs、再起動で消失)。

## 関連

- 設計: `docs/measurement_pipeline_v2.md` (§5.5 プロセスモデル、§5.6 sink トグル/制御、§8 移行)
- 負荷の前例: `docs/phase3_findings.md` §2.14 (3,400 fps drop=0)、§2.19 (混雑 72Mbps)
- PPS bridge: `docs/pps_sync_design.md`、落とし穴: `docs/lessons_learned.md` §C.26 (pps 番号)
