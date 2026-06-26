# 家庭環境 6-robot OWD 実験計画

> 作成 2026-06-21。普段使いの家庭ネットワーク (AP まで **ハブ 4 段**) で XIAO C5 **6 台** を
> robot0-5 として同時計測し、(1) 多 robot スケーラビリティ、(2) 劣化トポロジ (多段ハブ) 耐性、
> (3) 提出 7 項目の家庭環境値を取得する。会場 AP 環境への追随性検証も兼ねる。

## 0. 前提・既知の制約

- **SPAN は有効** (RasPi-**TL-SG105E** セット使用): AP を TL-SG105E の mirror source port に
  繋ぎ、eth0 を destination にすれば、家庭の他ハブが何段あっても **AP ポートミラーで
  wire 観測が成立**する (4 段ハブは TL-SG105E の上流にあり SPAN を妨げない)。
  → `wire` leg (eth0 promisc SPAN + PHC hwtstamp) 有効。**フル区間分解可**:
  下り host→wire(SPAN)→air(sniffer)→HID、上り HID→air→wire。daemon は `--wire eth0`。
- **OWD の正/参考**: 絶対 OWD は HID rx_dl(TSF)+PPS bridge (`owd_bridge`、host clock 非依存) を正、
  air↔wire 軸は同一機 (B-B) なので bridge 不要で leg 分解可。host-receive 系 (`owd_dl_approx`)
  はハブ遅延が乗るため参考に留める。
- **多段ハブの遅延寄与**: 4 段ハブは sender→(hubs)→TL-SG105E→AP の経路に L2 store-and-forward
  遅延を上乗せ。**この遅延は wire 観測点 (AP ポート) より上流**なので、host→wire leg に含まれ、
  wire→air leg (AP queue/送信) と分離して観測できる (劣化トポロジ評価に好都合)。
- **robot_id 割当**: reflector firmware の `ROBOT_ID` はコンパイル時定数 (`metrics_radio_reflector.ino:30`)。
  6 台分の対応は §2 で決める (6 ビルド / NVS ランタイム / MAC 由来)。
- **ストレージ**: RasPi5 SD は過去 I/O エラーで交換歴あり。高レート sniffer ログ (6 robot で増大)
  を SD に直書きは書込寿命リスク。**データ外部化必須** → §6。

## 1. ハードウェア構成

```
[sender (RasPi4 .233 or RasPi5 local)]
        │ downlink 40000+id
        ↓
   [hub1]-[hub2]-[hub3]-[hub4]   ← 家庭の既設ハブ (4段、TL-SG105E の上流)
        │
        ↓
   [TL-SG105E (SPAN)] ── AP ポート mirror ──→ [RasPi5 eth0] (promisc, PHC hwtstamp, IP無)
        │ (AP は mirror source port に接続)
        ↓
     [家庭 AP] ──WiFi(ch?)──→ [robot0-5 XIAO C5 ×6]
        ↑ uplink/radio_metrics broadcast 50000/52000+id

[RasPi5 計測]
   ├ eth0  SPAN destination (wire 観測点 = AP ポート)
   ├ eth1  計測 LAN (.231、radio_metrics 受信、TL-SG105E の通常ポート)
   ├ ttyUSB0  sniffer C5 (PROMIS, PPS GPIO10→pin12 /dev/pps0)
   └ powered USB hub ── robot0-5 XIAO C5 ×6 (電源+flash のみ、通信は WiFi)
```
> ポイント: **AP を TL-SG105E の mirror source、eth0 を destination** にすれば、
> 上流に家庭ハブが 4 段あっても AP ポートの上下トラフィックは全て eth0 にミラーされる。
> wire 観測点が「AP ポート」になるので、4 段ハブ遅延は host→wire leg に含まれる。

- **6× XIAO C5 = robot0-5**: powered USB hub 経由で電源供給 (XIAO C5 native USB=ACM、
  HWCDC は serial 不安定だが電源用途なので可)。reflector/HID firmware を焼く。
- **sniffer**: 既存 CP2102N devkit C5 (ttyUSB0)、TEAM_SSID_OPEN に追従、PPS GPIO。
- **sender**: 6 robot へ 40000+id downlink。RasPi4 WebUI (`:8502`) で動的 join/leave。
  4 段ハブ環境なので sender も RasPi5 ローカルにする選択肢あり (ハブ経路短縮、要比較)。
- **電源**: XIAO 6 台 = powered hub 必須 (RasPi5 USB 給電上限超過回避)。

## 2. セットアップ手順

1. **robot_id 割当方式を決定** (推奨順):
   - **(A) MAC 下位バイト → robot_id マッピング (推奨)**: firmware を 1 種ビルドし、
     起動時に自 MAC を読み、テーブル (MAC→0..5) で robot_id 決定。NVS 不要・1 ビルドで 6 台。
   - **(B) NVS ランタイム設定**: set_ssid と同様の downlink コマンド (例 `set_robot_id`) で
     NVS に保存。SanRei_HID の `CMD_SET_ROBOT_ID` 経路を流用。
   - **(C) 6 ビルド**: `ROBOT_ID` を 0-5 で個別ビルド・個別 flash (最も単純、保守は手間)。
2. 6 台に firmware を flash (host arduino-cli build → esptool、§開発手順)。
3. 各 robot を TEAM_SSID_OPEN へ (`set_ssid.py --robots 0,1,2,3,4,5 --mode open`、揮発注意)。
4. sniffer を TEAM_SSID_OPEN に追従 (dashboard `/api/sniffer/ssid`、ただし既に open なら ~CFG 再送不要)。
5. sender (RasPi4 WebUI) に robot0-5 を join、レート 100Hz。
6. daemon を `--robot-ids 0,1,2,3,4,5 --wire eth0` で起動 (WAL + sniffer auto-recover 版、
   `ee8a447`/`48df2af`)。**SPAN 有効なので `--wire eth0` 込み**。録画は Parquet 外部化 (§6)。
7. TL-SG105E の port mirror 設定確認: AP ポート = source、eth0 接続ポート = destination。

## 3. トポロジ特性評価 (T0)

| 計測 | 手法 | 記録 |
|---|---|---|
| SPAN 動作確認 | eth0 promisc で AP ポートの上下 (40000 下り / 50000・52000 上り) が見えるか | 双方向ミラー確認 (本セッション 2026-06-19 に確認済の手順) |
| 4 段ハブ RTT 寄与 | `ping`/`owping` で sender→AP、各セグメント。**leg 分解の host→wire でも定量化** | hub 段あたりの遅延 (host→wire leg に出る) |
| AP チャネル/帯域 | `iw dev wlan0 scan`、近隣 AP・ch 混雑 (D6 同様) | 家庭の干渉環境 |
| wire↔air 整合 | sniffer air 観測と wire(SPAN) を `hid_seq`/`cycle_count` で突合 | leg 分解の健全性 |

## 4. 段階的スケーリング試験 (T1)

robot 数を **1 → 2 → 4 → 6** と増やし、各段で 5-10 分計測:

| 観測量 | 期待 / 確認点 |
|---|---|
| robot 別 下り/上り OWD (`owd_bridge`) | robot 数増で median/p99 が悪化するか (AP queue 競合) |
| **区間分解 host→wire→air→HID / HID→air→wire** | SPAN 有効。AP queue (wire→air) と 4段ハブ (host→wire) を分離して劣化要因を特定 |
| cycle_count 損失率 | robot 数増で損失増加? (共有 cycle_count、shared send-task) |
| sniffer fps / dropped | 6 robot 時の air fps と dropped=0 維持 (auto-recover 発火頻度も記録) |
| PPS bridge 残差 | 負荷増での同期精度劣化 |
| AP queue freeze 頻度 | 多段ハブ + 6 robot での発生 (wire→air leg の worst-case で観測) |

## 5. 提出 7 項目の家庭環境値 (T2) + 長時間ラン (T3)

- **T2**: 6 robot idle で 30-60 分、robot 別に OWD **Mean/Variance/Max** + median/p95/p99、
  Average Packet Loss、Data Rate、Interference (D6 ロジック)、Startup (D4 ロジック)、
  (Power は USB 電力計があれば)、Cost。
- **T3 長時間**: 6 robot idle 1-6h、提出データ本番。AP queue freeze 頻度、worst-case OWD、
  PPS 残差 1σ を録画 `pps_bridge.csv` から確定。**外部ストレージへ録画 (§6)**。

## 6. データ外部化 (§ 別ファイル `docs/data_externalization.md` で詳細)

要件: (1) SD 書込を避け寿命保護、(2) 6 robot 高レート sniffer データ (~5-10k fps) を収容、
(3) 別マシンで後解析。方式は §別ファイル参照。**live SQLite (tmpfs, WebUI 用 5min ring) は
ローカル据置、提出 CSV/録画 (RecordSink) のみ外部化**する。

## 7. 留意点 (本セッションの知見)

- set_ssid は揮発 → robot reboot 毎に open 再適用。6 台分は `set_ssid.py --robots 0,1,2,3,4,5`。
- sniffer 復帰後は NVS saved SSID で起動、`~CFG` 再送不要 (再送 re-associate が hang 誘発)。
- daemon は WAL + sniffer auto-recover 済 (6h 無人ラン対応)。
- 1000Hz は HID 単一 loop 制約で計測不可、本番 100Hz。
- XIAO C5 の native USB serial は不安定 → robot 用途は電源のみ、計測は WiFi 経由で問題なし。
```
