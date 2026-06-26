# PPS による TSF↔unix 高精度同期 設計

> sniffer C5 が GPIO で PPS (Pulse Per Second) を出力し、RasPi が GPIO 割り込みで
> 受けることで、**TSF↔unix の bridge_offset を μs 精度**に引き上げる設計。
> 現行の UART read 経由 bridge (transport jitter 数 ms) を置換する研究的拡張。
>
> 位置付け: 提出主軸の **wifi_leg (clock 非依存) には不要**。**sniffer-based
> bridge OWD (§2.9)** と **air-wire diff** の絶対値精度を sub-ms 化するための拡張。
> `docs/lessons_learned.md` §E の大会後候補から前倒し検討。

## 1. 原理 — GPS の 1PPS + NMEA と同じ構造

| 信号 | 経路 | 役割 | 時刻精度への寄与 |
|---|---|---|---|
| **PPS パルス** | C5 GPIO → RasPi GPIO (割り込み) | **正確なタイミング** | ✅ これが精度を決める |
| **TSF ラベル** | C5 → UART (PPS marker record) | その PPS の TSF 値 (内容のみ) | ❌ transport 遅延は無関係 |

```
C5: TSF を監視 → TSF が境界値 X (例: 1秒境界) に達した瞬間に GPIO パルス
                  同時に "この PPS の TSF = X" を UART で送信 (遅延 OK)
RasPi: GPIO 割り込みで CLOCK_REALTIME を記録 (μs 精度、/dev/pps0)
       UART から TSF=X ラベルを受信
   → bridge: (TSF=X, unix=割り込み時刻) を μs 精度で対応付け
```

UART の数 ms 遅延は「TSF=X というラベル」を運ぶだけで、**bridge の時刻精度には効かない**。

## 2. 現状確認済の環境 (2026-05-28、Raspberry Pi OS bookworm)

| 項目 | 状態 |
|---|---|
| pps-gpio kernel module | ✅ kernel 6.12.75-rpt-rpi-2712 built-in、`gpioinfo` で `consumer="pps@12"` 確認可 |
| **pps-gpio device** | **`/dev/pps0` = `pps@12.-1`** (BCM18 PPS、`pps@12` の 12 は **物理 pin 12** を示す) |
| **eth0 PHC PPS** | **`/dev/pps1` = `ptp0`** (boot 順入替で番号変動するため `cat /sys/class/pps/*/name` で確認) |
| 40-pin header GPIO chip | **gpiochip0 = pinctrl-rp1 (54 lines)** |
| pps-tools / gpiod | ✅ インストール済 (ppstest、gpioinfo、gpiomon)。libgpiod 2.x syntax (`-c chipname`) |
| config.txt | `/boot/firmware/config.txt`、末尾に `dtoverlay=pps-gpio,gpiopin=18` 追加 |
| C5 sniffer の GPIO 使用 | GPIO27 (RGB LED) のみ → 空き GPIO 多数 |

## 3. 配線 — RasPi PPS は sniffer のみ、HID PPS はオシロ比較専用

**RasPi に PPS として繋ぐのは sniffer だけ**。HID PPS は **オシロで sniffer PPS と
比較する検証用** で、RasPi には繋がない (本走で HID は USB 不接続運用 = どこにも有線で
繋がない)。

| source | C5 GPIO | 接続先 | 用途 |
|---|---|---|---|
| **sniffer C5** | **GPIO10** | **RasPi BCM18 (pin 12) → /dev/pps0** ＋ オシロ CH1 | TSF↔unix bridge (RasPi) ＋ オシロ基準 |
| **HID C5** | **GPIO10** | **オシロ CH2 のみ (RasPi 非接続)** | sniffer PPS との Δt 比較 (§10) 専用 |
| GND | GND | RasPi GND (pin 6/9/14) ＋ オシロ GND — USB 給電なら共通 (要確認) | — |

> - C5 の GPIO は **両方とも GPIO10 で統一** (firmware 番号統一、運用シンプル化)。
>   strapping pin (GPIO8/9/15/27 等) を避けた汎用 IO。
> - RasPi の BCM18 は config.txt の `gpiopin=18` と揃える。3.3V 直結可。
> - **pps-gpio device は `/dev/pps0`** (kernel 命名は boot 順、現環境では gpio が先)。
>   eth0 PHC は `/dev/pps1`。番号は `cat /sys/class/pps/*/name` で確認:
>   - `pps@12.-1` → pps-gpio (BCM18)
>   - `ptp0` → eth0 PHC
> - **HID PPS は RasPi に繋がない** (オシロ CH2 のみ)。HID の TSF↔unix 同期は
>   「sniffer bridge を HID の rx_dl TSF に適用」で行い、その正当性 (HID と sniffer の
>   TSF が同じ精度で AP に同期) を §10 のオシロ Δt 比較で裏付ける。
> - HID firmware の `"pps"` JSON broadcast (§4.2) は RasPi で受信しないが、PPS が出ている
>   TSF 境界を確認するデバッグ用に残す (オシロ比較には GPIO edge のみで足りる、optional)。

## 4. C5 firmware 設計 (sniffer.ino)

### 4.1 PPS パルス出力

- `PPS_GPIO` を `OUTPUT` に設定 (sniffer/HID とも **GPIO10** に統一)
- **TSF 境界検出**: `esp_wifi_get_tsf_time()` を監視し、TSF が 1秒境界 (= `tsf_us % 1_000_000` が小さい) を跨ぐ瞬間を検出
- **タイミング精度**: `loop()` 内検出では ±1ms ずれる → **esp_timer one-shot callback** で次の境界時刻に正確に発火させる方式を推奨
  - 次の境界 TSF を計算 → esp_timer↔TSF 変換で esp_timer 発火時刻を算出 → `esp_timer_start_once()`
  - callback で `gpio_set_level(PPS_GPIO, 1)` → 短パルス後 `0`
- パルス幅: ~10-100 μs (RasPi の pps-gpio が assert edge を取れれば十分)

> **★ 両 chip で同じ絶対境界値を使う (オシロ検証 §10 の前提)**: sniffer と HID は
> 同じ AP の beacon に TSF 同期しているので、**両者が `tsf_us` の同じ絶対境界**
> (= `tsf_us` が `1_000_000` の倍数を跨ぐ瞬間 = AP TSF の N 秒ちょうど) で PPS を
> 出せば、**理想的に両パルスが同時刻**になる。境界の定義 (周期・位相) を両 firmware で
> 厳密に揃えること。揃っていないと §10 のオシロ比較が無意味になる。
> PPS は **rx_dl 等のパケット受信イベントとは独立**した定期 (1 Hz) パルスである点に注意
> (rx_dl の TSF は計測対象で、PPS で確立した bridge_offset を後段で適用する別物)。

### 4.2 PPS marker (TSF ラベル) — sniffer は UART、HID は broadcast

PPS パルスを出した瞬間の TSF 値を RasPi に伝える経路が 2 系統で異なる:

**sniffer (UART、USB 接続あり)** — sniffer.ino に新 record type:

```
TYPE_PPS = 0x04
payload: uint64 tsf_at_pps + uint64 esp_timer_at_pps (16 byte)
```

**HID (radio_metrics broadcast、USB 不接続)** — metrics_radio に新 JSON type:

```json
{"type":"pps","t_pps_tsf_us":<uint64>,"t_pps_esp_timer_us":<uint64>}
```

- HID は `metrics_radio` ライブラリ経由で 52000+id に broadcast (rx_dl/tx_ul/hb と同じチャネル)
- PPS 出力ロジック (TSF 境界検出 + esp_timer callback + GPIO パルス) は sniffer と共通だが、
  marker 送出だけ UART → broadcast に差し替え
- **設計上の含意**: HID PPS により HID 自身の TSF↔unix を μs 同期できる →
  **sniffer bridge を介さず HID 単独で rx_dl の絶対 OWD を μs 精度**で出せる
  (sniffer bridge は dedicated source として複数 reflector を一括カバー、
   HID PPS は各 HID 自身の絶対同期、用途が補完的)

- RasPi は各 /dev/ppsN の PPS event 順と marker (UART or broadcast) の TSF を
  **順序対応** で join (1 Hz なので取り違えにくい)

## 5. RasPi 設定

### 5.1 pps-gpio overlay

`/boot/firmware/config.txt` の末尾に **sniffer 1 系統のみ**追加:

```
dtoverlay=pps-gpio,gpiopin=18   # sniffer C5 GPIO10 → BCM18 → /dev/pps0 or /dev/pps1
```

再起動後、`pps@12` (= BCM18 = pin 12) と `ptp0` の番号割当を確認:

```bash
ls /dev/pps*                                 # /dev/pps0 と /dev/pps1 が両方ある
for d in /sys/class/pps/pps*/; do
  echo "$(basename $d) -> $(cat $d/name)"
done
# 出力例:
#   pps0 -> pps@12.-1   ← これが sniffer (BCM18)
#   pps1 -> ptp0        ← これが eth0 PHC

# sniffer 側を確認
sudo ppstest /dev/pps0          # assert sequence が 1Hz で進めば OK
```

> **重要**: kernel 命名は **boot 順依存**。pps-gpio が先に attach されると `pps0`、
> 後だと `pps1`。**毎回 `/sys/class/pps/*/name` で確認**してから gtnlv-rpid に
> `--pps-device /dev/pps0` などで明示渡しすること (実機 RPi OS では `/dev/pps0` で確認済)。

### 5.2 gtnlv-rpid での PPS event 読取り

chrony refclock は **絶対時刻 PPS (GPS 等)** 用。今回は AP TSF 基準の相対同期なので
chrony には入れず、**gtnlv-rpid が /dev/pps0 (sniffer source) を直接読む**:

- `ioctl(fd, PPS_FETCH)` (RFC 2783 timepps.h) で PPS event の assert timestamp
  (CLOCK_REALTIME) を取得
- UART の PPS marker record (TSF 値) と順序対応で join
- `(tsf_pps, unix_pps)` ペアを `pps_bridge.csv` に出力

## 6. bridge_offset の更新 (sniffer_bridge.py)

```
現行 (UART): bridge_offset = min(t_rpid_recv_unix − tsf_us/1e6)
             → transport jitter (数ms) が floor 推定に残る

PPS:         bridge_offset = unix_pps − tsf_pps/1e6   (per-PPS、直接)
             → UART transport 無関係、GPIO 割り込み jitter のみ
```

## 7. 期待精度

| 方式 | bridge_offset の floor 精度 |
|---|---:|
| 現行 (UART read、in_waiting 修正後) | ~数 ms (transport jitter) |
| PPS (素の GPIO 割り込み、非 RT kernel) | **~10-50 μs** |
| PPS (将来 RT kernel / chrony pps-gpio) | ~1-10 μs |

→ sniffer-based bridge OWD / air-wire diff の絶対値が **数 ms → μs** に。

## 8. 実装ステップ (タスク #65-72)

**sniffer 系統 (UART marker)**:
1. **#65** 設計 doc (本書) ✅
2. **#66** sniffer.ino に GPIO10 PPS 出力 + PPS marker record (UART) ✅
3. **#67** 配線 (sniffer GPIO10 → RasPi BCM18 + GND) ✅ ADALM2000 で 3.4V/50μs/1Hz 確認
4. **#68** RasPi pps-gpio overlay (gpiopin=18) + **`/dev/pps0`** 確認 ✅ (kernel 命名は boot 順、要 `/sys/class/pps/*/name` 確認)
5. **#69** gtnlv-rpid に `/dev/pps0` PPS event 読取り + UART marker join (未実装、次)
6. **#70** sniffer_bridge.py を PPS ペア対応 + UART bridge と精度比較 (未実装)

**HID 系統 (オシロ比較専用、RasPi 非接続)**:
7. **#71** ESP32C5Controller (SanRei_HID) に GPIO10 PPS 出力 + metrics_radio に `pps` JSON type 追加 (実装済、dev `2a3daf1`)
8. **#72** ~~RasPi で HID PPS を受ける~~ → **不要** (HID PPS は RasPi に繋がない)。代わりに **§10 オシロで sniffer PPS と HID PPS の Δt を比較**して chip 間 TSF 同期精度を検証

## 9. 注意点・リスク

| 項目 | 内容 |
|---|---|
| C5 TSF 推定精度 | midpoint fit ~1-2 μs (Phase 0 R4) → PPS GPIO jitter より小さく問題なし |
| C5 GPIO 出力 latency | esp_timer callback で ~μs。loop() 直書きだと ±1ms なので不可 |
| RasPi 割り込み jitter | 非 RT kernel で ~10-50 μs。RT kernel / chrony pps-gpio で改善 |
| RPi OS 移行との整合 | pps-gpio overlay は RaspberryPiOS の方が設定が素直。移行後に本実装する方が楽 |
| 屋内会場での意義 | EC25J の GNSS 1PPS が屋内受信困難な代替として、AP TSF を C5 が PPS 化 = 屋内でも μs 精度の共通時刻基準 (絶対時刻ではなく AP TSF 基準の相対同期) |

## 10. オシロによる chip 間 TSF 同期精度の直接検証

sniffer と HID が**同じ AP TSF の同じ絶対境界** (§4.1) で PPS を出すので、両 PPS
パルスの立ち上がり edge をオシロで重ねれば、**両 chip の時刻同期精度 Δt を直接測定**
できる。これは計測系全体の時刻同期の下限を与える最も直接的な検証。

### 10.1 測定の意味

```
sniffer PPS edge ──┐
                   ├─ 理想は同時刻 (同じ AP TSF 境界で発火)
HID PPS edge ──────┘
                   Δt = 両 edge の時間差
```

- **Δt = 両 chip の (TSF 推定 + GPIO 出力 latency) の相対誤差**
- これは bridge_offset の **chip 間相対精度の実測下限**
- bridge OWD で sniffer 基準と HID 基準を突き合わせる時の系統誤差にも相当

### 10.2 Δt の内訳と期待値 (実測で訂正済)

| 誤差源 | 寄与 |
|---|---:|
| **esp_timer dispatch jitter** (FreeRTOS scheduling、kernel level) | **10-30 μs** ← 実測主因 |
| TSF↔esp_timer fit 方式の差 (sniffer=midpoint a=1 / HID=linear regression a 計算) | 数 μs |
| 各 chip の midpoint fit 残差 (Phase 0 R4) | ~1-2 μs |
| crystal drift (100ms 較正周期内、20-40 ppm) | ~2 μs |
| GPIO 出力 latency 個体差 | ~1 μs |
| beacon 受信タイミングのずれ (sniffer/HID の距離・RSSI 差) | 数 μs |
| **合計 Δt 期待値** | **30-50 μs オーダー** (実測 §10.5 = 35 μs) |

当初は数 μs を見込んでいたが、実測 (§10.5) で esp_timer task の dispatch
jitter が主因と判明し、期待値を訂正。

### 10.3 プローブ手順

1. sniffer (GPIO4) と HID (GPIO10) を**同じ AP・同じ ch112** に associate
2. 両 firmware で**同じ絶対境界** (`tsf_us % 1_000_000` 0 跨ぎ) + 同じパルス幅 (~10-100 μs)
3. オシロ: CH1 = sniffer PPS、CH2 = HID PPS、**プローブ GND を両 C5 の GND に共通**接続
4. trigger を CH1 立ち上がり、CH2 の edge との時間差 Δt を読む (1 Hz なので 1 秒ごとに更新)
5. 複数パルスで Δt の分布 (jitter) も観測 — 一定なら系統オフセット、ばらつくなら beacon 同期ジッタ

### 10.4 結果の解釈

| Δt | 解釈 |
|---|---|
| **数 μs、安定** | 両 chip の TSF 同期良好 → bridge_offset 信頼できる、PPS 同期が機能 |
| 数十 μs 以上 | midpoint fit or beacon 同期に問題。どちらかの chip の RSSI / fit 残差を確認 |
| パルスごとに大きくばらつく | beacon 取り逃し or TSF discontinuity (re-associate)。`g_calib_valid` / fit 周期を見直し |
| 一定の系統オフセット | 両 chip の GPIO latency 差 or 境界定義の位相ずれ → firmware 側で補正可 |

> **注意**: この Δt は「sniffer と HID の **相対**同期精度」であり、RasPi unix との
> **絶対**精度 (= GPIO 割り込み jitter ~10-50 μs、§7) とは別物。オシロ検証は chip 間の
> 相対精度 (計測系の時刻基準の一貫性) を、PPS bridge 試験 (§8 task #70) は RasPi unix との
> 絶対精度を、それぞれ確認する。両方が揃って初めて絶対 OWD が μs 精度で信頼できる。

### 10.5 実測結果 (2026-05-28、両 devkit で確認)

**構成**:
- sniffer (LED 青)、HID firmware (SanRei_HID dev `2a3daf1`) 焼いた devkit を reflector 代用
- 両 devkit ともに同じ AP (LN6001-JP ch112)、両 GPIO10 から PPS パルス
- オシロ: CH1 = sniffer (青)、CH2 = HID (緑)、GND 共通

**測定値**:

| 指標 | 値 | 評価 |
|---|---:|---|
| **Δt (CH2 − CH1)** | **−35 μs** (HID が sniffer より 35 μs **早い**) | §10.2 訂正後の期待値 30-50 μs と整合 |

**事前確認の動作 (この測定の前提)**:
- HID firmware @ devkit: rx_dl 1501/1500 件、pps JSON broadcast 21/20 件、TSF 境界精度 (`t_pps_tsf_us = N × 10^6`) ✅
- sniffer GPIO10 + dst filter: cb_total 3,134 → captured 2,058 (dst filter 通過 65.7%)、dropped=0、broadcast 1,029 + reflector unicast 1,007 のみ通過 ✅

**解釈**:
- 35 μs は **計測系全体の chip 間 TSF 同期精度の下限**
- HID rx_dl 絶対 OWD への系統 bias ≈ **35 μs** (DL OWD median ~2.3 ms に対し 1.5%)
- 提出文書の精度要件 (ms オーダー) には十分余裕

**残課題 (35 μs の系統 vs jitter 切り分け)**:
- ~~単一サンプル観測~~ → **§10.6 で ADALM2000 連続取得 1423 events で確認済**

### 10.6 ADALM2000 による Δt 分布解析 (2026-05-28、N=1423)

§10.5 の単発計測 (−35 μs) を ADALM2000 (libm2k Python、AnalogIn 2ch 10 MS/s) で連続取得して分布化。`tools/m2k_pps_diff/pps_diff.py` で CH1 立ち上がりトリガ + sub-sample 線形補間。

**HID idle (PPS のみ、24 分)**: median **−41.5 μs**、sd **14.2 μs**、p99-p01 = 88.1 μs。  
ヒストグラム上は **bimodal** (主峰 −42 μs 約 95% + 副峰 0〜+40 μs 約 4%)。

→ 系統 (−42 μs オフセット) と jitter (esp_timer dispatch、sd 14 μs) の両方が寄与、副峰は偶発 preempt。

**負荷依存試験 (HID UDP rx 100/250/500/750/1000 Hz、各 5 分)**:

| 負荷 [Hz] | median [μs] | sd [μs] | p95-p05 [μs] |
|---:|---:|---:|---:|
| 0 (idle) | -41.5 | 14.2 | 26.7 |
| 100 | +8.1 | 9.5 | 21.8 |
| 250 | +9.4 | 31.2 | 97.1 |
| 500 | +10.6 | 34.5 | 119.0 |
| 750 | +20.9 | 39.7 | 138.3 |
| 1000 | +24.6 | 46.3 | 166.8 |

線形ではなく **2 段ステップ**: idle → 100Hz で median が +50 μs 急ジャンプ (rx 割込で esp_timer が遅延)、100-500Hz で plateau、750-1000Hz で再シフト。sd は単調増加。

**改善試行 (semaphore + 高優先 task) → 逆効果**:
- `pps_cb` を `xSemaphoreGive` のみ + 別 task (`configMAX_PRIORITIES-1` = 24、Wi-Fi 23 より上) で GPIO 駆動に変更
- 結果: task switch overhead が常に乗り、idle・100Hz・1000Hz 全条件で median ±5 μs シフト、p95-p05 は 100Hz/1000Hz で悪化 (21.8→25.0、166.8→180.3)
- 原因: dispatch chain が `esp_timer task → semaphore → wake → pps task → GPIO` と 1 段増え、preempt 防御効果よりオーバーヘッドが勝った

**ESP_TIMER_ISR dispatch 試行 → コンパイル不可**:
- `dispatch_method = ESP_TIMER_ISR` を試したが、Arduino-ESP32 3.3.8 の sdkconfig は `CONFIG_ESP_TIMER_SUPPORTS_ISR_DISPATCH_METHOD=n` で enum 値自体除外
- framework rebuild が要るため不採用

**esp_timer 版の到達点 (当時)**:
- PPS sync jitter は **`esp_timer` dispatch jitter が支配** = chip 間 TSF 同期の下限 (idle sd 14.2 μs)
- median は **負荷で 50 μs シフト**するため、bridge_offset は無負荷 cal を別途取るか負荷条件別に分けて見る必要あり
- WiFi OWD median に対し 1〜2%、Year 1 challenge には十分

**GPTimer free-running PPS に置換済 (2026-06-02、`phase3_findings.md` §2.22)**:
- sniffer/HID とも **GPTimer (driver/gptimer.h) を auto-reload 1Hz で自走** + アラーム ISR でパルス生成。esp_timer task の dispatch jitter を排除。位相は loop が回った時だけ `gptimer_set_raw_count` で TSF 境界へ再同期、record は ISR 捕捉時刻からの実 TSF。
- ADALM2000 実測: **idle sd 14.2→5.0 μs (bimodal 解消)**、100 Hz で取りこぼし 4→0、**1000 Hz でも PPS 継続** (旧 one-shot+loop re-arm は loop starve で停止していた)。
- **残課題**: 1000 Hz の極端負荷では HID 単一 loop で WiFi rx と TSF 較正を両立できず位相が ~150 μs ドリフト (PPS タイマ範囲外の構造的限界、本番 100 Hz では非問題)。GPIO matrix でハードウェア直結すれば ISR レイテンシも排除できるが現状不要。

参照: `docs/phase3_findings.md` §2.18 (esp_timer 特性化) / §2.22 (GPTimer 化)、データ: `out/m2k_pps_diff_*` `out/m2k_*`、スクリプト: `tools/m2k_pps_diff/`

## 関連ドキュメント

- `docs/measurement_architecture.md` §5.2 (bridge_offset)、§8 (NTP master 化)
- `docs/phase3_findings.md` §2.16 (transport delay の真因 = batch read)
- `docs/raspi_setup.md` §1 (pps-tools / gpiod)
- `docs/lessons_learned.md` §E (大会後候補)
