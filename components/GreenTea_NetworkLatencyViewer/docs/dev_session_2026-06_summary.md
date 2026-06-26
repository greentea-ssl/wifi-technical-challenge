# 開発セッション サマリ (2026-06-23〜25) + rx_dlb 実装計画

本番(192.168.4.x)移行〜rx_dlb 仕様策定までの作業ログと、次段(rx_dlb 実装)の指示用リファレンス。
context compaction を跨いで実装を継続するためのハンドオフ。

## 1. 現在の稼働状態 (重要)

| 要素 | 状態 |
|---|---|
| robots (XIAO C5 ×6) | **SanRei_HID dev 最新 (`1de891d`, rx_dl.rssi 入り broadcast)**、robot_id 1-6、**Open**、USB-JTAG で RasPi5 接続 |
| robot MAC↔id | r1=38:44:BE:A5:09:10 / r2=20:48 / r3=23:74 / r4=2C:0C / r5=32:A8 / r6=41:28 |
| sender | RasPi4 `gochiuma@192.168.4.217`(有線, ssh 可)/.218(wlan, power-save 遅), `gtnlv-sender` :8502, **6台60Hz**, mDNS robotN.local→.4.x |
| 計測 daemon | RasPi5 `~/gtnlv/gtnlv_rpid.py`(rssi 対応版, broadcast rx_dl), live `/dev/shm/gtnlv_live.db`, ctrl `/tmp/gtnlv.sock`, sniffer=46adfa(ttyUSB1 相当 by-id), wire eth0, pps0 |
| dashboard | RasPi5 :8501 (録画ボタン→NAS Parquet + PPS skew, sniffer 健全性チェック付) |
| sniffer | 1台目=46adfa(daemon, OUI), 2台目=38ca64(ttyUSB0, 通常 OUI に復帰済) |
| eth0/eth1 | eth0=SPAN(IP無 promisc, macb d8:3a:dd), eth1=計測LAN .4.213(USB-Eth Realtek) |
| chrony | RasPi5 = NTP master(allow .4.0/24 追加済, stratum3/internet), RasPi4 → 192.168.4.213 同期(host→SPAN +0.82ms) |
| NAS | `/mnt/nas`(//192.168.1.100/latencylog)tailscale subnet router 経由(accept-routes), R/W ~7.7MB/s |

## 2. リポジトリ/ブランチ状態

- **GreenTea** (`~/GreenTea_NetworkLatencyViewer`, branch `claude/repo-purpose-explanation-Ni0gb`): push 済。
  store.py(rx_dl.rssi), gtnlv_rpid.py(rssi parse + sniffer 健全性 + n_records==0 watchdog 修正),
  dashboard(録画ボタン/PPSトグル/robot別グラフ), owd_analyzer(leg_span/longagg/build_bridge_windowed),
  docs(phase3 §2.23 起動時間, lessons C.28 A-MPDU/C.29 上り自己干渉)。
- **SanRei_HID** (`/home/gochiuma/SanRei_HID`, branch `dev`, push 済 `1de891d`): rx_dl.rssi 実装。
  ※ 上り影響実験用フラグ(RX_DL_VIA_USB)は **commit せず**(robots は通常 broadcast に復帰済)。
  build/=通常 dev, build_exp/=USB実験(参考)。FQBN `esp32:esp32:XIAO_ESP32C5`, ビルド `~/bin/arduino-cli`。
- **robot_comm_spec** (`/home/gochiuma/robot_comm_spec`, branch `v2.1.0-dev`): rx_dlb spec commit 済
  **`608bc77`**(**未 push**)。submodule(SanRei_HID/robot_comm_spec)は別途 bump 要。

## 3. 本セッションの主な成果

1. **本番移行復旧**: eth1 DHCP 再取得(.4.213)、NAS tailscale 経由復旧、chrony 再同期(RasPi4→RasPi5 .4.213)。
2. **計測トラフィック復旧**: robots .4.x 再 join、sender 再投入(mDNS 再解決)。
3. **WebUI 強化**: 録画ボタン(NAS Parquet+PPS skew, 押下毎 新 run dir)、PPS skew トグル、robot 個体別 OWD グラフ、録画開始時 sniffer 健全性チェック+自動 RTS 復旧。
4. **sniffer 復旧の根治**: auto-recover の `n_records==0` ガード撤廃(起動時 hang も復旧)。esptool reset は逆効果(bootloader)、**pure RTS パルスが正**と判明。
5. **区間分解の確立** (`leg_span.py`): host→SPAN(参考, 機間クロック依存)/ SPAN→Air(AP滞留)/ Air→HID(無線)。多台数は捕捉バイアスのため **AP滞留 = SPAN→HID − Air→HID**(導出)。
6. **A-MPDU 解明** (lessons §C.28): 多台数の下り sniffer 捕捉欠落 = AP per-STA A-MPDU 集約(promiscuous C5 が他STA宛 A-MPDU 非復調)。**送信順反転で捕捉対象が r1↔r6 反転**して因果実証。台数勾配(2台88%→6台16%)。1台では混雑無くA-MPDU不発で~100%。
7. **窓分割 PPS bridge** (`build_bridge_windowed`): 2h/6h の単一fit残差(604/144μs)を窓分割で **9-12μs** に(phase3 最良域と整合)。
8. **6h 提出 run**(6台60Hz, `submit_6r_060hz_6h`): 下り SPAN→HID med **3.05ms**(Air→HID 0.67/AP滞留 2.38)、上り HID→air **0.99ms**、6h 損失 **0.14%**、精度 **11.7μs**、host→SPAN +0.82ms。
9. **起動時間** (§2.23): dev 最新 median **3.29s**/max 3.70s(全試行クリーン、旧 dev_v2.0.8 の ~6s リトライ消失)。
10. **rx_dl.rssi 実装**(spec v2.1.0): SanRei_HID dev + GreenTea store/daemon、全6台 -21〜-30dBm 蓄積確認。
11. **上り自己干渉の定量** (lessons §C.29): rx_dl を off-air(USB)化して上りエアタイム除去 → **下り容量 ~200Hz→~800Hz(4倍)、下り OWD p99 76→7.6ms(100Hz)/142→7.4ms(200Hz)(~10-19倍)**。観測トラフィックが観測対象を悪化させていた。
12. **rx_dlb spec 策定**(comm_spec v2.1.0-dev `608bc77`、下記)。

## 4. rx_dlb spec (実装対象、comm_spec radio_metrics.md §3.1.1, type `0x04`)

```json
{"meta":"524D04<rid><hid_seq BE>","type":"rx_dlb","bseq":4521,"rxc":271044,
 "tx":138207010679,"base":138207000000,
 "recs":[[13222708,9826,-24],[13222709,26596,-24]]}
```
- `recs[i]` = `[cycle_count(uint32), t_rx_tsf−base(int32 µs), rssi(int8)]` (固定順、compact)
- `meta` type byte `0x04`、`hid_seq`=batch seq。`bseq`=batch連番、`rxc`=累積rx数、`tx`=batch送信TSF(上りHID→airアンカー)、`base`=t_rx delta 基準TSF。
- **省略**: corr_unix_time(SPAN→HID は cycle_count join で完結)/dl_seq/t_*_esp/frame_size。
- **flush**: `recs≥K(≈50, UDP≤1400B)` or `0.5s` の早い方。フラグメント禁止。
- **損失分離**: batch(UDP)損失 ≠ 下り損失。`bseq` 連番抜けで batch 損失検出→下り損失計算から除外、`rxc` で冗長確認。
- **計測影響**: 下りは per-record 完全保持(t_rx は受信時刻で batch 遅延非影響)。上り HID→air は batch 単位(~2Hz/台)に間引き。
- **互換**: type `0x01` per-frame rx_dl はビルドフラグで残す(A/B・段階移行)。
- 効果見込み: 上り frame 360/s→~12/s、airtime ~14%→~1.3%(≒上り除去実験の効果回収)。

## 5. rx_dlb 実装チェックリスト (次段)

### A. SanRei_HID firmware (`src/ESP32C5Controller/metrics_radio.cpp`)
- [ ] ビルドフラグ `RX_DL_BATCH`(false=従来 0x01 per-frame, true=0x04 batch)
- [ ] record_rx で ring に (cycle_count, t_rx_tsf 用の t_local, rssi) 蓄積(既存 Entry 流用)
- [ ] metrics_task: flush 条件(件数≥K or 経過≥0.5s)で batch JSON 生成
      - meta(type 0x04, hid_seq=batch seq)、bseq++、rxc(record 毎 +1)、tx=送信直前TSF、base=batch先頭 t_rx_tsf
      - recs[] = 各 record [cycle, (t_rx_tsf−base), rssi]、UDP payload ≤1400B で打ち切り(残りは次 batch)
- [ ] fmt_meta を type 引数 0x04 で使用(既存)
- ビルド: `~/bin/arduino-cli compile --fqbn esp32:esp32:XIAO_ESP32C5 --libraries ./libraries --output-dir src/ESP32C5Controller/build src/ESP32C5Controller`
- 書込: 全6台 by-id へ `arduino-cli upload --input-dir build`(書込後 set_ssid open 要)

### B. daemon (`tools/rpi_daemon/gtnlv_rpid.py`)
- [ ] 52000 socket 受信で type=`rx_dlb` を分岐 → recs[] 展開、各 record を従来 rx_dl 行に(t_rx_tsf=base+delta、rssi、cycle_count)
- [ ] bseq を保持し連番抜け(batch 損失)を検出 → 損失解析用に記録(下り損失と分離)

### C. store.py (`tools/rpi_daemon/store.py`)
- [ ] rx_dl schema に `batch_seq` 追加(展開後の行に付与、_pa_type は既存で int64)

### D. sniffer (`tools/esp_firmware/sniffer/sniffer.ino`)
- [ ] type `0x04` を rx_dlb と認識(現状 meta offset 9 parse は不変、batch frame の hid_seq で air↔socket join、上り air 観測は batch 単位)。※下り相関は daemn 側 recs 展開なので sniffer 変更は最小

### 検証
- rx_dlb で 6台60/100Hz: 下り損失(bseq 分離)・下り OWD(SPAN→HID)・上り(batch tx)・airtime を broadcast 旧版と A/B 比較。下り容量/遅延裾の改善を確認。

## 5b. rx_dlb 実装完了 (2026-06-25)

§5 チェックリスト A-D を実装・全6台デプロイ・end-to-end 検証済。

- **A. firmware** (`SanRei_HID metrics_radio.cpp/.h`): ビルドフラグ `RX_DL_BATCH`(既定1=batch,
  `-DRX_DL_BATCH=0` で per-frame 0x01)。batch 蓄積 `s_batch[K=50]`、flush=件数≥50 or 経過≥0.5s、
  base/delta 圧縮、tx=送信直前TSF、bseq/rxc。UDP ≤1400B 厳守(超過は次 batch 繰越)。Flash 37%。
  全6台 (XIAO C5 by-id) 書込済、Open 再 join 確認 (rssi -19〜-31dBm)。
- **B. daemon** (`gtnlv_rpid.py`): `_handle_rx_dlb` で recs[] を従来 rx_dl 行に展開
  (t_rx_tsf=base+delta, rssi, cycle_count, t_hid_tx_tsf_us=tx, batch_seq=bseq, batch_rxc=rxc)。
  bseq 連番抜けで batch 損失検出 (n_batch_lost)、下り損失と分離。
- **C. store.py**: rx_dl schema に `batch_seq`/`batch_rxc` 追加 (per-frame 行は null)。
- **D. sniffer**: meta は type byte 非依存 parse のため **0x04 透過** (無改修、コメントのみ追記)。

**実測検証** (live DB、firmware 生フレーム照合):
- 生 rx_dlb: recs 30-31件/batch、payload ~785B、bseq 連続(損失0)、base/delta から t_rx_tsf 単調復元 ✓
- daemon 展開: 全行 batch_seq 付与、records/batch=30-32 (firmware と一致)、batch_lost=0 ✓
- **A/B 下り OWD** (SPAN→HID、6台同時負荷):

  | 下り負荷 | per-frame rx_dl (§C.29) p99 | **batch rx_dlb** p99 (med) | USB(上り完全除去) p99 |
  |---|---|---|---|
  | 100Hz | 76 ms | **17.7 ms** (2.61) | 7.6 ms |
  | 200Hz | 142 ms(崩壊) | **18.3 ms** (2.77) | 7.4 ms |

  上り frame 360/s→~12/s (30×削減) で下り p99 が per-frame 比 **4.3×/7.7×改善**、200Hz でも崩壊せず。
  §C.29 の自己干渉予測を回収。
- **持続 run** (`batch_6r_060hz_10min`、6台60Hz、10分、NAS、窓120s bridge):

  | 指標 | batch 10分 | per-frame 6h (§2.20) |
  |---|---|---|
  | 下り SPAN→HID med / p95 / p99 / max | **2.75 / 7.14 / 13.64 / 37.7 ms** | 3.05 / — / 46.7 / 466 ms |
  | 通算損失 (全6台) | **0.0 %** | 0.10–0.14 % |
  | 窓分割 bridge 精度 (sd) | **2.7 μs** | 11.7 μs |

  同一 60Hz で **p99 46.7→13.6 ms (3.4×)**、損失 **0.10–0.14%→0.0%**。詳細・制約は phase3 §2.24。
  ※ 10分は 6h の稀な DFS/queue freeze (~65回/6h) を含まず裾の直接比較は不公平、median/p95/p99 が頑健。
  ※ 区間分解 (Air→HID/上り) は多台数 sniffer air 捕捉制約 (§C.28) で本 run では join 不成立。

**運用上の発見 (再発防止)**:
- **daemon 起動は `--sniffer-port` にリテラルパスを渡す**。`SNIF="..." sudo python3 ... "$SNIF"` の
  prefix 代入は同一コマンド内 `$SNIF` 展開に効かず空文字 → `if args.sniffer_port:` が falsy で
  SnifferReader が生成されず sniffer 全停止 (ログ・fd・frame すべて無)。今回これで数回ハマった。
- **計測 socket は `--iface eth1` 固定が必須**。production (.4.x) で eth1(.213) と wlan0(.214) が
  同一サブネットのため broadcast が両 NIC に届き、0.0.0.0 bind だと rx_dl/rx_dlb を**二重受信**
  (records/batch が 2倍に化ける)。`--iface eth1` (SO_BINDTODEVICE) で単一受信。

## 6. 残タスク (TaskList)
- #6 提出データ D1-D7/S1(D2 6h 済、他項目)
- #7 AP A-MPDU 無効化での sniffer 捕捉率最終確証(AP 設定変更, 後日)
- rx_dlb: ✅ 実装・デプロイ・検証済 (§5b)。残: 長時間 run での損失/精度の batch 版再評価、
  本番 SanRei_HID への取り込み (現在は試験 6台が batch firmware)

## 7. 主要数値リファレンス(提出 7 項目)

| # | 項目 | 値 |
|---|---|---|
| 1 | One-way Latency 下り(SPAN→HID, 6台60Hz 6h) | med 3.05ms / p99 46.7 / max 466。内 Air→HID 0.67ms |
| 1 | One-way Latency 上り(HID→air) | med 0.99ms / p99 22.6 |
| 2 | Packet Loss(6h) | 0.10-0.14% |
| 3 | Data Rate | 60Hz×64B×6台 |
| 4 | Interference Detection | 公式は Yes/No のみ(RSSI 不要)。A-MPDU 輻輳/retry/近隣AP で「Yes」 |
| 5 | Startup Time | median 3.29s / max 3.70s |
| 計測精度 | 窓分割 PPS bridge | 11.7μs |
| (参考) 上り自己干渉 | rx_dl off-air で下り容量4倍/OWD p99 10倍 → **rx_dlb で回収済 (§5b、p99 4-8×改善)** |

> 注: 公式要件は **Round-Trip Latency** 指定だが、時刻同期(PPS bridge 11.7μs)を実装・精度確認済のため**双方向 OWD で提出**する方針。
