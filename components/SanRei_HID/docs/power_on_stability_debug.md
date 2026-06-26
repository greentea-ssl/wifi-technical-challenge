# 電源投入安定性デバッグ (Wio / Xiao) — 構成・経緯・対策

## 1. 目的 (ゴール)

電源投入時に Wio Terminal 画面で「**xiao disconnected**」「**PWR: No Response**」が表示され、
通信が不完全に見える問題の調査と対策。最終的に Wio/Xiao を **OTA で更新できる**状態まで持っていく。

## 2. 検証構成 (テストベンチ)

- **電源**: PowerBoard に AC アダプタから 16V を供給。**バッテリのバランス端子は未接続**。
- **課題**: C5(Xiao)/Wio に USB を直結すると USB から給電され、PowerBoard の給電制御を
  バイパスしてしまうため、電源投入安定性を検証できない。
- **解決策(ログ採取手法)**: C5/Wio の GPIO に **USB-serial 変換器を接続(VCC 線は結線しない)**。
  そちら経由でログを出力することで、**PowerBoard からの給電制御を効かせたまま**起動直後の挙動を観測する。

### 配線(デバッグ UART)

| ボード | 信号 | ピン | 内部 |
|---|---|---|---|
| Wio Terminal | TX | **D0** (PB08) | SERCOM4/PAD0 |
| Wio Terminal | RX | **D1** (PB09) | SERCOM4/PAD1 (本用途では未使用) |
| Xiao ESP32C5 | TX | **GPIO9** | UART0 (`Serial0`) |
| Xiao ESP32C5 | RX | **GPIO8** | UART0 (本用途では未使用) |

> SAMD51 の UART TX は PAD0/PAD2 のみ。D0=PAD0 / D1=PAD1 のため **TX=D0 / RX=D1 が唯一の構成**
> (当初要望の「TX=D1」はハードウェア上不可)。変換器 RX を Wio D0 へ接続する。

## 3. ログ実装 (LogMux)

両ファームとも、全ログを **USB CDC と デバッグ UART の両方** へ出すロガー `LogMux Log` を導入し、
既存の `Serial.print*` を `Log.*` へ置換した。

### Wio (`src/WioDisplay/WioDisplay.ino`)

- デバッグ UART = **sercom4 (D0/D1)**。`Uart Serial3` は begin() による UART 設定にのみ使用し、
  **送信は DRE ポーリングで DATA に直接書き込む**(`LogMux::dbgByte`)。
  - 理由: SERCOM4 の割り込みハンドラは core の `Wire.cpp` が **Wire1(gyro, sercom4)用に非weak で定義**
    しており、Serial3 へ割り込みを向けられない(多重定義になる)。`Serial3.write()` は割り込み駆動の
    TX リングを使うため、ハンドラがリングを排出せず **write が詰まってハングする**(無音/フリーズの真因)。
    → ポーリング送信で割り込み不要にして回避。送信時は SERCOM4 IRQ を NVIC で無効化。
- USB CDC へは **`USBDevice.connected()` が真のときのみ**書く。SAMD の USB CDC は未ドレイン時に
  `write` がブロックし得るため。

### Xiao (`src/ESP32C5Controller/ESP32C5Controller.ino`)

- デバッグ UART = **`Serial0` (UART0, GPIO9=TX/GPIO8=RX)**。`Serial1`(=`mainCom`, CU との UART)とは別。
- ESP32 は GPIO マトリクスで任意ピンに割当可能なため素直に実装。

## 4. 判明した原因と対策

| # | 症状 | 原因 | 対策 |
|---|---|---|---|
| 1 | Wio がログ無音・ループハング | `Serial3.write` の TX リングが SERCOM4 割り込み(Wire1 が所有)で排出されず詰まる。USB CDC `Serial.write` も未接続時ブロック | デバッグ UART を **ポーリング送信**化、USB CDC は `USBDevice.connected()` ガード |
| 2 | xiao disconnected / 取りこぼし | Wio の I2C スレーブが**単一バッファ**で、Xiao の READY 再送バーストを取りこぼし(overrun) | I2C 受信を **8段リングバッファ**化し、ループで全件ドレイン |
| 3 | PowerBoard が STANDBY/STOP に落ちる | **バランス未接続でセル電圧がフローティング → 過放電(0x10)を誤検出** | テスト時は `powerboard-control` skill で **過放電保護を無効化**(`overrideod`→`resetprot`)。※過放電は今回の通信不安定とは無関係(実運用の本対策ではない) |
| 4 | Wio の WiFi が associate 失敗 | **AP の WiFi チャンネルが RTL8720 の対応範囲外**だった | AP のチャンネルを対応 ch に変更(ベンチ側で修正済み) |
| 5 | OTA コマンドを Wio が受信しない | OTA リスナを `otaUdp.begin(port)` で開始。rpcWiFi は **port のみだと UDP を受信できない** | 参考 `greentea-ssl/WioReceiveUDP` に倣い **`otaUdp.begin(WiFi.localIP(), port)`** へ修正 |
| 6 | rpcWiFi の間欠ハング(白画面) | RTL8720 の rpcWiFi 呼び出しが稀にループをブロック(電源/USB 過渡や混雑 AP で誘発) | **ウォッチドッグ(SAMD51 WDT, ~16s)** を追加。ループが回らないと自動リセット→再接続で自己復帰。OTA ダウンロード中は WDT を蹴り続ける |

## 5. PowerBoard 操作スキル

`.claude/skills/powerboard-control/`(`powerboard_ctl.py`)。PowerBoard C6 の USB シリアル経由で:

- `off` = `stop`(SYS_PWR LOW)、`on` = C6 リセット → STANDBY(SYS_PWR HIGH)。**`on` は既定で過放電保護を
  無効化**(バランス未接続環境の自動 STOP 防止)。
- `cycle`(電源投入試験)、`status`、`od-protect on|off`、`reset-prot`、`raw "<cmd>"`。
- LogicPwr on/off の原理: `stop`→STATE_STOP(SYS_PWR LOW)。ON は STOP からアプリ内復帰経路が無いため
  **C6 リセット**で STANDBY に戻す。

## 6. 現状と残作業

- ✅ Wio: ログ安定化(ポーリング送信)、I2C リングバッファ、OTA 受信修正、ウォッチドッグ — 実装・ビルド済み。
- ✅ Wio WiFi: ch 修正後 **接続確認**(Wio IP 192.168.4.213、ping 安定)。
- ✅ Xiao: デバッグ UART ログ(USB 給電なしで起動直後ログ採取可)を確認。
- ⏳ **OTA 実証**: 受信修正は適用済みだが、**ベンチの USB が不安定**(変換器の脱落、ブートローダの
  mass storage I/O ハング等)で修正版の書込み/検証が難航中。安定したケーブル/ポートで `.uf2` を
  「Arduino」ドライブへドラッグ&ドロップ(または bossac/arduino-cli)で書込み後、unicast OTA で実証予定。

### 書込み・検証メモ

- ビルド: `src/WioDisplay/build.sh`(`.uf2` も生成) / `src/ESP32C5Controller/build.sh`。
- OTA 検証: PC で HTTP サーバ(firmware.bin 配信)を立て、UDP `0x30 + "http://<PC>:<port>/firmware.bin"`
  を Wio の **41000** へ送る(`ota_tool.py` 相当)。Wio が HTTP GET で取得 → flash → 再起動。
- OTA 後の WiFi heartbeat: Wio は `WIFI-HB: conn=.. ip=.. otaListen=.. mdns=..` を 3 秒周期で出力(診断用)。
