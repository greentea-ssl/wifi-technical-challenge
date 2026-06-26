# radio_metrics — HID 起源の Radio Metrics チャネル (Port 52000 + robot_id)

> **本書は `robot_comm_spec` v2.1.0 以降への追加提案ドラフト**。上流リポジトリにマージされたらタグ付けされ、各ファームウェアリポジトリは submodule を bump して取り込む想定。

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

### 3.1 `rx_dl`: 下りコマンドフレーム受信時

HID が port `40000 + robot_id` (通常コマンド) で UDP パケットを受信したとき、即座に 1 つの `rx_dl` を broadcast する。

```json
{
  "type": "rx_dl",
  "hid_seq": 18234,
  "t_rx_tsf_us": 4567890123,
  "t_rx_esp_timer_us": 1234567,
  "frame_size": 64,
  "rssi": -42,
  "corr_unix_time": 1748037600.123456
}
```

| フィールド | 型 | 必須 | 説明 |
|---|---|:---:|---|
| `type` | string | ✓ | 固定値 `"rx_dl"` |
| `hid_seq` | uint32 | ✓ | HID 採番、起動時 0 から単調増加 |
| `t_rx_tsf_us` | uint64 | ✓ | HID の WiFi TSF (μs)、`esp_wifi_get_tsf_time()` 由来 |
| `t_rx_esp_timer_us` | uint64 | ✓ | HID の `esp_timer_get_time()` 値 |
| `frame_size` | uint16 | ✓ | UDP ペイロード長 (bytes) |
| `rssi` | int8 | 任意 | 受信フレームの RSSI (取得できる場合のみ) |
| `corr_unix_time` | float64 | ✓ | **下り OWD 計測の相関キー**。受信ペイロード offset 38-45 の unix time (double LE) をそのままコピー (`downlink_command.md` AI 指令用 packet 既存フィールド) |

### 3.2 `tx_ul`: 上りフレーム送信時

HID が port `50000+id` (CU 起源テレメトリ) または port `51000+id` (HID 起源 CAN 診断) で UDP パケットを broadcast 送信したとき、その**直前** (もしくは送信成功確認後) に 1 つの `tx_ul` を broadcast する。

```json
{
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
| `type` | string | ✓ | 固定値 `"tx_ul"` |
| `hid_seq` | uint32 | ✓ | HID 採番、`rx_dl` と同じカウンタを共有可 (発行順序の解釈用) |
| `tx_port` | uint16 | ✓ | 送信ポート (`50000+id` または `51000+id`) |
| `t_tx_tsf_us` | uint64 | ✓ | HID の WiFi TSF、送信直前/送信成功時のいずれか (実装で固定) |
| `t_tx_esp_timer_us` | uint64 | ✓ | 同じく `esp_timer_get_time()` |
| `frame_size` | uint16 | ✓ | 送信した UDP ペイロード長 |

### 3.3 `hb`: ハートビート (任意)

長時間下りも上りも無い場合の生存確認用。1 秒周期程度を想定。

```json
{
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
| `type` | string | ✓ | 固定値 `"hb"` |
| `hid_seq` | uint32 | ✓ | 同上 |
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

### 4.2 上り OWD (HID → Host)

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

## 5. 実装上の注意

- **`esp_wifi_get_tsf_time()` を ISR で呼ばないこと** (典型 0〜120μs、実機で数百 μs かかる事例あり)。HID の WiFi RX コールバックでは `esp_timer_get_time()` だけを保存し、別タスクで TSF を取得して線形回帰換算する方が確実。線形回帰較正は esp_timer↔TSF の**中点** (`(t_before + t_after) / 2`) を使うこと (`t_before` 単独だと残差 p99 が 3.5 倍に悪化する実機例あり)。
- 52000+id channel の送信失敗は **fire-and-forget**。production uplink の遅延要因にならないこと。
- HID 起動直後 (WiFi associate 完了前) は TSF が 0。`tx_ul` / `rx_dl` 発行前にチェックすべし。
- 高頻度送出 (12 ロボット × 上下 60Hz 想定で 1440 msg/s) で WiFi 自己干渉のリスクあり。本チャネルは broadcast のため再送無し、burst 時に sniffer が捕捉漏れする可能性 → 統計的に「捕捉率」を併記する運用を推奨。

## 6. 互換性

- 既存の 40000 / 40999 / 41000+id / 50000+id / 51000+id ポートには一切手を加えない
- 既存ファームは本提案を実装せずとも従来通り動作 (52000+id を listen するロガーが居なければそれだけ)
- 計測ロガー側は本仕様未対応 HID 相手では OWD 報告できないので、HID ファーム側で v2.x.0 対応バージョンが必要
