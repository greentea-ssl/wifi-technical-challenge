# ESP32C5Controller

XIAO ESP32C5用 I2C Master コントローラ

## 概要

ESP32C5Controllerは、XIAO ESP32C5をI2C Masterとして動作させ、WiFi/UDP通信を管理し、WioTerminal (Slave) に表示データを送信します。また、CAN HID通信でPowerBoardを制御します。

## 機能

- WiFi接続管理 (STA模式)
- UDP通信
  - AIダウンリンク受信 (ポート 40000 + robotId) → メイン基板へ UART 転送 (Remote EMO 中も転送)
  - メイン基板アップリンク送信 (ポート 50000 + robotId)
  - EMSパケット受信 (ポート 40999) — Remote EMO トリガ
- mDNS広告 (`robot{id}.local`)
- Robot ID永続化 (Preferences)
- I2C Master通信 (WioTerminal表示制御)
- Remote EMO 制御 (EMSパケット駆動の遠隔停止)
- CAN HID 通信 (robot_comm_spec v2.0.0)
  - PowerBoard 直結 (CAN 0x088 / 0x0D8): ロボット ID 設定, STOP/RESUME, 状態取得, OC/OD 保護オーバーライド (HID_CMD_PROTECTION_OVERRIDE=0x05)
  - 起動時 KEY_C 押下で OC/OD オーバーライドを 0x088 の 0x05 コマンドで設定 (v1.x の 0x201 PARAM 直送回避策は v2.0.0 で廃止、詳細は `sendIgnoreOCODOverride()` のコメント参照)
  - メイン基板向け HID 状態通知 (CAN 0x008, rev4 §1.3): 動作モード + robotId
  - メイン基板からの FW バージョン応答受信 (CAN 0x040, rev4 §1.4, **DLC=5**: FW version 4B + 動作モード echo-back 1B)
- ローカル停止 (Wio 5WAY 1 秒長押し) と Remote EMO の OR 統合 (`applyEstopState()`): どちらが立っても PowerBoard へ STOP、両方降りた時のみ RESUME を発行
- hid_bridge (PC↔HID UDP/JSON, port 41000/51000): CAN 送出 / テレメトリ転送 / hid_status 状態取得 (旧 Web Server は robot_comm_spec v2.1.0 で廃止)
- OTA アップデート (UDP/40999 + EMS と共有)。更新中は進行状況 (接続/DL%/適用/完了/失敗) を I2C `CMD_OTA_PROGRESS (0x07)` で Wio に送り、Wio が専用画面に表示する

## ハードウェア要件

- XIAO ESP32C5
- I2C接続: SDA/SCL
- 外部プルアップ抵抗: 4.7kΩ (SDA/SCL各ライン)
- CAN接続: CAN Transceiver

## ピン配置 (XIAO ESP32C5)

| 機能 | GPIO | ピン |
|------|------|------|
| I2C SDA | 23 | D4 |
| I2C SCL | 24 | D5 |
| Serial1 TX | 11 | D6 |
| Serial1 RX | 12 | D7 |
| CAN TX | 1 | D0 |
| CAN RX | 0 | D1 |

## 開発環境

### Arduino IDE

1. ボードマネージャURL追加:
   ```
   https://espressif.github.io/arduino-esp32/package_esp32_dev_index.json
   ```

2. ボードパッケージインストール: `esp32` (ver 3.0以上)

3. ボード選択: `XIAO_ESP32C5`

### arduino-cli

```bash
# コンパイル
arduino-cli compile -b esp32:esp32:XIAO_ESP32C5 ./ESP32C5Controller.ino

# 書き込み
arduino-cli upload -p COMx -b esp32:esp32:XIAO_ESP32C5 ./ESP32C5Controller.ino
```

## 設定

### WiFi設定 (`config.h`)

```cpp
#define WIFI_SSID       "your_ssid"
#define WIFI_PASSWORD   "your_password"
```

ビルド時に `-D__CONFIG__ -D__SSID__=\"xxx\" -D__PASSWD__=\"xxx\"` で指定も可能

## ファイル構成

```
ESP32C5Controller/
├── ESP32C5Controller.ino    # メインプログラム
└── config.h                 # WiFi設定等
```

## ポート構成

| 用途 | ポート番号 | 説明 |
|------|------------|------|
| AIダウンリンク | 40000 + robotId | 受信のみ |
| アップリンク | 50000 + robotId | 送信のみ |
| EMS | 40999 | 受信のみ |
| hid_bridge Downlink | 41000 + robotId | PC→HID JSON (CAN送出 / set_log_level / hid_status 要求) |
| hid_bridge Uplink | 51000 + robotId | HID→PC JSON (テレメトリ / hid_status 応答, broadcast) |

## 停止系 (E-Stop) 動作仕様

Manual EMO (旧 5WAY 短押しトグル) は廃止。残るのは:

| 種別 | トリガー | 状態変数 |
|------|----------|----------|
| Remote EMO | EMS パケット受信 (UDP/40999) | `isRemoteEMO` |
| Local 停止 | Wio 5WAY を 1 秒以上長押し → トグル | `isLocalEstop` |

両者は PowerBoard 側で同じ `ABORT_HID_ESTOP` ビットを共有するため、`applyEstopState()` で OR (`isRemoteEMO || isLocalEstop`) してから CAN を発行する。エッジ検出 (`prevHidEstopAsserted`) で同じ状態の連続送信は抑止 (force=true 指定で初期化/再接続時のみ強制送信)。

- **EMS タイムアウト**: 3 秒 (`EMS_TIMEOUT_MS`)。タイムアウト後 `isRemoteEMO = false` → `applyEstopState(force=true)`
- **Active 時**: PowerBoard に CAN STOP (0x088 / cmd=0x03) を送信
- **両方解除時**: PowerBoard に CAN RESUME (0x088 / cmd=0x04) を送信
- **片方だけ解除**: STOP を維持 (もう片方の停止意図を踏み潰さない)
- **UDP→UART 転送**: Remote EMO 状態に関わらず常に転送 (commit 8b64ed6 以降)

## 起動時 KEY_C 押下: PowerBoard OC/OD 保護のオーバーライド

Wio 起動時に KEY_C 押下保持で起動すると、Wio が `CMD_BOOT_BUTTONS (0x86)` の `BTN_KEY_C` ビット (0x04) を立てて C5 に通知する。C5 はこれを受けて `sendIgnoreOCODOverride()` を呼び、PowerBoard に CAN 0x088 HID_CMD_PROTECTION_OVERRIDE (0x05) を 1 フレーム送信:

```
Frame: [0x05 (HID_CMD_PROTECTION_OVERRIDE), 0x03 (bit0=過電流, bit1=過放電)]
```

> robot_comm_spec v2.0.0 で HID 直結チャネル (CAN ID 0x088) に保護オーバーライド設定コマンド (0x05) が新設された正規ルート。v1.x では 0x088 に該当コマンドが無く、HID は本来「メイン基板 → 電源基板」役割の 0x201 PARAM_CMD_SET を 2 フレーム直送する暫定実装で代替していた (v2.0.0 で解消)。

このとき hid_bridge のログレベルも `BRIDGE_VERBOSE_LOG_LEVEL` (=5/TRACE) に引き上げる (`sendIgnoreOCODOverride()` 内)。保護を無効化した危険動作中はテレメトリを最大限転送する意図。

## マニュアルモード (rev4 §1.3, CAN 0x008)

メイン基板に対して HID の動作モードを通知する仕組み。

| 値 | モード | 投入トリガー |
|----|--------|-------------|
| 0 | ノーマル | 通常起動 |
| 1 | マニュアル | WioTerminal の 5WAY を押下しながら起動 |
| 2 | デバッグ | (未実装) |

`0x008` 発行タイミング (`sendHIDStatusToMain()` 関数):
- HID 起動完了直後 (`setup()` 末尾)
- ロボット ID 変更時 (`handleIdConfirm()`)
- マニュアルモード投入時 (`case CMD_BOOT_BUTTONS` の 5WAY ビット、または legacy `case CMD_ENTER_MANUAL`)

メイン基板からの応答 `0x040` (FW バージョン + 動作モード echo-back, DLC=5) は `twai_rx_callback` で受信し、`processCANMessages()` 内でバージョン or モード変化時にログ出力。Main がエコーバックした動作モードが `hidOpMode` と一致しない場合は `[WARN] Op mode mismatch!` を出力。

マニュアルモード投入時は hid_bridge のログレベルも `BRIDGE_VERBOSE_LOG_LEVEL` (=5/TRACE) に引き上げる。

## hid_bridge 起動時ログレベル

hid_bridge (port 51000+id) のテレメトリ転送しきい値は、起動モードに応じて初期化される (揮発。PC からの `set_log_level` で実行時上書き可)。

| 起動モード | 初期ログレベル | 設定箇所 |
|----|----|----|
| 通常起動 | `2` (WARN) — `BRIDGE_DEFAULT_LOG_LEVEL` | グローバル初期値 (`bridgeLogLevel`) |
| MANUAL モード (5-way 起動押下) | `5` (TRACE) — `BRIDGE_VERBOSE_LOG_LEVEL` | `case CMD_BOOT_BUTTONS` の 5WAY ビット / legacy `case CMD_ENTER_MANUAL` |
| 保護オーバーライド (KEY_C 起動押下) | `5` (TRACE) — `BRIDGE_VERBOSE_LOG_LEVEL` | `sendIgnoreOCODOverride()` |

> 通信仕様 (robot_comm_spec) は起動時ログレベルを規定しない (ファーム固有のポリシー)。spec が定めるのは転送ルール (severity ≤ level、種別 `11110` は常時) のみ。

## バージョン

- Controller Version: 3.0.2

## 参照

- [I2C通信プロトコル設計書](../../docs/I2C_Protocol_Design.md)
- [Seeed Studio XIAO ESP32C5 Wiki](https://wiki.seeedstudio.com/xiao_esp32c5_getting_started/)
