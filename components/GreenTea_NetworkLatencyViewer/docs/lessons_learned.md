# 不採用にした選択肢と試行錯誤の集約

本書は **「過去に検討したが採用しなかった選択肢」** と **「実装してみたが失敗・修正に至った事例」** を 1 ヶ所にまとめる。現行仕様の説明は別ドキュメント (`docs/architecture.md`, `docs/measurement_architecture.md`, `docs/phase3_findings.md` 等) を参照。

目的:
- 同じ選択肢を再提案しないよう判断根拠を残す
- 同じ罠を再度踏まないよう失敗 pattern と回避策を残す
- 提出文書の "rejected alternatives" 節執筆の参照点とする

---

## §A. 不採用にした選択肢 (architectural decisions)

### A.1 時刻同期に関するもの

| 選択肢 | 却下理由 |
|---|---|
| GPS-PPS | 屋内 (体育館等) で衛星が見えず受信不可 |
| JJY (LF 時報電波) | ~10 ms 精度不足、屋内受信が不安定 |
| SNTP/NTP-over-Wi-Fi を主同期源 | 混雑下で対称 RTT 仮定が破綻 (`docs/sync_alternatives.md` §1) |
| ESP-NOW を同期 / 返送に | 同 PHY 混雑問題 + 既存テレメトリ経路で十分 |
| FTM (802.11mc) | ESP32-C5 対応未確定 |
| 天井 IR ビーコン / 上面 IR | 会場・外装制約 (天井設置不可、ロボット外装変更不可) |
| AIPC↔RasPi PTP | 計測コードを AIPC に置かない設計のため不要 |
| AIPC NIC の hwtstamp 確保 | 同上 |
| AIPC の USB hwtstamp NIC (AQC111U 等) | AQC111U は PTP 非対応、調達意義なし |

### A.2 ハードウェア / sniffer 選定

| 選択肢 | 却下理由 |
|---|---|
| **Intel AX200/AX210 を主軸 sniffer** | radiotap TSFT 壊れ + HE PPDU monitor 非対応 (実機検証済、§B.1)。**ただし補助 monitor (beacon/制御フレーム + TSFT、broadcast)** としては有用なので併用 |
| MT7921 USB sniffer | ESP32-C5 で代替可、追加調達不要 |
| sniffer 2 台 (1 台 TSF 同期、1 台 promiscuous) | 1 台で TSF 取得と PROMIS が両立可能 (esp_timer↔TSF 中点フィット)、追加機材不要 |
| reflector を本物の HID 模擬とする | reflector は試験用、本番 HID (`SanRei_HID`) のジッタ特性は別途特性化 |

### A.3 計測アーキテクチャ

| 選択肢 | 却下理由 |
|---|---|
| **AP-internal `gtnlv-apd`** (AP 内 SSH で計測 daemon を起動) | 競技会場提供 AP は SSH 不可、AP 非依存設計に再構成 |
| **40999 (EMS) を計測対象に含める** | AI 系と独立した安全系起源、計測対象外 |

---

## §B. 実機で検証して採用しなかった選択肢 (proven not usable)

### B.1 AX210 monitor mode は HE PPDU + TSF 主軸不可

dev ブランチ (`origin/dev`、最終コミット `da6ebcf` 2026-04-19) と Phase 3 実機検証より:

| 検証項目 | 結果 |
|---|---|
| Wi-Fi カード | Intel AX210 PCIe、Raspberry Pi 5 上 `wlP1p1s0` |
| ドライバ | iwlwifi |
| **radiotap TSFT (dev branch HE 環境)** | 全フレームで同一値 `0x00000820a0000820` (壊れ) |
| `iw dev get tsf` | 未対応 |
| 802.11ax (HE) monitor mode | **非対応** (HE Iftypes は managed/P2P-client のみ) |
| チャネル幅 | 20MHz no-HT 固定。AP の 80MHz に追従不可 |
| 非 HE フレーム受信 | **成功** (260 fps、Phase 3 ch112 11a/legacy で再確認) |
| radiotap TSFT (legacy frame) | **取得可能** (`7191986 us tsft` 等) |

**用途の変遷**: HE PPDU 主軸の取得は ESP32-C5 sniffer に任せ、AX210 は §2.7 air-wire diff 計測 (DL/UL × bc/uc) の補助 monitor として使用。

**廃止 (Phase 3 後)**: §2.13 で **現 sniffer firmware は他 STA 宛て unicast も air RX できる**ことが判明 (reflector 宛 60k frame 観測、§C.2 update)。これにより AX210 の補助 monitor 役は不要化。M.2 B-key スロットは **EC25J (Quectel EC25-J、LTE backhaul)** に置換予定 — 会場 LAN にインターネットが無い時の remote access / データ吸い上げ用 (GNSS / 時刻同期には不使用、RasPi NTP master 維持)。AX210 で取得した air-wire diff 数値は `docs/phase3_findings.md` §2.7 に記録済。

### B.2 AX210 + `SO_TIMESTAMP` パターン (採用しなかったが知見として残す)

dev branch で実装済みの代替活用:
- `SO_TIMESTAMP` (sockopt=29) で μs 精度 Unix 時刻取得
  - Python 3.13 で `socket.SO_TIMESTAMP` 定数未定義 → 数値 `29` 直指定で動作
  - `setcap cap_net_raw+ep /usr/bin/python3` (実体パス指定) が必要
- AP 内部滞留を Unix domain で計算 (`AX210_DL_time - send_time`、較正不要)
- monitor mode で 802.11a/g/n データフレームをキャプチャ可能 (HE は不可)

現行アーキテクチャでは PHC + sniffer C5 で同等以上のことが可能なため不採用。

### B.3 dev branch ESP32 sniffer の旧スループット制約 (新設計で解消)

dev branch の `esp32-sniffer` (ASCII CSV、921600bps) で観測されたボトルネック:

| 指標 | dev branch 値 |
|---|---|
| シリアル出力レート上限 | ~1000 pps (921600bps 帯域律速) |
| Sniffer フィルタ通過率 | Total 14108 → Reported 7000 / **Skipped 7108 (約 50%)** |
| ストレステスト達成 | 500Hz × 5 秒で loss 0.1% (AX210 併用構成時) |

現行 sniffer は **2 Mbps + バイナリプロトコル** で帯域 2-3 倍化、Phase 3 overnight で **cb_total 7.4M / captured 4.77M, dropped=0** を実証。

---

## §C. 実装の試行錯誤 (in-project failures and fixes)

### C.28 多台数下りの sniffer 捕捉欠落は AP の per-STA A-MPDU 集約 (送信順=キュー位置で決まる)

**症状**: 本番 LN6001 (WiFi 6/11ax) + Open SSID、ロボット 6 台 @ 100Hz で、sniffer の
**下り (AP→robot unicast) 捕捉率が robot 間で極端に非対称** (例: r1 ~50-60% / r3-6 ~0-2%、
distinct(robot,cycle) 比)。上り (robot→AP) は全台均一に ~100% 捕捉できるのに下りだけ。

**誤った仮説と否定** (順に実験で潰した):
- **ビームフォーミングで方向 null** → ❌ ロボットは co-located + sniffer は AP 至近で電界は
  方向に依らず十分。さらに下り捕捉できた frame の RSSI は -17dBm と強い。
- **sniffer の単一無線ランダム競合** → ❌ 2台目 sniffer を co-located で足しても union がほぼ
  伸びず (16.7%→18.2%)、両機が同じ frame を取り逃す = 系統的。
- **callback/dst フィルタ負荷** → ❌ 2台目を r4-6 専任 (DSTFILTER_MAC) にしても r4-6 は
  0-2% のまま。dst を絞っても radio が復調しないものは増えない (cb_total に来ない、dropped=0)。
- **A-MSDU で複数 IP を 1 frame に纏めて誤カウント** → ❌ 捕捉できた下りは全て単一パケット
  (sig_len 中央値 130、366+ は 0.003%)。

**真因 = AP の per-STA A-MPDU 集約**:
- A-MPDU は AP の送信キューに複数 frame が溜まった時に発動する。**サイクル内で最初に AP へ
  届いた robot 宛 frame は、次サイクル分が積む前に即送信される (単一 MPDU) → promiscuous の
  C5 が復調でき sniffable**。後続 robot はキューに溜まり A-MPDU 化 → **他 STA 宛 A-MPDU を
  C5 promiscuous が傍受復調できず、frame ごと欠落** (cb に来ない)。
- **送信順反転実験で因果を実証**: sender (`sender_webui`、dict 挿入順=送信順) を 1→6 で送ると
  r1 が 58% / 6→1 で送ると **r6 が 53% に跳ね r1 は 2% に崩壊** → 捕捉対象が送信順そのままに反転。
  robot の個体・位置・電界・id とは無関係、**送信順=AP キュー位置**で決まる。
- **台数勾配**で混雑依存も実証: 平均下り捕捉率 2台 88.8% → 3台 70.4% → **4台 33.6%** → 6台 16.4%
  (本環境の閾値は 3→4 台)。**1台では混雑が無くキューが溜まらないので集約されず ~100% 捕捉**
  (1台 1000Hz では単機でもキューが溜まり 85.8% に低下=混雑依存の傍証)。
- これは **AP 滞留 (SPAN→Air) 遅延と同一現象の裏表**: 混雑→キュー積み上がり→① 滞留遅延↑ ②
  A-MPDU 化で sniffer 捕捉↓。高滞留 frame ほど取り逃す (air-leg の捕捉バイアスの正体)。

**ハードウェア限界**: **ESP32-C5 の `esp_wifi_rxctrl_t` (HE版) に aggregation/single_mpdu フラグが無い**
ため、sniffer 自身では「取り逃した frame が A-MPDU か」を直接報告できない (`cur_bb_format`/`rate`/
`he_siga1` のみ)。確証には AP 側で A-MPDU 無効化 / legacy rate 強制 (全 robot が同時 sniffable に
なるはず)、または monitor mode アダプタ (radiotap で A-MPDU/MCS 取得) が要る。

**運用上の結論**: air 区間分解 (`Air→HID` 等) を全 robot で取りたいなら **台数を絞る** (単機 ~100%、
混雑無しが条件)。多台数では下り air-leg は構造的に欠落するので、**下り OWD は sniffer 非依存の
HID rx_dl TSF + SPAN wire (`SPAN→HID`) で測る** (本プロジェクトの既定設計どおり、提出数字に影響なし)。
`Air→HID` の無線リンク値 (~0.68ms) は捕捉できた robot 偏重サブセットだが、電波伝搬+HID処理は物理的に
robot 非依存なので代表性はある。

### C.29 上り計測トラフィック (rx_dl broadcast) が下りの遅延・容量を律速する (観測の自己干渉)

**動機**: rx_dl は 60-100Hz×6台で **WiFi broadcast**(52000+id)。broadcast は basic rate(典型6Mbps)
固定・無集約なのでエアタイムを著しく食う(§C.28 関連)。これが下りをどれだけ律速しているか検証。

**手法**: SanRei_HID metrics_radio に実験フラグを追加し、rx_dl を **WiFi broadcast → USB Serial 出力に
切替(off-air、上りエアタイム除去)**。下り送信レートを ramp し、下り容量 (損失/受信率) と下り OWD
(SPAN→HID、USB rx_dl × wire を cycle_count join + PPS bridge) を「上り有/無」で比較。

**結果① 容量** (6台):
| 下りレート | 上り有 (WiFi-rx_dl) | 上り無 (USB-rx_dl) |
|---|---|---|
| 100Hz | loss 0.07% | ~2-3% |
| 200Hz | **崖: loss 70%** | 4.5% (受信 191/s) |
| 800Hz | — | 5.4% (受信 757/s = 送信追従) |
→ **上り除去で下り容量の崖が ~200Hz → ~800Hz (約4倍)**。

**結果② 遅延** (下り OWD SPAN→HID):
| 下りレート | 上り有 | 上り無 |
|---|---|---|
| 100Hz | med 3.31 / mean 14.2 / p99 **76** / max 87 ms | med 2.55 / mean 2.52 / p99 **7.6** / max 14.6 ms |
| 200Hz | med 5.98 / mean 21.6 / p99 **142** / max 247 ms | med 2.40 / mean 2.38 / p99 **7.4** / max 13.7 ms |
→ 中央値は +0.8〜3.6ms だが **裾が壊滅的: p99 が 10〜19倍、mean 5〜9倍**。**上り無なら下り OWD は
~2.5ms / p99 ~7.5ms とレート非依存で安定**(AP 下りキューが積み上がらない)。

**機構**: 上り rx_dl broadcast がエアタイムを奪い **AP の下りキューをバースト的に滞留**させる →
下り OWD の重い裾。つまり **観測トラフィック(rx_dl)が観測対象(下り遅延)を悪化させる自己干渉**。
WiFi-rx_dl で測った下り OWD(med 3.3ms / p99 76ms)は計測負荷込みの値で、**production 実力(計測上り
無し)は ~2.5ms / p99 ~7.5ms** に近い。

**注意**: USB-rx_dl の損失 ~5% フロアは host 側で 6 ttyACM を Python 同時読みする取りこぼしを含む
(容量結論は受信率の送信追従で頑健、遅延は per-frame なので median/p99 とも頑健)。sniffer 下り捕捉は
上り除去でも改善せず(§C.28、A-MPDU は下りキュー由来で上りと独立)。

**含意**: rx_dl の **off-air 化(USB)or batching(≤1MTU + batch_seq)** で上り負荷を断てば、下り容量
~4倍・下り遅延裾 ~10倍改善。提出の下り One-way Latency / Data Rate は計測上り無しの値が production に近い。

**対策実装・実証済 (2026-06-25、§2.24)**: batching を `rx_dlb` (radio_metrics.md §3.1.1、type 0x04、
compact-JSON で複数 rx_dl を 1 UDP に、flush=50件 or 0.5s、bseq で batch 損失分離) として実装し
全6台へ展開。上り frame 360/s→~12/s (30×)。A/B で下り p99 が per-frame 比 4.3×(100Hz)/7.7×(200Hz)
改善、200Hz 崩壊解消。10分持続 run (60Hz) でも p99 46.7→13.6ms (3.4×)・損失 0.10–0.14%→0.0%。
broadcast を維持しつつ自己干渉の大半を回収 (off-air USB の p99 ~7.5ms には僅かに及ばず)。
firmware ビルドフラグ `RX_DL_BATCH` (既定1=batch / 0=従来 per-frame、A/B 用)。

### C.1 ESP32-C5 PROMIS が UDP RX path を starve させる

**症状**: cal test を sniffer 自身を target にして実施 (PROMIS ON 状態) → unicast の 64% がロス + 残った 35% も平均 6.8 秒遅延。

**原因**: PROMIS モードで cb 起動が多発 (~2670/s) すると、チップ内部 RX バッファが promiscuous-delivered フレームで埋まり、自分宛て unicast の通常 RX path が starve する。

**現行運用**:
- sniffer は **PROMIS 専任** (UDP listen を併設しない)
- cal 試験時は **`ENABLE_PROMISCUOUS = false`** に再 flash、終了後 true に戻す
- HID は PROMIS 非使用なので影響なし

### C.2 ESP32-C5 PROMIS と他 STA 宛て unicast (Phase 3 で **取れることを確認**、§C.2-update)

**Phase 0 R12 観測時の症状**: pc_emulator 6 kHz 送信 to XIAO reflector、devkit sniffer (PROMIS ON、BSSID マッチフィルタ通過) で **cb_total ~22/s 頭打ち** = chip がほとんど cb を起動していない。broadcast 系のみ 1500+ fps 取得可。

**Phase 3 (§2.12 envsim 試験) で再観測**: AIPC 1000 Hz unicast を reflector 宛て送出中、現在の sniffer firmware で **reflector 宛 unicast 60,076 件を air RX 確認** (= 1000 Hz × 60s の wire 件数 60,001 にほぼ一致)。

**差異の原因 (推定)**:
- Phase 0 R12 は XIAO C5 firmware かつ promiscuous filter 設定が現在と異なる可能性
- 現在の sniffer.ino (devkit C5) は `WIFI_PROMIS_FILTER_MASK_DATA` を立てており、自分宛て / broadcast / 他 STA 宛 unicast の data frame をすべて cb に渡す挙動

**現状の運用効果**:
- DL unicast (他 STA 宛て) の AP queue 滞留を sniffer 経由で per-packet 観測可
- **AX210 monitor 補助は不要に近づいた** (`docs/measurement_architecture.md` §5.3 補強案も実質不要、cal_sender の uc 経路は queue 競合観察用に使える)
- AX210 残しても air-wire diff の補完監視として有用、外しても主要計測は欠落しない

### C.3 XIAO C5 native USB JTAG/Serial が反復試験で固まる

**症状**: XIAO ESP32-C5 (native USB CDC) で反復 flash + Serial デバッグを行うと HWCDC が応答しなくなる。物理抜き挿し必要。

**現行運用**:
- 開発ベンチでは **CP2102N devkit C5 を 2 台** 使用 (sniffer + reflector)
- 本番 HID は XIAO C5 だが **USB 不接続** で動作するため発生しない
- 反復試験用途では XIAO 不採用

### C.4 ESP32-C5 board variant FQBN の混同で誤動作

XIAO C5 用 FQBN (`esp32:esp32:XIAO_ESP32C5`) を devkit に焼くとピン配置違いで誤動作する。**現行の FQBN 割り当て**:
- XIAO C5: `esp32:esp32:XIAO_ESP32C5`
- devkit C5: `esp32:esp32:esp32c5`

### C.5 `wifi_pkt_rx_ctrl_t.timestamp` 重複値バグ

ESP-IDF の既知 issue ([esp-idf#2468](https://github.com/espressif/esp-idf/issues/2468))。実機で 0〜2.6% の重複を再現。**現行対応**:
- ホスト側で `(rx_timestamp, src_mac, hdr_seq)` で dedup

### C.6 `esp_wifi_get_tsf_time()` の ISR 内呼び出し禁止

実機で中央値 290 μs / 最大 120 μs+ かかる。**現行対応**:
- 通常タスクで呼び出し
- 中点フィット (`(t_before + t_after) / 2`) を使う (`t_before` 単独だと p99 が 3.5 倍悪化)

### C.7 port mirror で WiFi STA 宛て UDP unicast が host port では落ちる

**症状**: GS308E (NETGEAR) + Xikestor SKS3200M-8GPY1XF の両方で、**mirror source = host PC port** にすると TCP unicast / UDP broadcast / cross-subnet UDP は mirror dst に届くが、**WiFi STA 宛て UDP unicast / ICMP echo は完全に drop** される。

**原因**: 多くの managed switch firmware の挙動で、STA 宛て unicast を通常 forwarding fabric 経由で AP port に送り mirror engine をスキップする path がある (推定)。

**現行対応**:
- **mirror source を AP port にする** — UDP unicast も含めて全 traffic が mirror される
- これで PHC wire bridge 計画が動作可能になった

### C.8 sniffer `ENABLE_PROMISCUOUS` フラグの二面性で一晩無効

**症状**: cal_sender 試験用に `ENABLE_PROMISCUOUS=false` にした状態を本走に持ち越して overnight 走行、**cb_total=0 のまま一晩** 環境スキャンが取れず。

**現行運用**:
- sketch 冒頭コメントで明示
- cal 試験前後で必ず flag を確認、本走時は **true** に戻す

### C.9 broadcast 二重受信 (RasPi 内 2 iface)

**症状**: RasPi が wlan0 + USB-Eth の両 iface で 52001 JSON を受信、N が 2x に膨張。

**現行対応**:
- wlan0 を上流 192.168.1.x (管理用) に分離、計測 LAN (192.168.4.x) と異なる subnet
- broadcast は subnet を跨がないので USB-Eth にのみ届く
- analyze.py に保険として `(hid_seq, aipc_seq)` ベース dedup を入れている

### C.10 analyze.py の floor 計算が TSF discontinuity に脆弱

**症状**: TSF↔unix のグローバル `min()` を floor とする設計で、6h ラン中の 1 回の TSF jump (re-associate 等) によって全 sample の relative 値が 100ms+ シフト。

**現行対応**:
- **60s rolling-window min-filter** に置換
- raw OWD (`t_rpid_recv − corr_unix_time`) を primary metric として併用 (NTP-bound、TSF 非依存)

### C.11 reflector C5 の一時固着

**症状**: USB-UART (CP2102N) は enumerate するが ESP32 が bootloader sync に応答しない状態が稀に発生。esptool `--connect-attempts 5` でも復帰せず。

**現行対応**:
- 別ホストの USB ポートに挿し替え + クリーン電源サイクルで復帰
- BOOT ボタン + EN ボタンで強制 download mode 投入も有効
- **本番 XIAO C5 は USB 非接続なので発生しない想定**

### C.12 metrics_radio の subnet hardcode

**症状**: `metrics_init(robot_id, subnet_third)` で `SUBNET_THIRD=1` を hardcode していたため、subnet 移行時 broadcast が届かなくなる。

**現行対応**:
- `metrics_init(robot_id, 0)` で **`WiFi.localIP()` から自動算出**
- subnet 移行に追随、競技会場の任意 subnet で動作

### C.13 host PC ↔ RasPi の CLOCK_REALTIME に NTP drift ~2 ms (Phase 3 後段で改善済)

両 host とも canonical NTS 経由で間接同期していたため、Phase 3 overnight 計測中の host PC が RasPi より **+1.94 ms 進んでいた** (実測)。

**Phase 3 後段の対応** (実施済):
- RasPi を **NTP master 化** (`local stratum 10` + `allow 192.168.4.0/24`)、AIPC が RasPi に直接同期
- 結果: AIPC↔RasPi 一致度 ~1.94 ms → **~200 μs** (実測、wire 起点 join)
- 詳細は `docs/measurement_architecture.md` §8、`docs/phase3_findings.md` §2.10

**追加で得た教訓**: 開発環境で AIPC が canonical NTS (stratum 2) を best として選び、RasPi (stratum 3) を `^?` (selectable 評価で負け) のままにする現象を観測 → `prefer` flag だけでは弱く、`chronyc -a delete <canonical>` での動的削除が必要 (会場では canonical unreach のため問題は自然消滅)。

**残課題**: 更に < 10 μs を狙うなら PTP + hwtstamp 対応 NIC への置換 (現 USB-Eth AX88179 は hwtstamp 非対応)。大会後候補 (§E)。

### C.14 AF_PACKET で SPAN mirror フレームを受けるには promiscuous mode 必須

**症状**: `wire_capture.py` (AF_PACKET + SO_TIMESTAMPING) で eth0 SPAN dst から packet を受けようとしたが 0 matched。tcpdump は libpcap が自動で promiscuous 化していたため見えていた。

**現行対応**:
- 計測前に `sudo ip link set eth0 promisc on` 明示
- 起動スクリプトに組み込み

### C.15 sniffer の `wifi_pkt_rx_ctrl_t.timestamp` は **TSF ではない**

sniffer.ino の binary record に含む `rx_timestamp_us` は `wifi_pkt_rx_ctrl_t.timestamp` を直接出力していたが、これは **chip の local clock (esp_timer 系) であり AP TSF ではない**。

そのため sniffer の (chip-local-ts, RasPi unix) ペアでブリッジを作ろうとすると、reflector の `t_hid_rx_tsf` (TSF 軸) と base が一致せず、**chip 間で数十秒のズレ** が出る (実機確認: 72.7 sec)。

**正解**: bridge は reflector の rx_dl JSON 内 `t_hid_rx_tsf_us` (TSF 軸) と RasPi が JSON を受信した `t_rpid_recv_unix` の組で構築する。`tools/owd_analyzer/sniffer_bridge.py` 参照。

**Phase 3 で実装済**: sniffer.ino の主タスクで `esp_wifi_get_tsf_time()` を 100 ms 周期で midpoint-fit して `g_calib_tsf_us / g_calib_esp_us` を保持、cb で `tsf_us = g_calib_tsf_us + (esp_timer_now − g_calib_esp_us)` を Entry に書く。これにより sniffer 自身を **dedicated bridge source** にできた (複数 reflector 環境で reflector を bridge にしない設計、`tools/owd_analyzer/sniffer_bridge.py`)。副作用は C.17 (RAM 圧迫) と C.18 (parser 二重持ち)。

### C.16 sniffer transport delay の主因は host 側 batch read だった (CP2102N 説は誤り、§2.16 で訂正)

**当初の誤った推定**: sniffer の transport delay median 50-500 ms を「CP2102N 内部 buffer + USB CDC polling が bottleneck」と推定。`Serial.flush()` per record を入れても median 不変だったため、CP2102N が犯人と考えていた。

**§2.16 で判明した真因**: `gtnlv-rpid` / `sniffer_runner` の **`ser.read(4096)` + `timeout=0.5`** が batch read を作っていた。
- 4096 byte 貯まらない限り 0.5s timeout 待ち → chunk 内 frame に read 完了時刻が付与 → transport delay が **0-500ms に均等分布** (max が常に ~502ms = timeout 0.5s と一致が決定的証拠)
- **`ser.read(ser.in_waiting or 1)` の即時 read に変更 → 200ms → 1-2ms (~117×改善)**

**併せて判明**: native USB CDC 化試験 (devkit の CP2102N port + native USB port 両接続、`CDCOnBoot=cdc`) で、**CP2102N の方が native USB より速い** (read 修正後 p50 1.37 vs 1.74 ms、p95 2.17 vs 7.89 ms)。ESP32 内蔵 HWCDC は TinyUSB stack の overhead があり、専用 UART bridge (CP2102N) が低 jitter。

**現行対応**:
- 両 sniffer reader (`gtnlv_rpid.py`, `sniffer_runner/run.py`) を `in_waiting` ベースの即時 read に修正済
- **native USB 化は不要・逆効果** (§E から候補削除)、CP2102N のまま
- `Serial.flush()` は引き続き入れる (ESP32 側 TX を即送出)
- bridge offset の floor 推定が transport jitter 200ms→数ms で遥かに安定化

### C.17 sniffer Entry 拡張で RAM 圧迫 → WiFi.begin() timeout

C.15 を受けて sniffer.ino の `Entry` に `uint64_t tsf_us` (8 byte) を追加 (Entry 34 → 42 byte、LEN_FRAME 36 → 44)。リング `RING_N = 4096` を維持したまま flash したところ、起動時 `WiFi.begin()` が **30 秒 timeout** して association できず、`# ERR: WiFi connect timeout` で cb_total=0。

**根本原因**: Entry × RING_N = 42 × 4096 ≈ **172 KB** static アロケーションで WiFi スタック初期化用 SRAM が枯渇 (ESP32-C5 は 320 KB SRAM)。

**修正**: `RING_N = 4096 → 2048` (容量 84 KB に半減)。100Hz cal + DL/UL air frame 流量で overflow なしを 5 min 試験で確認。

**教訓**:
- ESP32-C5 で大きな静的バッファを増やす時は WiFi スタック空き SRAM (`ESP.getFreeHeap()`) を起動直後に出力して確認すべき
- Entry 構造体に新フィールド追加する変更は副作用が大きい (binary format、host parser、RAM 全部に波及)

### C.27 SD カード I/O error は突然来る — A2 class + 定期 backup 推奨

RPi OS インストール後の運用中、突然 `ls`, `nano` 等の binary が `Input/output error`。
ssh も `Connection reset by peer` で入れなくなった。原因は **SD カード fs / SD 自体の
コラプション** (binary read 不可、kernel は alive で ping は通る)。

**症状**:
- shell builtin (`cd`, `echo`) は動く
- `/usr/bin/*` 系は I/O error
- ssh handshake は途中で reset (sshd binary も読めない)
- ping は通る (kernel 既存 buffer で動作)

**対処**:
- write 続けると悪化する → 直ちに power off (poweroff コマンドも動かなければ電源抜き)
- 新 SD に焼き直しが最速 (本リポジトリの設定は `raspi_setup.md` §1〜§5.5 + §8.5 + §10 で
  20-30 分で復旧可能、計測データは AIPC 側にもあれば無傷)

**予防**:
- A2 class + 信頼できるブランド (SanDisk Extreme, Samsung Pro Endurance) を使う
- 計測中 RasPi 側に大きな CSV を書かない (`tools/.../out/` を tmpfs 化 or NFS mount 検討)
- 重要設定は git に入れる (RasPi 側にしか無いファイルを作らない)

### C.26 RPi 5 上で `/dev/pps0` / `/dev/pps1` の割当は boot 順依存

RPi OS bookworm + `dtoverlay=pps-gpio,gpiopin=18` + eth0 PHC (macb) の組合せでは
**LinuxPPS の登録順 = boot 時の driver init 順** で `pps0` / `pps1` が決まる。
Ubuntu 25.10 時代の予想 (eth0 PHC が `pps0`、pps-gpio が `pps1`) と逆になることがある:

実機 (2026-05-28、kernel 6.12.75-rpt-rpi-2712):
```
/sys/class/pps/pps0/name → pps@12.-1   ← pps-gpio (BCM18)
/sys/class/pps/pps1/name → ptp0        ← eth0 PHC
```

`/dev/pps1` を ppstest した時に sniffer の event が来ず "Connection timed out" になり、
firmware/配線を疑って数十分溶かした事案あり (実際は pps1 = ptp0 で gpio source ではなかった)。

**実装ルール**:
- script は `/sys/class/pps/*/name` から `pps@12.-1` (BCM18 PPS) を探して
  `/dev/ppsN` を動的決定する (固定値で書かない)
- もしくは gtnlv-rpid に `--pps-device /dev/pps0` 等の明示 flag を出す
- 文書の例示は **「pps-gpio は /dev/pps0、ptp は /dev/pps1」を実機で確認した上で書く**

### C.25 ModemManager 1.24 (Debian trixie) は Quectel EC25 を allowlist 外で probe しない

Debian trixie / RPi OS bookworm の `modemmanager 1.24.0-1+deb13u1` で
Quectel EC25-J (USB VID `2c7c` PID `0125`) を挿しても `mmcli -L` で `No modems`。
原因: ModemManager の plugin allowlist (debug log で確認) に Quectel VID `2c7c` が
**登録されていない** (Qualcomm SoC ベース機種なのに Quectel plugin が VID 登録漏れ)。

USB device 認識自体は OK (`dmesg`: `option ttyUSB0-3` + `qmi_wwan wwan0` + `cdc-wdm0`
すべて enumerate)、ModemManager の filter が tty 系 device を modem 候補から外している
だけ。debug log:
```
[filter] tty devices default: forbidden
[filter] registered plugin allowlist vendor id: 2cb7, 0421, ...
   (※ 2c7c は無い)
```

**解決**: udev rule で `ID_MM_DEVICE_PROCESS=1` を全 SUBSYSTEM (tty/net/usbmisc/usb) で
EC25 USB ID に付与すると ModemManager が候補に入れ、Quectel plugin が claim する:
```
ACTION=="add|change", SUBSYSTEMS=="usb", ATTRS{idVendor}=="2c7c", ATTRS{idProduct}=="0125", ENV{ID_MM_DEVICE_PROCESS}="1"
SUBSYSTEM=="tty", ... 同条件 ...
SUBSYSTEM=="net", ... 同条件 ...
SUBSYSTEM=="usbmisc", ... 同条件 ...
```
詳細は `docs/raspi_setup.md` §8.5.1。

### C.24 EC25-J は au系 SIM (povo 等) に対し網側 IMEI 拒否 (cause 33)

EC25-J modem firmware EC25JFAR06A06M4G (carrier config `Commercial-KDDI` 選択済) で
povo SIM (KDDI MNC 44051) を装着 → アンテナ装着で signal 31/-53 dBm の強電界、
KDDI Band 18/41 セルを物理的に捕捉できるが **`AT+CEER` = `5, 33`** ("Requested service
option not subscribed") で attach reject。

切り分け:
- 同じ povo SIM をスマホ (Pixel) に挿すと正常接続 → **SIM 契約・データトッピングは問題なし**
- `AT+QCFG="ims",1` で IMS 有効化 → 変化なし
- `AT+COPS=1,2,"44051",7` で KDDI 強制 → `+CME ERROR: 30` (No network service)
- modem は KDDI MBN を select している (`AT+QMBNCFG="list"` で `1,1,1,"Commercial-KDDI"`)

**結論**: au/KDDI 網側で EC25-J の IMEI を data service に対して許可していない (au は
SIM フリー modem に対し動作確認端末リスト方式で IMEI 制限する慣行あり)。

**回避**: **docomo 系 SIM に切替** (実機で **irumo (docomo MNC 44010)、APN spmode.ne.jp**
で接続成功、ping RTT 64-75ms)。docomo 系 (IIJmio D-plan / OCN モバイル ONE / irumo /
docomo 直契約) は IMEI 制限が緩く EC25 系で広く動作確認されている。

### C.23 sniffer dst MAC フィルタで非計測トラフィック (スマホ等) を除外

cb の段5 (src=BSSID) は「AP 送信の全 data frame」を通すため、同 AP にスマホ等が
接続して大量下り通信 (動画/DL) すると **AP→スマホ frame も ring に入り**、cb 負荷源
+ dropped (計測 frame 取りこぼし) になる。§2.14 で sniffer 限界 ~5,000-8,000 fps、
スマホ高負荷 100 Mbps ≈ 8,000 fps で超過しうる。

**重要**: cb はハングしない (ring overflow は `dropped_total++` で安全に drop、§C.16
の read 系とは別) が、計測 frame の loss は増える。これを予防する dst フィルタを段4 として追加。

**設計** (`sniffer.ino`):
- `broadcast` (ff:ff:ff:ff:ff:ff): 計測 broadcast (cal/metrics) 用に常に通す
- `multicast` (group bit set、mDNS 等): **除外** (計測対象外)
- `unicast`: `DST_FILTER_MODE` で **OUI (上位24bit) / MAC (48bit)** 許可リストを切替
  - `TARGET_OUIS[][3]` (ベンダー単位)、`TARGET_MACS[][6]` (個体単位)
- `ENABLE_DST_FILTER=false` で旧挙動 (全 AP 下り通す) に戻せる

**注意点**:
- ESP32 は複数 OUI を持つ (devkit=`D0:CF:13`、XIAO C5 は別の可能性) → **本走 HID
  (XIAO C5) の MAC を確認して `TARGET_OUIS` or `TARGET_MACS` に追加が必須**
- 「ベンダー ID」= OUI = MAC 上位 **24bit** (48bit は MAC 全体 = 個体識別)
- §2.13 の「他 STA unicast も取れる」利点は計測対象 OUI/MAC に限定されるが、本走は
  計測対象 robot のみで十分
- 会場に他 ESP32 機器がある場合、OUI フィルタでは通してしまう → 厳密には MAC フィルタ
  (`DSTFILTER_MAC`) を使う

### C.22 ESP32-C5 single core でも FreeRTOS task 分離で tail latency を 30× 改善 (Phase 3 §2.15)

SanRei_HID firmware (本物 production HID) に `metrics_radio` を統合した試験で、初期実装 (PoC reflector とほぼ同じロジックを ino に追加) では:

- **100 Hz UDP rx でも 25% loss + median 69 ms** という致命的な性能
- 原因: `loop()` 末尾の **`delay(10)` が 10ms 強制スリープ** = 100 Hz UDP (10ms 間隔) と完全衝突
- 加えて `handleUdpPackets()` 内の `Serial.println` (per-packet 大量) と `server.handleClient()` (WebServer) で loop 周期が伸びていた

**段階的改善**:

| Step | 修正 | delivery | median | p95 |
|---|---|---:|---:|---:|
| 1 | `delay(10)` → `yield()`、Serial.print 削除 | 96% | -232 μs | 71 ms |
| 2 | UDP rx を 高 priority task (10) で polling、queue 経由 main 連携 | 100% | -524 μs | 142 ms |
| 3 | **`metrics_task()` も別 task (priority 5) で常時稼働** | 100% | 3.7 ms | 4.2 ms |

**重要な発見**:

1. **ESP32-C5 は single HP core**だが FreeRTOS の preemption + priority 階層化で実用マルチタスク化が可能 (LP core は WiFi/UDP には使えない、ULP 用途のみ)
2. **UDP rx だけ task 化しても tail 解消せず** — broadcast 送出が main loop 内で待たされると、TSF を早く取っても受信側の観測時刻が遅延
3. **broadcast 系も別 task 化が必要** = "rx fast / broadcast fast" の両方が揃って初めて短い tail

**設計原則** (`tools/esp_firmware/metrics_radio/` を本物 firmware に組み込む時):
- UDP rx と metrics broadcast は **必ず別 task** で動かす (main loop に置かない)
- main loop の周期処理 (CAN polling, WebServer 等) は UDP 経路と完全 isolation
- `delay(N)` は使わず `vTaskDelay()` か `yield()`

SanRei_HID 側の commit `44494c0` (branch `feat/radio-metrics-integration`) に reference 実装。

### C.21 不在 IP 宛て unicast flood は AP の ARP overhead を増やす (Phase 3 §2.13)

「production 10 STA」を 1 台 reflector + 9 不在 IP 宛て unicast 100 Hz 各で simulation した試験で、**AP queue 全体が顕著に悪化**を観測。

**観測** (1+9 不在 spread vs 全部 .111 集中 1000 Hz):

| leg | 集中 | 不在 spread | 倍率 |
|---|---|---|---|
| F) DL→.111 AP 滞留 | 4 μs | 2,895 μs | **700×** |
| C) bc cal AP 滞留 | 170 μs | 1,645 μs | 10× |
| D) uc cal AP 滞留 | 582 μs | 3,033 μs | 5× |

**原因**: 9 個の不在 IP 宛て 900 Hz は AP が ARP request を **broadcast し続ける** ことになり、AP 自身の処理が飽和。WiFi queue 全体が遅延。

**含意**:
- 「9 割不在 IP 宛て flood」は **production 10 STA とは異なる挙動** (= ARP request flood)
- production 10 STA は ARP cache 済で AP overhead 低い、これを simulation するには **物理 STA 複数台 associate** が必要
- 1 台 reflector しか無い PoC 環境では:
  - **集中 mode (試験 1)** = HID 律速の評価
  - **不在 spread mode (試験 2)** = AP の ARP overhead 限界の評価
  - 両者は補完的、production の queue 挙動再現にはどちらも不向き

**教訓**: 試験計画で "不在 IP は AP queue 負荷を増やさない" と推測したが、実際は **増やす方向に作用** (ARP overhead 経由)。production simulation では fake STA associate 等の工夫が要る。

### C.20 PoC reflector firmware が 1000 Hz DL を捌けない (E−F ギャップ 13 ms)

§2.12 実環境想定試験 (1000 Hz unicast flood) で、wire → HID 合算 OWD = 13.3 ms、しかし sniffer 経由の AP 滞留は ~0 ms。**差 13 ms ≈ reflector 内 UDP socket queue + 処理遅延**。

**原因 (推定)**:
- `tools/esp_firmware/metrics_radio_reflector/metrics_radio_reflector.ino` の `loop()` で `udp_in.parsePacket()` を polling する設計
- 1000 Hz 到着率に対し loop 1 周あたり 1 packet 処理 + UART JSON 出力で律速、queue 溜まる
- 100 Hz 程度の本来想定では問題なかった (Phase 3 overnight 6h で rx_dl=2.16M, loss 0.134% 実績)

**含意**:
- 本走 `SanRei_HID` の lwIP buffer + RTOS task 設計が重要、PoC reflector とは別実装が必要
- 試験計画書に "1000 Hz 規模で reflector firmware の捌き性能限界が露呈する" 注記
- 計測値の解釈で raw OWD = AP queue + air + **HID 処理時間** であり、HID 処理時間が dominant な場合があると認識

**今後の検証**:
- reflector firmware を RTOS task ベース + lwIP recvfrom() に書き換えて再試験
- もしくは本物 SanRei_HID 実装の rx_dl 処理時間を直接測定

### C.19 reflector `FORCE_LEGACY_11A=true` で UDP unicast が deliver されない

`docs/phase3_findings.md` §2.7 (air/wire diff、AX210 monitor が HE PPDU 不可) のために reflector firmware に試験フラグ `FORCE_LEGACY_11A=true` を導入し、AP が STA に 11a で送信する状態を作った。

**症状** (戻し忘れて再試行時に発覚):
- WiFi association OK (ch=112、rssi=-30)、ICMP ping 通る
- reflector serial に `# listen UDP 40001 (downlink)` 表示 (socket listen 成立)
- pc_emulator が 40001 unicast 送出、AIPC enp34s0 / RasPi eth0 SPAN 両方で物理 packet 確認
- それでも reflector の `udp_in.parsePacket()` が trigger されず `rx_dl=0`
- broadcast (52001/50001) は正常に出力されている

**仮説**: LN6001-JP の AP が 11a 強制 STA への UDP unicast を deliver しない or 大幅 deferral する挙動。ICMP は L2 forwarding で通るが、UDP unicast の AP queue 経路で何か起きている。AP 内部 log 取れないので確定は不可。

**対応**: `FORCE_LEGACY_11A=false` で再 flash → rx_dl 即時復活 (10 s 試験で N=1001、100% delivery)。

**教訓**:
- 試験用フラグは default `false` で書く
- 試験で `true` 化した時は同 commit で「試験後 false に戻す」を TODO に記録
- air/wire diff 等で 11a 強制が要る試験の後は **必ず本走 firmware に flash 戻し** をルーチン化
- "ping は通るが UDP unicast だけ不通" の症状は 11a/legacy 強制を疑う

### C.18 gtnlv-rpid が独自 sniffer parser を持つ罠

`tools/sniffer_runner/run.py` (offline 解析用) と `tools/rpi_daemon/gtnlv_rpid.py` (本番デーモン) **両方に sniffer binary parser がある** (FRAME_STRUCT、CSV header 含む)。C.17 で Entry を拡張した時、`sniffer_runner` だけ修正して `gtnlv_rpid.py` を放置 → smoke test で `sniffer.csv` に `tsf_us` 列が出ずに気付いた。

**修正**: `gtnlv_rpid.py` の `FRAME_STRUCT = "<I I I Q B B B b H H 6s 6s B B H"` (Q 追加)、`LEN_FRAME_PAYLOAD = 44`、CSV writer の header に `tsf_us` 追加、unpack 順に `tsf_us` 追加。両 parser を同期させる。

**教訓**: sniffer binary format を変更する時は **両 parser の grep + 同時更新** が必要。

> 将来統一案: `tools/sniffer_proto/` のような共通モジュールに parser を移して両方が import する構造にすべき (現状未着手、大会後の整理候補)。

---

## §D. dev ブランチで残った未解決課題 (現プロジェクトで再評価対象)

dev branch `doc/conversation_log.md` 末尾の TODO で、現プロジェクトに持ち越されたもの:

| 項目 | 現プロジェクトでの対応 |
|---|---|
| 複数 Reflector 対応 | metrics_radio v2.0.0 仕様で対応設計済 (port `40000 + robot_id` 等)、実装未着手 |
| Uplink Seq# = 0 問題 (dev 観測) | Phase 0 R12 / Phase 3 で再現性確認済、現実装の reflector はちゃんと incrementing |
| TSF-Unix キャリブレーション精度 | 線形回帰 + rolling-window min-filter で改善済 (Phase 3 で sniffer bridge 設計に発展) |

---

## §E. 大会後に追加検討する候補 (deferred to next season)

| 候補 | 期待効果 |
|---|---|
| **UWB DW3000** を研究用 ground truth として導入 | PCB 内蔵、外装変更不要、ns 精度。SSL コミュニティに先行事例なし、研究的価値高 |
| TCXO 換装 (SiT5356 等) | 10 分試合の自走時ドリフトを 60μs オーダーに |
| ~~chrony で host PC ↔ RasPi 直接同期~~ → **Phase 3 後段で実施済** (RasPi を master 化、AIPC が client、§C.13) | raw OWD の NTP drift ~2 ms → ~200 μs に圧縮 |
| host PC ↔ RasPi PTP (eth0 PHC を master に) | 上記より更に高精度 (μs 単位)、ただしハード制約あり |
| GS308E / Xikestor 以外の managed switch で mirror UDP unicast 挙動を確認 | host port mirror で UDP unicast 落ちる問題が switch 共通か否かを確定 |
| ~~sniffer firmware の native USB-CDC 化~~ → **不採用** (§2.16 で検証) | native USB は CP2102N より遅かった (HWCDC overhead)。transport delay の真因は host 側 batch read で、`in_waiting` read 修正で解決済 (200ms→1-2ms)。CP2102N 維持が最適 |

---

## §F. dev ブランチを参照する方法

dev のコードを参照したい時は以下:

```bash
git ls-tree -r --name-only origin/dev          # ファイル一覧
git show origin/dev:esp32-sniffer/main/main.c  # 特定ファイルの中身
git show origin/dev:doc/conversation_log.md    # 会話ログ全体 (886 行、試行錯誤の記録)
```

dev ブランチは **削除せず保持**。再利用可能な実装パターンとして:
- `estimate_tsf()` 実装 (`esp_timer_get_time()` で capture して後追いで TSF 変換、`esp32-sniffer/main/main.c`)
- AP BSSID 自動学習 (暗号化下りフレームの src MAC を学習する pattern)
- USB シリアル出力フォーマット (`$SNF,<D|U>,...` ASCII CSV、人間可読、デバッグ用)
- Sequence Number Bridge (UDP payload と 802.11 hdr_seq を組み合わせて 3 者統合)
- CCMP PN dedup (複数ロボット環境)

これらの詳細は `git show origin/dev:doc/conversation_log.md` から発掘可能。
