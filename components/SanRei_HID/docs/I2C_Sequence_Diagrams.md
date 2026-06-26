# ESP32C5Controller ↔ WioDisplay 通信シーケンス

## 通信の前提

- **物理層**: I2C(SDA=23, SCL=24, 100 kHz)
- **役割**: ESP32C5 = Master, WioDisplay = Slave (`0x08`)
- **方向**:
  - **Master → Slave**: `Wire.beginTransmission` + `cmd, len, data...` ([ESP32C5Controller.ino:147](../src/ESP32C5Controller/ESP32C5Controller.ino))
  - **Slave → Master**: Slave側の `eventQueue` に積み、Masterが `Wire.requestFrom(0x08, 32)` でポーリング ([ESP32C5Controller.ino:289](../src/ESP32C5Controller/ESP32C5Controller.ino), [WioDisplay.ino:114](../src/WioDisplay/WioDisplay.ino))

| コード | 名称 | 方向 | 意味 |
|---|---|---|---|
| `0x01` | `CMD_UPDATE_STATUS` | M→S | WiFi/IP/Port/PowerBoard状態の定期更新 |
| `0x02` | `CMD_SET_ROBOT_ID` | M→S | 確定後のRobot IDをDisplayへ反映 |
| `0x03` | `CMD_UPDATE_EMO` | M→S | Remote EMO 状態の更新 (1 byte) |
| `0x05` | `CMD_FULL_REFRESH` | M→S | 全項目の一括再送 |
| `0x06` | `CMD_UPDATE_NETWORK` | M→S | SSID+Versionの更新 |
| `0x81` | `CMD_ID_CONFIRM` | S→M | KEY_C 短押しでID確定 |
| `0x83` | `CMD_READY` | S→M | Display起動/再接続要求 |
| `0x84` | `CMD_ENTER_MANUAL` | S→M | **legacy**: 旧 Wio fw 用 (新 fw は 0x86 を使用) |
| `0x86` | `CMD_BOOT_BUTTONS` | S→M | 起動時に押されていたボタン bitmap (1 byte) |
| `0x87` | `CMD_BTN_LONGPRESS` | S→M | 通常動作中の長押し確定通知 bitmap (1 byte) |

> - `0x82` (`CMD_EMO_TOGGLE`) は manual EMO 廃止により削除済み
> - `0x85` は予約 (開発中の専用イベントから bitmap 形式へ統一)
> - bitmap: `BTN_KEY_A=0x01`, `BTN_KEY_B=0x02`, `BTN_KEY_C=0x04`, `BTN_5WAY_PRESS=0x08`, 0x10-0x80 reserved

---

## ① 起動シーケンス (READY ハンドシェイク)

```mermaid
sequenceDiagram
    participant M as ESP32C5 (Master)
    participant S as WioDisplay (Slave)

    Note over M: setup()<br/>I2C/CAN初期化、WiFi非同期開始
    Note over S: setup()<br/>TFT初期化、I2C Slave開始
    S->>S: queueEvent(CMD_READY)
    Note over M: delay(2000)<br/>WioDisplayの起動待ち

    M->>S: CMD_UPDATE_STATUS (0x01, len=10)
    Note right of S: 初期値はWiFi未接続 etc.
    M->>S: CMD_UPDATE_NETWORK (0x06, SSID/Ver)
    M->>S: CMD_SET_ROBOT_ID (0x02, robotId)
    M->>S: CMD_UPDATE_EMO (0x03, remote=0)

    loop メインループ (~10ms毎)
      M->>S: Wire.requestFrom(0x08, 32)
      S-->>M: CMD_READY (0x83, len=0)
      Note over M: handleWioReady()<br/>全データを再送
      M->>S: CMD_UPDATE_STATUS
      M->>S: CMD_UPDATE_NETWORK
      M->>S: CMD_SET_ROBOT_ID
      M->>S: CMD_UPDATE_EMO
    end
```

参照: [ESP32C5Controller.ino:1217-1228](../src/ESP32C5Controller/ESP32C5Controller.ino), [ESP32C5Controller.ino:376-388](../src/ESP32C5Controller/ESP32C5Controller.ino), [WioDisplay.ino:597-600](../src/WioDisplay/WioDisplay.ino)

---

## ② 定期ステータス更新 (150ms周期)

```mermaid
sequenceDiagram
    participant M as ESP32C5
    participant S as WioDisplay

    loop STATUS_UPDATE_INTERVAL_MS = 150ms
      M->>S: CMD_UPDATE_STATUS (status bitmap, IP, port, stopReason, powerStatus)
      Note right of S: handleUpdateStatus()<br/>差分があれば needsUpdate=true<br/>xiaoConnected=true 更新
    end

    loop 各ループ (Slave側)
      Note over S: lastXiaoUpdate から 1秒超 →<br/>xiaoConnected=false → "Xiao Disconnected"表示
    end
```

参照: [ESP32C5Controller.ino:1267-1269](../src/ESP32C5Controller/ESP32C5Controller.ino), [WioDisplay.ino:201-237](../src/WioDisplay/WioDisplay.ino), [WioDisplay.ino:610-613](../src/WioDisplay/WioDisplay.ino)

---

## ③ ID変更 (KEY_A/B/C 操作)

```mermaid
sequenceDiagram
    actor U as User
    participant S as WioDisplay
    participant M as ESP32C5
    participant P as PowerBoard (CAN)

    U->>S: KEY_A 押下 (setId--)
    Note over S: ローカルsetIdのみ変化<br/>画面再描画
    U->>S: KEY_B 押下 (setId++)
    U->>S: KEY_C 押下 (確定)
    S->>S: queueEvent(CMD_ID_CONFIRM, [setId])

    M->>S: Wire.requestFrom(0x08, 32)
    S-->>M: CMD_ID_CONFIRM (0x81, newId)
    Note over M: handleIdConfirm()<br/>NVS保存・UDP/mDNS再構成
    M->>S: CMD_SET_ROBOT_ID (0x02, robotId)
    Note right of S: state.robotId = setId
    M->>P: CAN 0x088: HID_CMD_SET_ROBOT_ID
```

参照: [WioDisplay.ino:529-537](../src/WioDisplay/WioDisplay.ino), [ESP32C5Controller.ino:327-367](../src/ESP32C5Controller/ESP32C5Controller.ino)

---

## ④ 起動時 BOOT_BUTTONS (5-Way 押下 → MANUAL モード, rev4 §1.3)

```mermaid
sequenceDiagram
    actor U as User
    participant S as WioDisplay
    participant M as ESP32C5
    participant Main as メイン基板 (CAN-LS)

    Note over U,S: 電源 ON 時に 5-Way 押下保持
    Note over S: setup() Phase 2.5: 4 ボタンを 50ms (10×5ms) polling
    Note over S: bootBtns |= BTN_5WAY_PRESS (0x08)
    S->>S: queueEvent(CMD_BOOT_BUTTONS, [0x08])
    Note over S: TFT に "MANUAL MODE" 表示

    Note over M: setup() 末尾: sendHIDStatusToMain(NORMAL, robotId)
    M->>Main: CAN 0x008 [mode=0, robotId]

    M->>S: Wire.requestFrom(0x08, 32)
    S-->>M: CMD_BOOT_BUTTONS (0x86, [0x08])
    Note over M: mask & BTN_5WAY_PRESS<br/>hidOpMode = OP_MODE_MANUAL
    M->>Main: CAN 0x008 [mode=1, robotId]

    Main-->>M: CAN 0x040 [FW version 4B + opMode echo 1B] (応答, DLC=5)
```

参照: [WioDisplay.ino setup() Phase 2.5](../src/WioDisplay/WioDisplay.ino), [ESP32C5Controller.ino case CMD_BOOT_BUTTONS](../src/ESP32C5Controller/ESP32C5Controller.ino)

> Legacy: 旧 Wio fw が `CMD_ENTER_MANUAL (0x84)` を送る場合も、C5 側ハンドラで同じく hidOpMode=MANUAL に遷移する。

---

## ⑤ 起動時 BOOT_BUTTONS (KEY_C 押下 → OC/OD ignore)

```mermaid
sequenceDiagram
    actor U as User
    participant S as WioDisplay
    participant M as ESP32C5
    participant P as PowerBoard

    Note over U,S: 電源 ON 時に KEY_C 押下保持
    Note over S: setup() Phase 2.5: bootBtns |= BTN_KEY_C (0x04)
    S->>S: queueEvent(CMD_BOOT_BUTTONS, [0x04])
    Note over S: TFT に "IGNORE OC/OD" 表示

    M->>S: Wire.requestFrom(0x08, 32)
    S-->>M: CMD_BOOT_BUTTONS (0x86, [0x04])
    Note over M: mask & BTN_KEY_C<br/>sendIgnoreOCODOverride()

    M->>P: CAN 0x088 HID_CMD_PROTECTION_OVERRIDE [0x05, 0x03]

    Note over P: ctx.ignore_masks |= IGNORE_OVERCURRENT | IGNORE_OVERDISCHARGE
    Note over P: 既存の ABORT_OVERCURRENT / OVERDISCHARGE はマスクされる<br/>(hasActiveAbortReason() で除外)

    P-->>M: CAN 0x0D8 HID直接応答 (result, robotId, state, abort)
```

> **設計メモ**: robot_comm_spec v2.0.0 で HID 直結チャネル (CAN ID 0x088) に保護
> オーバーライド設定コマンド (0x05, Byte1 bit0=過電流/bit1=過放電) が新設された正規ルート。
> v1.x では 0x088 に該当コマンドが無く、HID は本来「メイン基板 → 電源基板」役割の
> 0x201 PARAM_CMD_SET を 2 フレーム直送する暫定実装で代替していた (v2.0.0 で解消)。

参照: [WioDisplay.ino setup() Phase 2.5](../src/WioDisplay/WioDisplay.ino), [ESP32C5Controller.ino sendIgnoreOCODOverride()](../src/ESP32C5Controller/ESP32C5Controller.ino), [robot_comm_spec/CAN_LS.md §2.7](../robot_comm_spec/CAN_LS.md)

---

## ⑥ BTN_LONGPRESS (5-Way 長押し → ローカル停止トグル)

```mermaid
sequenceDiagram
    actor U as User
    participant S as WioDisplay
    participant M as ESP32C5
    participant P as PowerBoard

    Note over U,S: 通常動作中に 5-Way を 1 秒以上押下保持
    Note over S: handleButtons() / updateButton() が<br/>now - press_start_ms ≥ LONGPRESS_THRESHOLD_MS<br/>を検出 (押下サイクルあたり 1 回のみ)
    Note over S: longpressBitmap |= BTN_5WAY_PRESS (0x08)
    S->>S: queueEvent(CMD_BTN_LONGPRESS, [0x08])

    M->>S: Wire.requestFrom(0x08, 32)
    S-->>M: CMD_BTN_LONGPRESS (0x87, [0x08])
    Note over M: mask & BTN_5WAY_PRESS<br/>isLocalEstop = !isLocalEstop
    Note over M: applyEstopState():<br/>active = isRemoteEMO ‖ isLocalEstop

    alt 状態が変わった (エッジ)
        alt active=true
            M->>P: CAN 0x088 HID_CMD_STOP
            Note over P: abort_reasons |= ABORT_HID_ESTOP (bit1)
        else active=false
            M->>P: CAN 0x088 HID_CMD_RESUME
            Note over P: abort_reasons &= ~ABORT_HID_ESTOP
        end
    end

    P-->>M: CAN 0x258 (テレメトリ: stopReason=0x02 等)
    M->>S: CMD_UPDATE_STATUS [stopReason=...]
    Note over S: stopReason bit1 → "LOCAL" 表示
```

参照: [WioDisplay.ino handleButtons() / updateButton()](../src/WioDisplay/WioDisplay.ino), [ESP32C5Controller.ino case CMD_BTN_LONGPRESS / applyEstopState()](../src/ESP32C5Controller/ESP32C5Controller.ino)

---

## ⑦ Remote EMO 変化 (UDP 由来) + ローカル停止 OR 統合

```mermaid
sequenceDiagram
    participant Net as External UDP
    participant M as ESP32C5
    participant S as WioDisplay
    participant P as PowerBoard

    Net->>M: UDP "stop" → port 40999 (EMS)
    Note over M: lastEmsPacketTime更新<br/>isRemoteEMO=true
    Note over M: applyEstopState(force=true)<br/>active = isRemoteEMO ‖ isLocalEstop = true
    M->>S: CMD_UPDATE_EMO (remote=1)
    M->>P: CAN 0x088 HID_CMD_STOP

    Note over M: EMS_TIMEOUT_MS=3000ms 経過<br/>→ isRemoteEMO=false<br/>applyEstopState(force=true)
    alt isLocalEstop=false
        M->>S: CMD_UPDATE_EMO (remote=0)
        M->>P: CAN 0x088 HID_CMD_RESUME
    else isLocalEstop=true
        M->>S: CMD_UPDATE_EMO (remote=0)
        Note over M: ローカル停止が立っているので<br/>STOP を維持 (RESUME しない)
        M->>P: CAN 0x088 HID_CMD_STOP
    end
```

> Remote EMO とローカル停止は PowerBoard 側で同じ ABORT_HID_ESTOP ビットを共有するため、
> C5 側で OR 統合してから HID_CMD_STOP/RESUME を発行する。
> 片方の解除がもう片方の停止意図を踏み潰さないよう、`applyEstopState()` で必ず両方の状態を見る。

参照: [ESP32C5Controller.ino applyEstopState()](../src/ESP32C5Controller/ESP32C5Controller.ino)

---

## ⑧ WiFi接続イベント

```mermaid
sequenceDiagram
    participant W as WiFi Stack
    participant M as ESP32C5
    participant S as WioDisplay

    W-->>M: ARDUINO_EVENT_WIFI_STA_GOT_IP
    Note over M: wifiConnected=true<br/>UDP/mDNS開始
    M->>S: CMD_UPDATE_STATUS (wifi=1, IP, port)
    M->>S: CMD_UPDATE_NETWORK (SSID, version)

    W-->>M: ARDUINO_EVENT_WIFI_STA_DISCONNECTED
    Note over M: wifiConnected=false
    Note over M: 次の150ms周期で<br/>CMD_UPDATE_STATUS送信
    M->>S: CMD_UPDATE_STATUS (wifi=0)
```

参照: [ESP32C5Controller.ino:683-723](../src/ESP32C5Controller/ESP32C5Controller.ino)

---

## ⑨ Xiao切断検出 / 再接続要求

```mermaid
sequenceDiagram
    participant M as ESP32C5
    participant S as WioDisplay

    Note over M: ESP32C5側がリセット/フリーズ
    Note over S: lastXiaoUpdateから1s経過<br/>→ xiaoConnected=false<br/>"Xiao Disconnected"表示

    loop 2秒毎
      Note over S: queueEvent(CMD_READY)
      M->>S: Wire.requestFrom(0x08, 32) (復帰時)
      S-->>M: CMD_READY (0x83)
      Note over M: handleWioReady() で全再送
      M->>S: CMD_UPDATE_STATUS
      M->>S: CMD_UPDATE_NETWORK
      M->>S: CMD_SET_ROBOT_ID
      M->>S: CMD_UPDATE_EMO
      Note over S: xiaoConnected=true 復帰
    end
```

参照: [WioDisplay.ino:610-620](../src/WioDisplay/WioDisplay.ino), [ESP32C5Controller.ino:310-313](../src/ESP32C5Controller/ESP32C5Controller.ino)

---

## 注意点 (実装上の特徴)

- `CMD_FULL_REFRESH` (0x05) は両側に定義はあるが、現コードのMaster側に呼び出し元がない (READY時は `sendUpdateStatus` + `sendUpdateNetwork` + `sendSetRobotId` + `sendUpdateEmo` を個別送信)。
- Slave→MasterのイベントはMaster側のループで毎回 `Wire.requestFrom` してポーリングする方式。Slave側に未送信イベントがない場合は `Wire.available() < 2` で空読みされる。
- Slave側 `eventQueue` は16バイトのリングバッファで、容量超過時はサイレントに破棄される ([WioDisplay.ino:139-151](../src/WioDisplay/WioDisplay.ino))。
