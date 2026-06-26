# WioDisplay

WioTerminal用 I2C Slave ディスプレイコントローラ

## 概要

WioDisplayは、WioTerminalをI2C Slaveデバイスとして動作させ、ESP32C5 Masterからの表示コマンドを受信してTFT画面に表示します。また、ボタン入力を検知してMasterにイベントを送信します。

## 機能

- I2C Slave通信 (アドレス: 0x08)
- TFT液晶表示制御 (320x240)
- ボタン入力処理 (各ボタンを「短押し」「起動時押下」「loop 中の長押し (1 秒)」の 3 種で扱う)
  - 短押しは Wio 側で完結する処理 (KEY_A/B = setId 増減、KEY_C = ID 確定通知)
  - 起動時押下と長押しは Wio 側では HW 状態だけを bitmap で報告し、機能の意味付けは Master (C5) が行う
  - これにより新機能の追加時は C5 のみ更新で済み、古い Wio fw でも C5 が新解釈を実装すれば動く (forward compat)

| ボタン | 入力 | Wio の動作 |
|--------|------|----------|
| KEY_A | 短押し | setId-- (ローカル) |
| KEY_B | 短押し | setId++ (ローカル) |
| KEY_C | 短押し | `CMD_ID_CONFIRM` (0x81) を Master に送信 |
| KEY_C | 起動時押下 | `CMD_BOOT_BUTTONS` (0x86) で BTN_KEY_C ビット (0x04) を Master に通知 → C5 が PowerBoard OC/OD オーバーライドを発行 |
| 5WAY Press | 起動時押下 | `CMD_BOOT_BUTTONS` で BTN_5WAY_PRESS ビット (0x08) を通知 → C5 が MANUAL モード投入 |
| 5WAY Press | loop 中 1 秒長押し | `CMD_BTN_LONGPRESS` (0x87) で BTN_5WAY_PRESS ビットを通知 → C5 がローカル停止トグル |
| KEY_A/B/C | loop 中 1 秒長押し | `CMD_BTN_LONGPRESS` で対応ビットを通知 (現状 C5 側で未割当 / 将来拡張用) |
- 自動復旧機能（リセット時にMasterへ再送信要求）
- PowerBoard状態表示（テキスト形式）
- mDNS (`wio{robotId}.local`) — robotId 確定後に自動登録、変更時に再登録
- WiFi (RTL8720DN / rpcWiFi) 経由のOTAアップデート
  - 待受: UDP/41000 (コマンド `0x30` + ファームウェアURL)
  - フラッシュ書き込み: ArduinoOTA (JAndrassyフォーク) の `InternalStorage`
  - **アプリサイズ制約: ~252KB 以内** (SAMD51 内蔵フラッシュ 512KB の上半分にステージングするため)

## ハードウェア要件

- WioTerminal (Seeeduino SAMD51)
- I2C接続: PIN4 (SDA), PIN5 (SCL)
- 外部プルアップ抵抗: 4.7kΩ (SDA/SCL各ライン)

## 開発環境

- Arduino IDE 2.x
- ボードパッケージ: `Seeeduino:samd`
- ボード選択: `Seeeduino Wio Terminal`

## ビルド方法

### Arduino IDE

1. `WioDisplay.ino` を開く
2. ボード: `Seeeduino Wio Terminal` を選択
3. 書き込み実行

### arduino-cli

```bash
arduino-cli compile -b Seeeduino:samd:seeed_wio_terminal ./WioDisplay.ino
arduino-cli upload -p COMx -b Seeeduino:samd:seeed_wio_terminal ./WioDisplay.ino
```

## ファイル構成

```
WioDisplay/
├── WioDisplay.ino    # メインプログラム
├── config.h          # 設定ファイル
└── Free_Fonts.h      # フォント定義
```

## I2Cコマンド一覧

### Master → Slave

| CMD | 名称 | 説明 |
|:---:|------|------|
| 0x01 | UPDATE_STATUS | WiFi状態・IP・Port更新 |
| 0x02 | SET_ROBOT_ID | 確定済みRobot ID設定 |
| 0x03 | UPDATE_EMO | EMO状態更新 |
| 0x05 | FULL_REFRESH | 全データ一括更新 |
| 0x06 | UPDATE_NETWORK | SSID・バージョン送信 |

### Slave → Master

| CMD | 名称 | LEN | 説明 |
|:---:|------|:---:|------|
| 0x81 | ID_CONFIRM | 1 | ID 確定通知 (KEY_C 短押し時、payload=newId) |
| 0x83 | READY | 0 | 準備完了通知 (起動時・再接続時) |
| 0x84 | ENTER_MANUAL | 0 | **legacy**: 旧 fw 用専用イベント (新 fw は 0x86 で送る) |
| 0x86 | BOOT_BUTTONS | 1 | 起動時に押下されていたボタン bitmap |
| 0x87 | BTN_LONGPRESS | 1 | 通常動作中の長押し確定通知 bitmap |

bitmap (0x86 / 0x87 共通):

| ビット | 名称 | 値 |
|:---:|---|:---:|
| bit 0 | BTN_KEY_A | 0x01 |
| bit 1 | BTN_KEY_B | 0x02 |
| bit 2 | BTN_KEY_C | 0x04 |
| bit 3 | BTN_5WAY_PRESS | 0x08 |
| bit 4-7 | reserved | (5-way UP/DOWN/LEFT/RIGHT 等の将来拡張用) |

> - 0x82 (EMO_TOGGLE) は manual EMO 廃止により削除済み
> - 0x85 は予約 (開発中の専用イベントから bitmap 形式へ統一)

## 関数リファレンス

### I2Cコールバック関数

#### `void receiveEvent(int howMany)`

I2C Masterからデータを受信した時に呼び出される割り込みハンドラ。

| パラメータ | 型 | 説明 |
|------------|-----|------|
| howMany | int | 受信バイト数 |

**処理内容:**
- 受信データを `i2cRxBuffer` に格納
- `i2cDataReady` フラグをセット
- バッファオーバーラン検出時は警告出力

---

#### `void requestEvent()`

I2C Masterからデータ要求があった時に呼び出される割り込みハンドラ。

**処理内容:**
- イベントキューに溜まっているイベントを送信
- キューが空の場合は何も送信しない

---

### イベントキュー関数

#### `void queueEvent(uint8_t cmd, uint8_t dataLen, const uint8_t* data)`

Master送信用のイベントをキューに追加。

| パラメータ | 型 | 説明 |
|------------|-----|------|
| cmd | uint8_t | コマンドコード (0x81, 0x83, 0x86, 0x87; 0x84 は legacy) |
| dataLen | uint8_t | データ長 |
| data | const uint8_t* | データポインタ |

**戻り値:** なし

**注意:** キューが満杯の場合はイベントを破棄

---

### コマンドハンドラ関数

#### `void handleUpdateStatus(const uint8_t* data, uint8_t len)`

`CMD_UPDATE_STATUS` (0x01) の処理。WiFi状態とIP/Port情報を更新。

| パラメータ | 型 | 説明 |
|------------|-----|------|
| data | const uint8_t* | 受信データ |
| len | uint8_t | データ長 |

**更新内容:**
- WiFi接続状態
- mDNS有効状態
- PowerBoard接続状態
- IPアドレス
- 待受ポート番号
- Stop Reason
- Power Status (0:STOP, 1:STANDBY, 2:DRIVE)

---

#### `void handleSetRobotId(const uint8_t* data, uint8_t len)`

`CMD_SET_ROBOT_ID` (0x02) の処理。確定済みRobot IDを設定。

| パラメータ | 型 | 説明 |
|------------|-----|------|
| data | const uint8_t* | 受信データ (data[0] = robotId) |
| len | uint8_t | データ長 |

**処理内容:**
- `state.robotId` を更新
- `state.setId` も同期して更新

---

#### `void handleUpdateEmo(const uint8_t* data, uint8_t len)`

`CMD_UPDATE_EMO` (0x03) の処理。Remote EMO 状態を更新。

| パラメータ | 型 | 説明 |
|------------|-----|------|
| data | const uint8_t* | 受信データ [remoteEMO] (1 byte; manual EMO 廃止) |
| len | uint8_t | データ長 |

---

#### `void handleFullRefresh(const uint8_t* data, uint8_t len)`

`CMD_FULL_REFRESH` (0x05) の処理。全表示データを一括更新（起動時使用）。

| パラメータ | 型 | 説明 |
|------------|-----|------|
| data | const uint8_t* | 全データパケット |
| len | uint8_t | データ長 |

**更新内容:**
- ステータス全般
- IP/Port
- Robot ID
- Remote EMO 状態
- SSID
- ESP32バージョン

---

#### `void handleUpdateNetwork(const uint8_t* data, uint8_t len)`

`CMD_UPDATE_NETWORK` (0x06) の処理。SSIDとバージョン情報を更新。

| パラメータ | 型 | 説明 |
|------------|-----|------|
| data | const uint8_t* | 受信データ (SSID + Version) |
| len | uint8_t | データ長 |

**更新内容:**
- SSID
- ESP32バージョン

---

#### `void processI2CCommands()`

受信バッファ内のコマンドを解析して適切なハンドラに振り分け。

**処理内容:**
1. `i2cDataReady` フラグをチェック
2. CMD/LENを解析
3. 対応するハンドラ関数を呼び出し

---

### 表示関数

#### `void updateDisplay()`

現在の状態をTFT画面に描画。`state.needsUpdate` が true の時のみ更新。

**表示内容:**
- 設定中ID (setId)
- ESP32接続状態 / SSID
- Remote EMO 状態
- IPアドレス
- 待受ポート
- 確定済みRobot ID
- PowerBoard状態 (STOP/STANDBY/DRIVE)
- Stop Reason（テキスト形式）
- WiFi接続状態
- mDNS状態
- バージョン情報 (ESP32/WIO)

---

### ヘルパー関数

#### `const char* getPowerStatusText(const DisplayState& s)`

PowerBoard状態をテキストで返す。

| Status | 戻り値 |
|--------|--------|
| 0 | "STOP" |
| 1 | "STANDBY" |
| 2 | "DRIVE" |
| その他 | "UNKNOWN" |
| 未接続 | "No Response" |

---

#### `const char* getStopReasonText(uint8_t stopReason)`

Stop Reasonをテキストで返す。

| bit | テキスト |
|-----|---------|
| 0x01 | "MAIN" |
| 0x02 | "LOCAL" |
| 0x04 | "REMOTE" |
| 0x08 | "OVERCUR" |
| 0x10 | "LOW_BAT" |
| 0 | "" (空文字) |

---

### ボタン処理関数

#### `void handleButtons()`

各ボタンを `updateButton(pin, track, mask, longpressBitmap, onShortPress)` で一様に処理する:

- 押下立ち上がり (rising edge of pressed) で `onShortPress` を呼ぶ (NULL なら短押しなし)
- 押下継続が `LONGPRESS_THRESHOLD_MS` (1000ms) 超えた最初のループで `longpressBitmap` に該当ビットを立てる
- 同一押下サイクルで長押しは 1 回のみ
- 短押しと長押しは同一押下中に両方発火し得る (短押し先, 長押し後)。現状の用途では衝突しない

ループ末尾で `longpressBitmap != 0` なら `CMD_BTN_LONGPRESS` (0x87) を 1 byte payload でキューする。

| ボタン | 短押し処理 | 長押し処理 (1 秒) |
|--------|-----------|------------------|
| KEY_A | setId 減少、表示更新 | bitmap に BTN_KEY_A を立てて報告 |
| KEY_B | setId 増加、表示更新 | bitmap に BTN_KEY_B を立てて報告 |
| KEY_C | `CMD_ID_CONFIRM` (0x81) をキューに追加 | bitmap に BTN_KEY_C を立てて報告 |
| 5WAY_PRESS | (短押しは未割当, NULL) | bitmap に BTN_5WAY_PRESS を立てて報告 |

> 起動時押下中だったボタンは `setup()` Phase 2.5 で「既に押下サイクル中」(`prev_pressed=true, longpress_fired=true`) として登録されるため、リリースして再押下するまで `handleButtons()` 側では発火しない。

---

### メイン関数

#### `void setup()`

初期化処理。

**処理内容:**
1. Phase 1: 状態構造体初期化 + I2C Slave 開始 (アドレス 0x08) + `CMD_READY` (0x83) をキュー
2. Phase 2: ボタンピン初期化 (INPUT_PULLUP) + Serial 初期化 (115200bps)
3. Phase 2.5: 4 ボタン (KEY_A/B/C, 5WAY_PRESS) を 50ms (10×5ms) ポーリングし、連続 LOW (押下) を確定したボタンの bitmap (`bootBtns`) を作成。`bootBtns != 0` のとき `CMD_BOOT_BUTTONS` (0x86) を 1 byte payload でキュー。同じく押下中だったボタンは `handleButtons` 側のトラッカーに「既押下サイクル」を反映 (誤発火抑止)
4. Phase 3: TFT 初期化 + スタートアップ表示 (BTN_5WAY_PRESS で "MANUAL MODE", BTN_KEY_C で "IGNORE OC/OD" を表示)
5. Phase 4: WiFi 起動は loop() 初回に遅延

---

#### `void loop()`

メインループ。

**処理内容:**
1. `processI2CCommands()` - I2Cコマンド処理
2. `handleButtons()` - ボタン入力処理
3. ESP32接続タイムアウト確認 (1秒)
4. 自動復旧処理（未接続時2秒間隔でCMD_READY送信）
5. `updateDisplay()` - 表示更新
6. 10ms delay

---

## 自動復旧機能

WioTerminalがリセットされた場合、自動的にESP32からデータを再取得します。

1. `state.xiaoConnected` が `false` の状態で2秒経過
2. `CMD_READY` (0x83) をESP32に送信
3. ESP32が受信すると全データを再送信
   - `UPDATE_STATUS` (0x01)
   - `UPDATE_NETWORK` (0x06)
   - `SET_ROBOT_ID` (0x02)
   - `UPDATE_EMO` (0x03)

---

## 状態構造体

```cpp
struct DisplayState {
    // ID management
    uint8_t robotId;          // 確定済みID (0xFF = invalid)
    uint8_t setId;            // 設定中ID

    // Connection status
    bool xiaoConnected;       // ESP32接続状態
    unsigned long lastXiaoUpdate;

    // WiFi status
    bool wifiConnected;
    bool mdnsEnabled;
    uint8_t ipAddress[4];
    uint16_t listenPort;

    // EMO status (manual EMO 廃止; remote のみ)
    bool remoteEMO;

    // PowerBoard status
    bool powerBoardConnected;
    uint8_t stopReason;
    uint8_t powerStatus;      // 0:STOP, 1:STANDBY, 2:DRIVE

    // Network info
    char ssid[33];
    char espVersion[16];

    // Display control
    bool needsUpdate;
};
```

## 表示レイアウト

```
┌────────────────────────────────────────┐
│ set  +    -  sel: [setId]              │
│                                        │
│ [Xiao Disconnected / SSID: xxx]        │
│ EMO:  [Remote]                         │
│ IP: [xxx.xxx.xxx.xxx / disconnected]   │
│ Listen Port: [ppppp]                   │
│ Robot ID: [robotId]                    │
│ PWR: [STOP / STANDBY / DRIVE] [reason] │
│ [MDNS enable/disable]                  │
│ ESP:[version] | WIO:[version]          │
└────────────────────────────────────────┘
```

### PowerBoard状態表示

| Status | 色 | 説明 |
|--------|-----|------|
| STOP | 黄色 | 停止中 |
| STANDBY | 緑色 | 待機中 |
| DRIVE | シアン | 駆動中 |
| No Response | オレンジ | 通信断 |

### Stop Reason表示（テキスト）

- MAIN (0x01)
- LOCAL (0x02)
- REMOTE (0x04)
- OVERCUR (0x08)
- LOW_BAT (0x10)

## バージョン

- WIO Version: 2.0.0

## 参照

- [I2C通信プロトコル設計書](../../docs/I2C_Protocol_Design.md)
