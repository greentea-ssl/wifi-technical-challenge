# Radio Metrics チャネル — HID 起源の WiFi 計測メタデータ (Port 52000 + robot_id)

HID が自身の WiFi RX/TX イベントに関するメタデータ (TSF / esp_timer / 連番等) を broadcast する独立チャネル。**v2.0.0 で導入**。RoboCup SSL 2026 Radio Communications Challenge への提出を含む、無線区間の片方向遅延 (One-Way Delay; OWD) 計測や電波品質モニタリングに利用する。

## 1. 目的

無線通信の **片方向遅延 (One-Way Delay; OWD) 計測**およびその他の電波品質メトリクスのため、HID が自身の WiFi RX/TX イベントに関するメタデータ (TSF / esp_timer / 連番等) を独立した broadcast チャネルへ出力する。

既存の `uplink_telemetry.md` (port 50000+id) は **CU 起源**のデータを HID が透過中継するだけのチャネルであり、ここに WiFi TSF を埋めるのは責任分界違反 (CU は WiFi を知らないし、知るべきでもない)。

`hid_bridge.md` (port 41000/51000) の「HID = アクティブエンドポイント」設計と整合する形で、port `52000 + robot_id` を **HID 起源の WiFi メトリクスチャネル**として新設する。

### 1.1 想定 listener

外部の計測機 (RoboCup チームの計測用 PC、運用ロガー等) が同 subnet で本チャネルを listen する想定。listener が居なくても HID の動作は影響を受けない (fire-and-forget broadcast)。

### 1.2 暗号化との関係

本チャネルの相関キー (`corr_unix_time`) は**受信フレームの payload から抜く**。HID は当該 STA の暗号鍵を持つため、SSID が **WPA2 でも Open でも payload を平文で扱える**。AP↔ホスト PC 間の有線部分も平文 Ethernet。**本仕様は SSID の暗号化方式に依存しない**。

## 2. ポートとアドレッシング

| 方向 | ポート | 送信方式 | 用途 |
|---|---|---|---|
| Uplink (HID → PC) | `52000 + robot_id` | **broadcast** (`192.168.x.255`) | HID 自身の WiFi RX/TX タイムスタンプメタデータ |

エンコーディング: **UTF-8 JSON**、1 UDP パケットにつき 1 JSON オブジェクト (`hid_bridge.md` と同じ慣習)。

設計上の位置付け:

- HID は **アクティブエンドポイント**として自身の WiFi 経験を観測・報告する
- CU は関与しない (CU 行きのフレーム情報ではなく HID の WiFi 層情報)
- broadcast にすることで複数の計測ロガーが同時受信可能 (試合中の運用ロガーと開発時の解析ロガーが併存できる)

## 3. メッセージ種別

### 3.0 全種別共通フィールド `meta` (off-board 観測用 join key)

すべての radio_metrics メッセージ (`rx_dl` / `tx_ul` / `hb`) は **先頭フィールドとして `meta`** を持つ。`meta` は、JSON を解さない**オフボード観測者** (air を聞く WiFi sniffer、有線 tap / SPAN ミラー等) が、ブロードキャストフレームを空中・有線上で**固定バイトオフセットで特定**し、socket で受信した JSON レコードと **`hid_seq` で突合**するための、固定長 HEX 文字列である。

**配置規則 (必須)**: `meta` は JSON オブジェクトの**最初のキー**でなければならない。シリアライズは必ず `{"meta":"` (9 バイト) で始まるため、**HEX 値はペイロード先頭から常にバイトオフセット 9** に現れる。`meta` より前に可変長要素を置いてはならない (オフセットが固定でなくなる)。

**HEX 値のレイアウト (9 バイト = 18 桁、大文字 HEX、多バイト整数は big-endian)**:

| バイト | 内容 | 値 |
|---|---|---|
| 0-1 | magic | `0x52 0x4D` (ASCII `"RM"`、radio_metrics フレーム判定用) |
| 2 | meta フォーマット版 | `0x01` |
| 3 | メッセージ種別 | `0x01`=`rx_dl` / `0x02`=`tx_ul` / `0x03`=`hb` / `0x04`=`rx_dlb` (rx_dl バッチ、§3.1.1) |
| 4 | `robot_id` | 0-15 |
| 5-8 | `hid_seq` | uint32 big-endian |

オフボード観測者は「ペイロード offset 9 から 18 HEX を読む → デコード → magic が `RM` か確認 → `(robot_id, hid_seq)` を取得」だけで、JSON パース無しに任意の観測点 (air / wire / host-socket) で同一フレームを同定・突合できる。`hid_seq` は**全メッセージ種別で共有する単一の単調増加カウンタ** (1 フレーム送出ごとに +1、起動時 0) であり、これにより各ブロードキャストフレームが大域的に一意となる = join key として機能する。

> **設計原則 — 送信時刻は `meta` (および payload) に入れない**: `meta` には**送信前に確定している値 (カウンタ・ID) のみ**を入れる。フレームの実際の **on-air 送出時刻はシリアライズ時点では未知**であり、payload に埋めることは原理的にできない。送出時刻は**オフボードの air sniffer が観測する** (本チャネルを production データから分離した目的そのもの)。本仕様の `t_*_tsf_us` 群は「HID が送信前/受信時に software で読んだ近似アンカー」であって on-air 瞬間ではない。

### 3.1 `rx_dl`: 下りコマンドフレーム受信時

HID が port `40000 + robot_id` (通常コマンド) で UDP パケットを受信したとき、即座に 1 つの `rx_dl` を broadcast する。

`rx_dl` は **下り受信のアンカー (`t_rx_*`)** に加え、**自身を上り broadcast する直前の送信アンカー (`t_tx_*`、v2.1.0 追加)** を持つ。これにより 1 フレームで「下り OWD (`corr_unix_time`/`t_rx_tsf`)」と「上り OWD (`t_tx_tsf` を起点に off-board の air/wire 観測と突合、§4.3)」の**両方向**を計測できる。下りは常時 (AI 100Hz) 流れるため、上り計測も下りレートに追従した高密度サンプルが得られる。

```json
{
  "meta": "524D0101000000473A",
  "type": "rx_dl",
  "hid_seq": 18234,
  "cycle_count": 1238291,
  "t_rx_tsf_us": 4567890123,
  "t_rx_esp_timer_us": 1234567,
  "t_tx_tsf_us": 4567890201,
  "t_tx_esp_timer_us": 1234645,
  "frame_size": 64,
  "rssi": -42,
  "corr_unix_time": 1748037600.123456
}
```

| フィールド | 型 | 必須 | 説明 |
|---|---|:---:|---|
| `meta` | string(hex) | ✓ | **先頭キー必須**。off-board 観測用 join header (§3.0)。`524D` + ver + type(`01`) + robot_id + hid_seq(BE) |
| `type` | string | ✓ | 固定値 `"rx_dl"` |
| `hid_seq` | uint32 | ✓ | 全種別共有の単調増加カウンタ (起動時 0、1 フレームごと +1)。off-board join key (§3.0)。`meta` の値と一致すること |
| `cycle_count` | uint32 | ✓ | **cycle 単位の損失/到達分析キー**。受信ペイロード offset 51-53 の cycle_count (24bit LE、`downlink_command.md` 参照)。AI が全ロボットへ同時送出する周期カウンタ (0-16,777,215、wrap-around。100 Hz でも約 1.9 日まで wrap しない) |
| `t_rx_tsf_us` | uint64 | ✓ | **下り受信時刻**。HID の WiFi TSF (μs)、`esp_wifi_get_tsf_time()` 由来 |
| `t_rx_esp_timer_us` | uint64 | ✓ | 同じく下り受信時の `esp_timer_get_time()` 値 |
| `t_tx_tsf_us` | uint64 | ✓ | **v2.1.0 追加**。**この rx_dl フレーム自身を broadcast する直前**に読んだ HID の WiFi TSF (μs)。上り OWD の **`HID→air` leg** (channel access + 内部 TX queue) を求める送信アンカー (§4.3)。下り受信時刻 `t_rx_tsf_us` とは別物 (両者の差は HID の受信→上り発行処理時間)。TSF 未確定 (associate 前) なら本フレーム発行を見送る (§5) |
| `t_tx_esp_timer_us` | uint64 | ✓ | **v2.1.0 追加**。同じく送信直前の `esp_timer_get_time()` 値 |
| `frame_size` | uint16 | ✓ | 受信した下り UDP ペイロード長 (bytes) |
| `rssi` | int8 | 任意 | 受信フレームの RSSI (取得できる場合のみ) |
| `corr_unix_time` | float64 | ✓ | **下り OWD 計測の相関キー**。受信ペイロード offset 38-45 の unix time (double LE) をそのままコピー (`downlink_command.md` AI 指令用 packet 既存フィールド) |

> robot ID は本メッセージの送信元 port (`52000 + robot_id`) および受信ペイロード offset 02 から判るため、`rx_dl` には含めない。
>
> **`cycle_count` と `corr_unix_time` の使い分け**: `corr_unix_time` は **per-packet の OWD** (送信時刻との差) を測る相関キー。`cycle_count` は **AI 送出周期 (全ロボット同時バースト) 単位**の到達/損失分析キー。1 cycle 内の全ロボット宛パケットは同じ `cycle_count` を持つため、「ある cycle で何台に届いたか」を集計できる。旧 `aipc_seq` (offset 52-55 の uint32) は本フィールドに統合され廃止。

### 3.1.1 `rx_dlb`: `rx_dl` のバッチ送信 (v2.1.0、帯域削減)

`rx_dl` を下り受信毎 (60-100Hz × N台) に broadcast すると、broadcast は basic rate (典型 6Mbps)
固定・無集約でエアタイムを著しく消費し、**上り計測トラフィックが下り (計測対象) の遅延・容量を
悪化させる自己干渉**になる (実測: 上り除去で下り容量 ~4倍・下り OWD p99 ~10倍改善)。これを避けるため、
複数の `rx_dl` レコードを **1 つの UDP フレームにまとめて (compact 配列) broadcast** する `rx_dlb` を定義する。

> **per-frame `rx_dl` (type `0x01`) との関係**: `rx_dlb` は `rx_dl` の送出を間引かず**まとめるだけ**で、
> 下り計測情報 (`cycle_count`/`t_rx_tsf`/`rssi`) は per-record で完全保持する。ファームは
> `0x01` per-frame と `0x04` batch を**ビルドフラグで切替**できる (A/B 比較・段階移行用)。

```json
{"meta":"524D010401000011A9","type":"rx_dlb","bseq":4521,"rxc":271044,
 "tx":138207010679,"base":138207000000,
 "recs":[[13222708,9826,-24],[13222709,26596,-24]]}
```

| フィールド | 型 | 必須 | 説明 |
|---|---|:---:|---|
| `meta` | string(hex) | ✓ | **先頭キー必須** (§3.0)。type バイト = `04`。`hid_seq` は**この batch フレームの seq**。off-board 観測の air↔socket join は **batch 単位** |
| `type` | string | ✓ | 固定値 `"rx_dlb"` |
| `bseq` | uint32 | ✓ | **batch 連番** (起動時 0、batch 毎 +1)。受信側はこの連番の抜けで **batch (UDP) 損失**を検出し、当該区間を下り損失計算から除外する (report 損失と真の下り損失の分離) |
| `rxc` | uint32 | ✓ | **累積 `rx_dl` 受信数** (起動時 0、下り受信毎 +1)。`bseq` 損失検出の冗長チェック・受信総数の復元用 |
| `tx` | uint64 | ✓ | この batch を broadcast する直前の HID WiFi TSF (μs)。上り OWD の **`HID→air` leg** のアンカー (§4.3)。**batch 単位**なので上り標本は flush レート (~2Hz/台) に間引かれる |
| `base` | uint64 | ✓ | batch 基準 TSF (μs)。`recs` の `t_rx_tsf` を delta 圧縮する基準 |
| `recs` | array | ✓ | 下り per-frame レコード配列。各要素 `[cycle_count, (t_rx_tsf − base), rssi]` (順序固定): `cycle_count`=uint32 (損失/相関キー、§3.1)、`t_rx_tsf−base`=int32 μs (下り受信 TSF の base からの差)、`rssi`=int8 dBm |

**`corr_unix_time` は持たない**: `rx_dlb` は TSF 基準の OWD (sniffer/wire の cycle_count 突合 + TSF↔unix bridge) を
前提とし、`corr_unix_time` (host clock 相関) は不要。必要な場合は有線観測 (SPAN) の `downlink_command` payload
offset 38-45 から復元できる。`dl_seq`/`t_*_esp_timer_us`/`frame_size` も冗長なため batch では省略する。

**フレームサイズと flush 条件**:
- **1 UDP = 1 batch、UDP payload ≤ 1400 byte** (IP フラグメント回避マージン)。フラグメントは broadcast 無再送下で
  1 個ロス時に batch 全レコード喪失 + frame overhead 復活を招くため**禁止**。
- compact record は ~21 byte/件 (`[16777215,50000,-24]` 級)、header ~130 byte → **K ≈ 50 件/frame** が上限の目安。
- **flush = `recs 件数 ≥ K` または `経過 ≥ 0.5s` の早い方**。常に ≤1MTU かつ最大 0.5s で送出 (report 鮮度保証)。
  60Hz では 0.5s が先に効き ~30 件/batch、100Hz 以上では K (満載) が先。

**計測影響**:
- 下り (OWD/損失/RSSI) は **per-record で完全保持**。`t_rx_tsf` は受信時刻なので batch 遅延 (≤0.5s) は
  **計測 OWD に非影響** (リアルタイム監視の鮮度のみ ≤0.5s 遅延)。
- 上り `HID→air` OWD は **batch 単位** (~2Hz/台) に間引かれる。統計量 (mean/var/max) には十分。
- **batch (UDP) 損失 ≠ 下り損失**: batch を 1 個落とすと ~K 件のレコードが一度に欠落する。受信側は `bseq` の
  連番抜けで batch 損失を検出し、その区間を下り損失から除外すること (怠ると下り損失が過大計上される)。

### 3.2 `tx_ul`: 上りフレーム送信時 (任意)

> **v2.1.0 で任意 (optional) に変更**。上り OWD の計測自体は `rx_dl` (v2.1.0 で送信アンカー `t_tx_tsf_us` を獲得) と `hb` (`t_now_tsf_us`) が自身の送信アンカーを持つため、これらの off-board 突合 (§4.3) で完結する。`tx_ul` は **production 上り (`50000`/`51000`、`meta` を埋められない CU 起源/透過チャネル) そのものの OWD を測りたい場合の専用プロキシ**としてのみ必要。Challenge の OWD 報告は `rx_dl`/`hb` 経由で足りるため、`tx_ul` 実装は必須ではない (CU 上り送出パスへのフックが不要になり HID ファームが簡素化する)。実装する場合の定義は以下のとおり。

HID が port `50000+id` (CU 起源テレメトリ) または port `51000+id` (HID 起源 CAN 診断) で UDP パケットを broadcast 送信したとき、その**直前** (もしくは送信成功確認後) に 1 つの `tx_ul` を broadcast する。

```json
{
  "meta": "524D0201000000473B",
  "type": "tx_ul",
  "hid_seq": 18235,
  "tx_port": 50000,
  "t_tx_tsf_us": 4567890456,
  "t_tx_esp_timer_us": 1234890,
  "frame_size": 1234
}
```

| フィールド | 型 | 必須 | 説明 |
|---|---|:---:|---|
| `meta` | string(hex) | ✓ | **先頭キー必須**。§3.0。type バイト = `02` |
| `type` | string | ✓ | 固定値 `"tx_ul"` |
| `hid_seq` | uint32 | ✓ | 全種別共有の単調増加カウンタ (§3.0)。off-board join key。`meta` の値と一致すること |
| `tx_port` | uint16 | ✓ | 送信ポート (`50000+id` または `51000+id`) |
| `t_tx_tsf_us` | uint64 | ✓ | HID の WiFi TSF、送信直前/送信成功時のいずれか (実装で固定) |
| `t_tx_esp_timer_us` | uint64 | ✓ | 同じく `esp_timer_get_time()` |
| `frame_size` | uint16 | ✓ | 送信した UDP ペイロード長 |

### 3.3 `hb`: ハートビート (任意)

長時間下りも上りも無い場合の生存確認用。1 秒周期程度を想定。

```json
{
  "meta": "524D0301000000473C",
  "type": "hb",
  "hid_seq": 18236,
  "t_now_tsf_us": 4567891000,
  "t_now_esp_timer_us": 1235021,
  "last_beacon_age_us": 78293,
  "missed_beacons": 0,
  "associated_bssid": "76:7F:F0:3B:74:26",
  "channel": 44
}
```

| フィールド | 型 | 必須 | 説明 |
|---|---|:---:|---|
| `meta` | string(hex) | ✓ | **先頭キー必須**。§3.0。type バイト = `03` |
| `type` | string | ✓ | 固定値 `"hb"` |
| `hid_seq` | uint32 | ✓ | 全種別共有の単調増加カウンタ (§3.0)。off-board join key |
| `t_now_*` | uint64 | ✓ | 発行時刻 |
| `last_beacon_age_us` | uint32 | ○ | 直近ビーコン受信からの経過 (同期品質モニタ用) |
| `missed_beacons` | uint16 | ○ | 直近 N=64 ビーコンの取り逃し数 |
| `associated_bssid` | string | ○ | 現在 associate 中の AP の BSSID |
| `channel` | uint8 | ○ | 現在のチャネル |

## 4. OWD 計測時の相関規則

### 4.1 下り OWD (Host → HID)

```
1. Host PC が port 40000+id に UDP unicast 送信
   送信ペイロードの offset 38-45 に unix_time (double LE) を埋める
   (downlink_command.md 既存の "unix time" フィールドをそのまま使う。
    Host PC 内部での時刻精度は問わない、一意キーとしてのみ機能すれば十分)

2. 計測機が tap/SPAN/inline 等の手段で同フレームを Ethernet 上で観測し、
   到着時刻 t_host_side_tx を記録 (payload offset 38-45 をパースして
   corr_unix_time を取得)

3. HID が受信、即座に rx_dl JSON を 52000+id へ broadcast
   corr_unix_time = 受信ペイロードの offset 38-45 を抜粋

4. 計測機が同 subnet で 52000+id を listen して rx_dl を受け、
   corr_unix_time でステップ 2 のローカル記録と照合

5. owd_dl = (t_hid_rx_tsf_us → 計測機 realtime 投影) − t_host_side_tx
```

### 4.2 上り OWD (HID → Host) — 任意 (`tx_ul` プロキシ法)

> **推奨は §4.3 の `meta`/`hid_seq` 多点突合**。`rx_dl` (v2.1.0 で送信アンカー `t_tx_tsf_us` を持つ) や `hb` (`t_now_tsf_us`) はフレーム自身が送信アンカーを持つため、time-window ヒューリスティック無しで `HID→air→wire` を per-frame 分解できる。本 §4.2 は **production 上り (`50000`/`51000`) そのものの OWD を測りたい場合に限り**、`tx_ul` を時間近接プロキシとして使う任意手法。

```
1. CU が UART 経由で uplink_telemetry JSON を HID に送る (port 50000+id 用)
   または HID 自身が hid_bridge JSON を生成 (port 51000+id 用)

2. HID は実際の UDP 送信直前に tx_ul JSON を port 52000+id へ broadcast
   (このとき本来の uplink_telemetry / hid_bridge JSON も同時に出る)

3. 計測機は両 broadcast を観測:
   - production uplink (50000+id) を host 側で受信 → t_host_side_rx 記録
   - tx_ul (52000+id) を同 subnet で受信 → t_hid_tx_tsf を取得

4. 同 HID (= 同 src_ip) の production uplink と tx_ul を **時間近接ペア**で対応
   閾値: |t_host_side_rx(production) − t_host_side_rx(tx_ul)| < 5 ms
   production uplink と tx_ul は HID 内部でほぼ同時に発行されるので、
   両者の host 側到着時刻差は WiFi 上の到着順 + kernel queue 差のみ、< 1 ms 想定

5. owd_ul = t_host_side_rx − (t_hid_tx_tsf_us → 計測機 realtime 投影)
```

#### 4.2.1 時間近接ペアの曖昧性

複数ロボットが同時に uplink を出す状況では、各 HID が独立 broadcast するため、計測機は src_ip (IP ヘッダの送信元) で発行ホストを識別すれば、HID 毎にペアリング可能。曖昧性は同一 HID の連続 uplink (バックトゥバック) のみだが、HID の uplink レートが 100 Hz 未満なら 5 ms 窓で十分一意。

### 4.3 off-board 多点観測による区間分解 (`meta` / `hid_seq` 突合)

§4.2 の「時間近接ペア (5 ms 窓)」は host 側到着のみを用いる発見的手法だが、`meta` (§3.0) を使うと**時間窓ヒューリスティック不要の per-frame 多点突合**ができる。計測機が air (WiFi monitor sniffer) と有線 (tap / SPAN ミラー) の双方に観測点を持つ場合、同一 radio_metrics フレームを各点で `hid_seq` により同定し、無線区間を分解できる。

```
HID emit (meta に hid_seq) ──ToDS──▶ AP ──┬─ 有線へ転送       → 有線 tap が観測 = t_wire
   software anchor t_*_tsf_us            └─ (AP の FromDS 再送は別フレーム)
  air sniffer が HID の ToDS 原送信を観測 = t_air   ← payload offset 9 の meta から hid_seq 取得
```

- 全観測点 (air / wire / host-socket) で **同一 `hid_seq`** によりフレームを同定 → 5 ms 窓不要
- **`rx_dl` を主とした上り区間分解** (v2.1.0、`t_tx_tsf_us` を送信アンカーに使用):
  - `HID→air` = `t_air (sniffer 観測) − t_tx_tsf_us (software anchor)` = HID 内部送信処理 (channel access + queue→PHY)
  - `air→wire` = `t_wire (有線観測) − t_air` = AP 受信処理 + 有線転送
  - → `rx_dl` 1 種で **下り OWD と上り OWD の両方向**を同一フレームから取得できる (上りは下り 100Hz に追従した高密度サンプル)
- **air 送出時刻は観測値**であって payload に埋めた値ではない (§3.0 設計原則)。`t_tx_tsf_us` はあくまで送信前 software アンカーで、`HID→air` leg はこのアンカーと air 観測値の差として求まる
- `hb` (`t_now_tsf_us`) も同様に自己アンカーを持ち、下り/上りが無いアイドル時の上り計測に使える。`tx_ul` (任意) を実装した場合は production 上りのプロキシとして同様に分解可能

> air sniffer は STA→AP の **ToDS 原送信** (802.11 `addr2` = HID の MAC) を捕捉する。AP の **FromDS 再送** (`addr2` = BSSID) は別フレーム・別タイミング (AP egress) なので「HID が空中に出した瞬間」を表さない点に注意。
>
> `meta` を持たない production チャネル (`40000`/`50000`/`51000`) のフレームは固定オフセット join key を持たないため、本手法の対象外 (それらは payload に計測情報を混ぜない設計)。production 上りの区間を測りたい場合は、同時刻に送出される `tx_ul` (本チャネル、`meta` 付き) を**プロキシ**として観測する。

## 5. 実装上の注意

- **`esp_wifi_get_tsf_time()` を ISR で呼ばないこと** (典型 0〜120μs、実機で数百 μs かかる事例あり)。HID の WiFi RX コールバックでは `esp_timer_get_time()` だけを保存し、別タスクで TSF を取得して線形回帰換算する方が確実。線形回帰較正は esp_timer↔TSF の**中点** (`(t_before + t_after) / 2`) を使うこと (`t_before` 単独だと残差 p99 が 3.5 倍に悪化する実機例あり)。
- 52000+id channel の送信失敗は **fire-and-forget**。production uplink の遅延要因にならないこと。
- HID 起動直後 (WiFi associate 完了前) は TSF が 0。`tx_ul` / `rx_dl` 発行前にチェックすべし。
- 高頻度送出 (12 ロボット × 上下 60Hz 想定で 1440 msg/s) で WiFi 自己干渉のリスクあり。本チャネルは broadcast のため再送無し、burst 時に sniffer が捕捉漏れする可能性 → 統計的に「捕捉率」を併記する運用を推奨。

## 6. 互換性

- 既存の 40000 / 40999 / 41000+id / 50000+id / 51000+id ポートには一切手を加えない
- 既存ファームは本仕様を実装せずとも従来通り動作 (52000+id を listen するロガーが居なければそれだけ)
- 計測ロガー側は本仕様未対応の HID 相手では OWD 報告できないので、HID ファーム側で v2.0.0 対応バージョンが必要
- `meta` (§3.0) は追加フィールド。JSON を素直にパースする listener は未知キーとして無視するだけで従来通り動作する (後方互換)。ただし off-board 多点観測 (§4.3) を行う観測者は `meta` が**先頭キー・固定長 HEX** であることに依存するため、emitter はこの配置を厳守すること。
- **v2.1.0**: `rx_dl` への `t_tx_tsf_us` / `t_tx_esp_timer_us` 追加は後方互換 (MINOR)。旧 listener は未知キーとして無視、旧 HID (v2.0.0) は当該フィールドを emit しないので上り leg 分解だけができない (下り OWD は従来どおり)。`tx_ul` の任意化も既存 emitter は引き続き emit してよく互換 (listener は `tx_ul` 不在を前提に `rx_dl`/`hb` で上りを測る)。
