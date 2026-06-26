# HID 汎用 CAN ブリッジ (PC ↔ HID UDP/JSON)

HID が CU を介さず PC と直接 UDP/JSON を交換し、CAN バスへの送出 / CAN からの傍受転送を行う橋渡しチャネル。**v2.0.0 で導入**。パラメータチューニング、デバッグ、ログ収集など、診断・運用系の用途を想定する。制御ループに必須のフレームは引き続き 40000/50000 系 (`[downlink_command.md](./downlink_command.md)` / `[uplink_telemetry.md](./uplink_telemetry.md)`) を使う。

設計上の位置付け:

- 40000/50000 系 (CU 行き/CU 発): HID は **透過フォワーダ** (UART ↔ UDP の中継のみ)。アプリケーション層プロトコルは CU が解釈する。
- **本仕様 (41000/51000 系)**: HID 自身が **アクティブエンドポイント** として JSON を解釈し、CAN バスへの送出 / CAN からの抽出転送を行う。CU は関与しない。


## ポートとアドレッシング

| 方向 | ポート | 送信方式 | 用途 |
|---|---|---|---|
| Downlink (PC → HID) | `41000 + robot_id` | **unicast** (mDNS で `robot<robot_id>.local` を解決) | CAN フレーム送出指示 / HID 設定変更 |
| Uplink (HID → PC) | `51000 + robot_id` | broadcast | CAN 上のテレメトリ (種別 `11111`) を JSON で転送 |

エンコーディングは両方向とも **UTF-8 JSON**。各 JSON オブジェクトは改行 (`\n`) 終端。**Downlink は 1 UDP パケットにつき 1 オブジェクト**。**Uplink は負荷低減のため 1 パケットに複数オブジェクトをまとめる場合がある** (NDJSON / JSON Lines、本書「Uplink」§「バッチ送信」を参照)。

---

## CAN 上でのテレメトリの識別と severity 規則

本セクションは [CAN_HS.md](./CAN_HS.md) §1 / [CAN_LS.md](./CAN_LS.md) §1 のテレメトリ種別に関する**正典定義**である。両バスとも同一規則を適用する。

CAN フレームのうち、**メッセージ種別 (CAN ID Bit 10-6) = `11111`** のものは「テレメトリ」として扱う。本メッセージ種別では:

- **送信元デバイス種別 (Bit 5-3)**: バスごとの通常割り当てに従う (HS: メイン/モータ/キッカー, LS: メイン/HID/WiFi/電源)
- **サブID (Bit 2-0)**: 通常のデバイスサブIDではなく **severity** として使用

### severity 一覧

| 値 | 名称 | 用途の目安 |
|---|---|---|
| `0` | `FATAL` | 復旧不能 / 即時停止が必要 |
| `1` | `ERROR` | 機能不全。継続運転は危険 |
| `2` | `WARN` | 想定外だが運転は継続可能 |
| `3` | `INFO` | 通常運用ログ |
| `4` | `DEBUG` | 開発時の詳細トレース |
| `5` | `TRACE` | 最詳細ログ (高頻度) |
| `6`-`7` | (予約) | v2.0.0 では未割当 |

### 例 (CAN ID パターン)

| CAN ID | バス | 送信元 | severity | 例 |
|---|---|---|---|---|
| `0x7C0` | HS / LS | メイン基板 | FATAL | メイン基板の致命的エラー |
| `0x7C3` | HS / LS | メイン基板 | INFO | メイン基板の情報ログ |
| `0x7C9` | HS | モータドライバ | ERROR | モータエラー (どのモータかはペイロードで識別) |
| `0x7DA` | LS | 電源基板 | WARN | 電源基板の警告 |

ペイロード (CAN-Classic は 0-8 byte / CAN-FD は最大 64 byte) の解釈は **完全にデバイス側仕様に委ねる**。本仕様では転送フレーミングのみを規定し、共通フィールド (フォーマットバージョン / シーケンス番号 / タイムスタンプ等) は強制しない。PC 側デコーダは `src_device_type` + `bus` の組み合わせから適切なデコーダを選ぶ想定。

---

## bridge の転送ルール — `11110` (応答) / `11111` (テレメトリ)  *(v2.1.0)*

HID bridge は通常、**メッセージ種別の上位 2 ビット (CAN ID Bit 10-9) が `11` のフレーム** (= 種別 `11110` / `11111`、CAN ID `0x780`–`0x7FF`) のみを PC へ転送する。bridge は **この固定ビットルールだけ**を持ち、個別の CAN ID やプロトコル(要求⇄応答の対応)は知らない (純粋な土管)。

| 種別 | クラス | 転送条件 |
|---|---|---|
| `11111` | テレメトリ | `severity ≤ ログレベル` のときのみ転送 |
| `11110` | **応答 / 診断 (ANSWER)** | **常時転送** (ログレベル非依存) |
| (全種別) | **raw (promiscuous)** | **ログレベル `F` (15) のときのみ**、種別に関わらず受信した**全フレーム**を転送 |

- ログレベルは転送のしきい値で、**`level 0` が床**。すなわち **`level 0` = FATAL テレメトリ (`severity 0`) ＋ ANSWER (`11110`)** が最小集合。`level` を上げると ERROR → … → TRACE のテレメトリが順に加わる (ANSWER は全レベルで常時転送される)。
- **ログレベル `F` (15) = promiscuous (raw 全転送)**: 上記のビット制限を解除し、bridge が受信した**全 CAN フレーム**を転送する (旧 Web UI `/can` raw ログの代替)。**デバッグ専用** — バス負荷が高いと UDP/WiFi を飽和させるため通常運用では使わない。本モードでも `11111`/`11110` は本来の `kind` (telemetry/answer) で送られ、それ以外の種別が `kind:"raw"` となる。
- **応答口の設計**: PC が観測したい応答は種別 `11110` の CAN ID で返す ([CAN_LS.md](./CAN_LS.md) §「bridge 透過クラス」)。bridge は中身を解さないため、要求口ごとに「要求 ID → 応答 ID (`11110`)」を仕様で定義し、PC は応答 ID ＋ ペイロードで自身の要求と突合する。
- パラメータ口の追加等で **bridge のソフトは変更不要** (新しい応答も `11110` を使うだけ)。
- 複数 PC が同時に listen していても、転送は broadcast (51000) で全員に届き、各 PC が自分の要求と突合するため、bridge 側のクライアント別状態は不要。

---

## Downlink: PC → HID (Port 41000 + robot_id)

リクエストは 1 UDP パケットにつき 1 JSON オブジェクト。共通スキーマ:

```json
{ "type": "<command type>", ... }
```

`type` ごとに追加フィールドが決まる。HID は未知の `type` を**サイレントに破棄**する (将来拡張のため)。

### type = `"can"` : CAN フレーム送信

HID に任意の CAN フレームを送出させる。

```json
{
  "type": "can",
  "bus": 1,
  "canid": "0x088",
  "payload": "02"
}
```
*(例: 低速バスの HID 直接コマンド (`0x088`) でロボットID/状態取得 (subcommand `0x02`) を発行)*

| フィールド | 型 | 必須 | 説明 |
|---|---|:--:|---|
| `type` | string | ✓ | 固定値 `"can"` |
| `bus` | int | ✓ | `0` = 高速バス (CAN-FD), `1` = 低速バス (CAN-Classic) |
| `canid` | int / hex string | ✓ | CAN ID。数値 (10進) でも `"0x..."` 形式の文字列でも可 |
| `payload` | hex string | ✓ | データバイト列を 16 進文字列で表記。空白区切り任意 (`"00 12 34"` も `"001234"` も可)。CAN-Classic は 0-8 byte、CAN-FD は最大 64 byte |
| `dlc` | int | optional | 明示する場合の DLC。省略時は `payload` のバイト数から算出 |

HID は受信した内容で CAN フレームを組み立て、指定バスに送出する。**v2.0.0 では fire-and-forget で確定**: HID は送出成功/失敗の同期応答を返さない。CAN バス上での結果 (応答フレームやエラー副作用) は uplink テレメトリ経路 (Port 51000+id) で観測する。

### type = `"set_log_level"` : テレメトリ転送のしきい値設定

HID 側のテレメトリ転送フィルタ (uplink port 51000+id) のしきい値を変更する。

```json
{ "type": "set_log_level", "level": 3 }
```

| フィールド | 型 | 必須 | 説明 |
|---|---|:--:|---|
| `level` | int | ✓ | `0`-`5` の severity しきい値 (`severity ≤ level` の `11111` テレメトリのみ転送。severity 定義は本書「CAN 上でのテレメトリの識別と severity 規則」§)。**`15` (`F`) = promiscuous**: 全 CAN フレームを `kind:"raw"` で転送 (本書「bridge の転送ルール」§、デバッグ専用)。`6`-`14` は予約。起動時の初期値は**ファーム実装が決定**し、本仕様では規定しない |

**揮発設定**: `set_log_level` で変更した値は HID メモリ上にのみ保持され、HID 再起動でファーム既定の起動時初期値に戻る。長期間それ以外で運用したい場合は PC 側から再送する運用とする。

### type = `"hid_status"` : HID 自身の状態取得 *(v2.1.0)*

HID (bridge エンドポイント) 自身の稼働状態を要求する。HID はこれを受けて状態を 1 つの JSON にまとめ、uplink (port 51000+id) に **1 回だけ broadcast 応答**する (本書「Uplink」§ の `type:"hid_status"` を参照)。Web UI の旧 `/api/status` をこのチャネルで置き換える。

```json
{ "type": "hid_status" }
```

追加フィールドは無い。**pull 方式** (要求があったときだけ応答) のため、定期 broadcast に比べ通信量を抑えられる。応答は uplink を listen している全 PC に届く (broadcast) が、robot_id ごとに HID は 1 台なので応答の対応付けは曖昧にならない。

### type = `"set_ssid"` : 接続先 WiFi AP の切替 *(v2.1.0)*

HID が接続する WiFi AP を指定の AP に切替える。通信解析用のオープン AP などへ一時的に移すための診断用。

```json
{ "type": "set_ssid", "ssid": "TEAM_SSID_OPEN", "password": "" }
```

| フィールド | 型 | 必須 | 説明 |
|---|---|:--:|---|
| `ssid` | string | ✓ | 接続先 SSID |
| `password` | string | optional | パスフレーズ。**省略または空文字でオープン AP** として接続 |

- 受信した HID は現在の AP を切断し、指定 AP への接続を試みる。
- **hidden SSID 対応**: SSID を明示した directed connection のため、ビーコンに SSID を出さない **hidden AP にも接続できる**。ファームの自動接続 (scan) は hidden を SSID 名で一致できないため、**hidden AP への接続は本 `set_ssid` を用いる**運用とする (例: `TEAM_SSID_OPEN`)。
- **揮発**: HID 再起動でファーム既定の AP 選択ポリシー (可視 AP の優先リスト) に戻る。
- 接続性確保の方針 (フォールバック等) は**ファーム実装に委ねる** (本仕様では規定しない)。SanRei_HID 実装は「猶予時間内に一度でも接続できれば以後その AP に固執 (sticky)、猶予内に一度も接続できなければ既定の優先リストへフォールバック」。
- **注意**: 切替により HID の所属ネットワークが変わるため、応答(`hid_status`/テレメトリ)は新ネットワーク側でのみ受信できる。指示元 PC も追従が必要。

### 将来追加予定の type (TBD)

将来の拡張余地として以下を検討中だが未定義:

- 任意のパラメータ設定 (`type: "param"` / `type: "set_param"`): CU パラメータの取得/設定 (旧仕様 0x090/0x0C0 相当)
- 汎用レスポンスチャネル: 任意 downlink に対する同期 ack/応答フォーマット (現状は CAN 上の応答 = 種別 `11110`、HID 自身の状態 = `hid_status` uplink として個別に定義済み)

---

## Uplink: HID → PC (Port 51000 + robot_id)

ロボット内部の各デバイス (メイン基板/モータドライバ/電源基板/HID 等) が CAN バスへ流す **テレメトリ (種別 `11111`)** および **応答/診断 (種別 `11110`)** のフレームを HID が傍受し、UTF-8 JSON にエンコードして PC へブロードキャスト転送する (転送条件は本書「bridge の転送ルール」§)。

### HID のフィルタとログレベル

HID は内部に「ログレベル」を保持し、**テレメトリ (`11111`)** は `severity ≤ ログレベル` のもののみ転送する (それ以外は破棄)。**応答/診断 (`11110`) はログレベルに関わらず常時転送**する (= `level 0` の床)。ログレベルは PC 側から `set_log_level` JSON (本書「Downlink」§) で動的に変更可能。

- 初期値 (起動時): **ファーム実装依存** — 本仕様では規定しない (起動モード等に応じてファームが決める。応答 `11110` はレベルに依らず常時転送)
- 永続化: **揮発** (HID 再起動でファーム既定の初期値に戻る)

### バッチ送信 (NDJSON) *(v2.1.0)*

uplink は負荷低減のため、複数の JSON オブジェクトを **1 UDP datagram にまとめて** 送る (特にログレベル `F` の raw 全転送時にパケット数を大幅削減)。

- 各オブジェクトは **改行 (`\n`) 終端** (NDJSON / JSON Lines)。1 datagram に 1 つ以上のオブジェクトが `\n` 区切りで並ぶ。
- HID は次のいずれかで datagram を送出 (flush) する: **(a) 累積サイズが MTU (約 1400 byte) を超える**、または **(b) 前回送信から 100 ms 経過**。
- **受信側は datagram を `\n` で分割し、各行を個別に JSON parse すること。** 低頻度時は 1 datagram = 1 オブジェクトになることも多い。
- 各オブジェクトのスキーマは下記のとおり (バッチ化は packing のみで、オブジェクト構造は不変)。

### JSON オブジェクト形式

各オブジェクトは 1 CAN フレーム (または `hid_status` 応答) を表す。`kind` で種別を区別する。

**テレメトリ (`11111`)**:
```json
{
  "type": "can",
  "kind": "telemetry",
  "bus": 1,
  "canid": "0x7DA",
  "src_device_type": 3,
  "severity": "WARN",
  "severity_level": 2,
  "payload": "01 02 03 04",
  "ts_ms": 12345678
}
```

**応答/診断 (`11110`)** — severity は持たない (サブID は通常エンコード):
```json
{
  "type": "can",
  "kind": "answer",
  "bus": 1,
  "canid": "0x788",
  "src_device_type": 1,
  "payload": "00 04 01 20",
  "ts_ms": 12345700
}
```

**raw (ログレベル `F` の全フレーム)** — `11111`/`11110` 以外の任意の種別。severity は持たない:
```json
{
  "type": "can",
  "kind": "raw",
  "bus": 1,
  "canid": "0x258",
  "src_device_type": 3,
  "payload": "01 00 00 00 00 00 00 01",
  "ts_ms": 12345720
}
```

| フィールド | 型 | 説明 |
|---|---|---|
| `type` | string | 固定値 `"can"` |
| `kind` | string | `"telemetry"` (種別 `11111`) / `"answer"` (種別 `11110`) / `"raw"` (ログレベル `F` 時の上記以外の全種別) (v2.1.0 で追加) |
| `bus` | int | `0` = 高速バス, `1` = 低速バス |
| `canid` | hex string | 受信フレームの CAN ID (`"0x7C0"` / `"0x788"` 等) |
| `src_device_type` | int | Bit 5-3 の値 (`0`-`7`)。CAN_HS / CAN_LS のバスごとに意味が異なる |
| `severity` | string | **telemetry のみ**。サブID から導出された syslog 風名称 (`"FATAL"`..`"TRACE"`)。`6`-`7` は予約 |
| `severity_level` | int | **telemetry のみ**。サブID の生値 (`0`-`7`) |
| `payload` | hex string | データバイト列 (例: `"01 02 03 04"`)。空文字列の場合あり |
| `ts_ms` | int | optional。HID 側の単調増加時刻 [ms]。受信タイミング分析用 |

### HID 状態 (`type: "hid_status"`) *(v2.1.0)*

downlink `{"type":"hid_status"}` に対する応答。HID 自身の稼働状態を 1 JSON で broadcast する。CAN フレームではないため `kind` / `severity` / `canid` / `bus` は持たない。Web UI (旧 `/api/status`) の置き換え。

```json
{
  "type": "hid_status",
  "robot_id": 1,
  "fw_version": "dev_v2.1.0",
  "op_mode": 1,
  "wifi": true,
  "emo_remote": false,
  "estop_local": false,
  "log_level": 5,
  "can_rx": 1234,
  "can_tx_ok": 567,
  "can_tx_fail": 2,
  "can_fail_streak": 0,
  "main_fw_received": true,
  "main_mode_echo": 1,
  "ota": false,
  "ts_ms": 12345678
}
```

| フィールド | 型 | 説明 |
|---|---|---|
| `type` | string | 固定値 `"hid_status"` |
| `robot_id` | int | HID が保持する robot ID |
| `fw_version` | string | HID ファームのバージョン文字列 (例 `"dev_v2.1.0"`、開発ビルドは `"LOCAL"`) |
| `op_mode` | int | 動作モード (`0` = NORMAL / `1` = MANUAL / `2` = DEBUG) |
| `wifi` | bool | WiFi 接続状態 |
| `emo_remote` | bool | リモート EMO (非常停止) 状態 |
| `estop_local` | bool | ローカル ESTOP (5-way 長押しトグル) 状態 |
| `log_level` | int | 現在の bridge ログレベル (`0`-`5`、または `15`=`F` raw) |
| `can_rx` / `can_tx_ok` / `can_tx_fail` | int | CAN 受信 / 送信成功 / 送信失敗の通算カウント |
| `can_fail_streak` | int | 連続送信失敗数 (回復制御の指標。`0` 以外で異常傾向) |
| `main_fw_received` | bool | メイン基板から FW バージョン応答 (`0x040`) を受信済みか |
| `main_mode_echo` | int | メイン基板がエコーバックした動作モード (`255` = 未受信) |
| `ota` | bool | OTA 更新が進行中か |
| `ts_ms` | int | HID 側の単調増加時刻 [ms] |

> フィールドはファーム実装が増減し得る。PC 側は未知フィールドを無視し、欠損フィールドにはデフォルトを充てること (前方/後方互換のため)。

### 将来拡張 (TBD)

- バッチ転送: 高頻度ログをまとめて 1 パケットに詰める形式
- 信頼性: 落ちた DEBUG/TRACE の件数を WARN レベルで通知する欠落通知
