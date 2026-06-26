# Linux ローカルビルド手順

ESP32C5Controller / WioDisplay の両方を **Linux ホスト上でビルド・書き込み**するための手順をまとめる。次の 2 経路を併記する:

- [§A. arduino-cli ローカルビルド (推奨・実運用)](#a-arduino-cli-ローカルビルド-推奨実運用)
- [§B. Arduino IDE 2.x GUI ビルド (代替)](#b-arduino-ide-2x-gui-ビルド-代替)

CI (`.github/workflows/build_check.yml`) は Docker (`src/*/Dockerfile`) で `arduino-cli` を回す構成。本ドキュメントはホスト直接実行版なので、`docker compose run build` を使う場合は各 `Dockerfile` / `build.sh` を参照すること。

---

## 0. 前提環境

| 項目 | 推奨 |
|------|------|
| OS | Ubuntu 22.04 / 24.04 など glibc 2.35+ の x86_64 |
| Python | 3.x (Wio の `.uf2` 変換用) |
| ユーザ | `dialout` グループ所属 (シリアルポート権限) |

```bash
sudo usermod -a -G dialout $USER
# 反映には再ログインが必要
```

---

## A. arduino-cli ローカルビルド (推奨・実運用)

実際の手元ビルドおよび OTA 用 `.bin` 生成はこの経路で行ってきた。リポジトリ同梱バイナリを使う前提で記述する。

### A.1 arduino-cli バイナリ

リポジトリ直下に **Linux 用** `arduino-cli` (v1.4.1) と **Windows 用** `arduino-cli.exe` を同梱している:

```
SanRei_HID/
├── arduino-cli       # Linux x86_64 (本ドキュメントで使う)
└── arduino-cli.exe   # Windows
```

PATH を通すか、絶対パスで実行する:

```bash
# 例: 一時的に PATH に追加
export REPO=/home/$USER/Documents/GitHub/sanrei/SanRei_HID
export PATH=$REPO:$PATH
arduino-cli version
# => arduino-cli  Version: 1.4.1 ...
```

> 別バージョンを使いたい場合は `curl -fsSL https://raw.githubusercontent.com/arduino/arduino-cli/master/install.sh | BINDIR=$HOME/.local/bin sh` で個別導入してもよい。

### A.1.5 サブモジュールの初期化

本リポジトリは `robot_comm_spec` (greentea-ssl/robot_comm_spec, v1.0.0) を Git サブモジュールとして参照している。クローン直後に必ず実行する:

```bash
cd "$REPO"
git submodule update --init --recursive
```

サブモジュールが取得されないと、CAN/I2C 仕様の参照ドキュメント (`robot_comm_spec/CAN_LS.md` など) が空ディレクトリになるだけで、ビルド自体は通る (現状ソースコードはサブモジュールの内容に直接依存しない)。CI/CD では `SUBMODULE_TOKEN` を使った PAT 注入方式で取得しているが、ローカルでは public submodule なので素朴な `git submodule update --init --recursive` で十分。

### A.2 ボードコアのインストール

ESP32 と Seeeduino の追加 URL を登録した上で、両コアを入れる。

```bash
arduino-cli config init  # 初回のみ。既に ~/.arduino15/arduino-cli.yaml があれば不要

arduino-cli config add board_manager.additional_urls \
    https://espressif.github.io/arduino-esp32/package_esp32_index.json \
    https://files.seeedstudio.com/arduino/package_seeeduino_boards_index.json

arduino-cli core update-index
arduino-cli core install esp32:esp32                # XIAO ESP32C5 用
arduino-cli core install Seeeduino:samd             # Wio Terminal 用
arduino-cli core install arduino:samd               # SAMD51 共通依存
```

確認:

```bash
arduino-cli core list
# ID             Installed Latest Name
# esp32:esp32    3.3.8     ...    esp32
# Seeeduino:samd 1.8.5     ...    Seeed SAMD Boards
```

> 本リポジトリの想定動作実績は `esp32:esp32 3.3.x` / `Seeeduino:samd 1.8.5`。

### A.3 ライブラリ

WioDisplay は `rpcWiFi` 系 / `ArduinoOTA` (JAndrassy fork, SAMD51 `InternalStorage` 対応) など **リポジトリ同梱の独自ライブラリ** に依存する。Library Manager の公式 `ArduinoOTA` は SAMD51 OTA 非対応なので使わない。

CLI ビルドでは `--libraries` フラグで `libraries/` を直接指定するのが最もシンプル:

```bash
arduino-cli compile \
    --fqbn Seeeduino:samd:seeed_wio_terminal \
    --libraries "$REPO/libraries" \
    "$REPO/src/WioDisplay"
```

それ以外に必要な公式ライブラリ:

```bash
arduino-cli lib install "TFT_eSPI"
```

ESP32C5Controller 側は ESP32 コア同梱ライブラリのみで完結する (追加インストール不要)。

### A.4 ビルド (ESP32C5Controller)

```bash
cd "$REPO/src/ESP32C5Controller"
[ -e config.h ] || touch config.h   # 初回のみ

arduino-cli compile \
    -b esp32:esp32:XIAO_ESP32C5 \
    --output-dir ./ \
    ./ESP32C5Controller.ino
```

成果物 (リポジトリ既存運用と同名):

```
ESP32C5Controller.ino.bin
ESP32C5Controller.ino.elf
ESP32C5Controller.ino.bootloader.bin
ESP32C5Controller.ino.partitions.bin
ESP32C5Controller.ino.merged.bin
```

バージョン文字列を上書きする場合 (CI / `build.sh` 互換):

```bash
FW_VERSION=3.0.2 ./build.sh
# 内部的には -DCONTROLLER_VERSION="\"3.0.2\"" を渡している
```

### A.5 ビルド (WioDisplay)

```bash
cd "$REPO/src/WioDisplay"
[ -e config.h ] || touch config.h

arduino-cli compile \
    -b Seeeduino:samd:seeed_wio_terminal \
    --libraries "$REPO/libraries" \
    --output-dir ./ \
    ./WioDisplay.ino

# UF2 変換 (リカバリ書き込みやマスストレージ経由のドラッグ&ドロップ用)
python3 "$REPO/utils/uf2/utils/uf2conv.py" -c -b 0x4000 \
    -o ./WioDisplay.ino.uf2 ./WioDisplay.ino.bin
```

`build.sh` を使えば上記をまとめて実行できる:

```bash
FW_VERSION=2.0.1 ./build.sh
```

### A.6 シリアル経由の書き込み

```bash
# ポート確認
arduino-cli board list

# ESP32C5
arduino-cli upload -p /dev/ttyACM0 \
    -b esp32:esp32:XIAO_ESP32C5 \
    "$REPO/src/ESP32C5Controller"

# Wio Terminal
arduino-cli upload -p /dev/ttyACM0 \
    -b Seeeduino:samd:seeed_wio_terminal \
    "$REPO/src/WioDisplay"
```

Wio Terminal がブートローダモードに入っている場合 (電源スイッチを左に2回素早くスライドして LED が脈動する状態) は、`Arduino` というUSBマスストレージとしてマウントされるので `.uf2` をドラッグ&ドロップでも書き込める。

### A.7 OTA 経由の書き込み

USB を使わず WiFi 経由で更新する場合は付属の `ota_tool.py` (GUI) または `tools/ota_cli.py` 相当の CLI を使う。詳細は [`README.md` §OTA アップデート](../README.md#ota-アップデート) を参照。

---

## B. Arduino IDE 2.x GUI ビルド (代替)

GUI ベースで触りたい場合の手順。IDE 側でも同じコア / 同じライブラリ群に依存するので、§A.2 / §A.3 の登録はそのまま IDE にも効く (`~/.arduino15/` を共有しているため)。

### B.1 Arduino IDE のインストール

公式の AppImage が手軽:

```bash
mkdir -p ~/Apps && cd ~/Apps
# https://www.arduino.cc/en/software から arduino-ide_*.AppImage を取得
chmod +x arduino-ide_*_Linux_64bit.AppImage
./arduino-ide_*_Linux_64bit.AppImage
```

AppImage 実行に `libfuse2` が必要な場合: `sudo apt install libfuse2`。

> apt の `arduino` パッケージは 1.8 系で本リポジトリの依存ライブラリ構成と相性が悪いので非推奨。

### B.2 ボード定義の追加

`File` → `Preferences` → **Additional boards manager URLs** に以下 2 つをカンマ区切りで追加:

```
https://espressif.github.io/arduino-esp32/package_esp32_index.json
https://files.seeedstudio.com/arduino/package_seeeduino_boards_index.json
```

`Tools` → `Board` → **Boards Manager** から以下をインストール:

| パッケージ | 用途 |
|------------|------|
| **esp32 by Espressif Systems** | XIAO ESP32C5 |
| **Seeeduino SAMD Boards** | Wio Terminal |
| **Arduino SAMD Boards** | SAMD51 共通依存 |

> §A.2 で `arduino-cli` から既にインストール済みなら、IDE 側にもそのまま反映される (両者が `~/.arduino15/` を共有するため)。

### B.3 ライブラリのリンク

IDE は CLI のような `--libraries` フラグを持たないので、リポジトリ同梱の `libraries/` をスケッチブック (`~/Arduino/libraries`) からシンボリックリンクで参照させる。

```bash
mkdir -p ~/Arduino/libraries
cd ~/Arduino/libraries

REPO=/home/$USER/Documents/GitHub/sanrei/SanRei_HID

ln -s "$REPO/libraries/ArduinoOTA"               ArduinoOTA
ln -s "$REPO/libraries/FlashStorage"             FlashStorage
ln -s "$REPO/libraries/Seeed_Arduino_FS"         Seeed_Arduino_FS
ln -s "$REPO/libraries/Seeed_Arduino_SFUD"       Seeed_Arduino_SFUD
ln -s "$REPO/libraries/Seeed_Arduino_mbedtls"    Seeed_Arduino_mbedtls
ln -s "$REPO/libraries/Seeed_Arduino_rpcUnified" Seeed_Arduino_rpcUnified
ln -s "$REPO/libraries/Seeed_Arduino_rpcWiFi"    Seeed_Arduino_rpcWiFi
ln -s "$REPO/libraries/Seeed_Arduino_rpcmDNS"    Seeed_Arduino_rpcmDNS
```

シンボリックリンクにしておくと `git pull` でライブラリ更新を自動追従できる。

加えて IDE の Library Manager から:

| ライブラリ | 用途 |
|------------|------|
| **TFT_eSPI** (Bodmer) | Wio Terminal の TFT 描画 |

をインストールする。

### B.4 スケッチを開く

別ウィンドウで開く運用が安全:

- `File` → `Open…` → `src/ESP32C5Controller/ESP32C5Controller.ino`
- `File` → `Open…` → `src/WioDisplay/WioDisplay.ino`

`config.h` がない場合は空ファイルとして作成 (CLI と同じ):

```bash
touch src/ESP32C5Controller/config.h
touch src/WioDisplay/config.h
```

### B.5 ボード選択とビルド・書き込み

| 対象 | Board メニュー | Port |
|------|----------------|------|
| ESP32C5Controller | `esp32` → **XIAO_ESP32C5** | `/dev/ttyACM*` |
| WioDisplay | `Seeeduino SAMD` → **Seeeduino Wio Terminal** | `/dev/ttyACM*` |

- ビルド: `Sketch` → `Verify/Compile` (Ctrl+R)
- 書き込み: `Sketch` → `Upload` (Ctrl+U)
- `.bin` を取り出すとき: `Sketch` → **Export Compiled Binary** (Ctrl+Alt+S) → `build/<fqbn>/...ino.bin` が生成される。OTA に流す場合はこれを使う。
- Wio の `.uf2` 化は §A.5 のコマンドを別途実行する。

### B.6 バージョン文字列の埋め込み

CLI / `build.sh` は `FW_VERSION` 環境変数からマクロ注入できるが、IDE にはこの仕組みがないので **スケッチ内 `#define` を直接書き換える** のが確実:

- `ESP32C5Controller.ino` の `#define CONTROLLER_VERSION "x.y.z"`
- `WioDisplay.ino` の `#define WIO_VERSION "x.y.z"`

(`platform.local.txt` で吸収する手もあるが GUI からは扱いにくいので非推奨。)

---

## C. CLI / Docker / CI との対応関係

| 操作 | §A arduino-cli ローカル | §B Arduino IDE | Docker (CI と同等) |
|------|--------------------------|----------------|---------------------|
| ESP32C5 ビルド | `arduino-cli compile -b esp32:esp32:XIAO_ESP32C5 ./` | Verify (Ctrl+R) | `cd src/ESP32C5Controller && docker compose run build` |
| Wio ビルド | `arduino-cli compile -b Seeeduino:samd:seeed_wio_terminal --libraries $REPO/libraries ./` | Verify (Ctrl+R) | `cd src/WioDisplay && docker compose run build` |
| Wio UF2 化 | `python3 utils/uf2/utils/uf2conv.py …` | 同左 (手動) | `build.sh` 内で自動 |
| ライブラリ解決 | `--libraries $REPO/libraries` | `~/Arduino/libraries` シンボリックリンク | `docker-compose.yml` の `../../libraries:/root/Arduino/libraries:ro` マウント |
| バージョン注入 | `FW_VERSION=x.y.z ./build.sh` | `#define` 直接編集 | `FW_VERSION=x.y.z ./build.sh` (CI が渡す) |

---

## D. シリアルポート関連

| 症状 | 対処 |
|------|------|
| `Permission denied: '/dev/ttyACM0'` | `dialout` グループ所属 + 再ログイン |
| ポートが見えない | `dmesg \| tail` で USB 認識を確認、ケーブル交換 |
| Wio で書き込み失敗 | 電源スイッチを左に2回スライドしてブートローダモードへ |
| ESP32C5 で `Failed to connect` | `Tools` → `USB CDC On Boot` が `Enabled` になっているか確認 |
| `ModemManager` が干渉 | `sudo systemctl stop ModemManager` (永続無効化は環境次第) |

---

## E. トラブルシュート

- **`fatal error: rpcWiFi.h: No such file or directory`**
  → CLI なら `--libraries` 未指定または値ミス。GUI なら §B.3 のシンボリックリンク不足。`Seeed_Arduino_rpcWiFi` だけでなく `rpcUnified` / `mbedtls` / `Seeed_Arduino_FS` / `SFUD` / `rpcmDNS` も全て必要。

- **`error: 'class ArduinoOTAClass' has no member named 'begin'` 等の SAMD51 OTA 関連エラー**
  → Library Manager の公式 `ArduinoOTA` が優先されている。`~/Arduino/libraries/ArduinoOTA` がリポジトリ同梱版へのリンクになっていることを確認 (Library Manager 版があれば削除)。CLI で `--libraries` を使う場合も同様にスケッチブック側に古い `ArduinoOTA` が残っていないか注意。

- **`Multiple libraries were found for "WiFi.h"`**
  → ESP32 用ビルド時に Wio 用 `rpcWiFi` と衝突するメッセージが出ることがあるが、選択ログ末尾で `Used: .../arduino-esp32/...` ならビルド自体は問題なし。

- **`fqbn: esp32:esp32:XIAO_ESP32C5 not found`**
  → ESP32 コアが古い (3.x 未満)。`arduino-cli core upgrade esp32:esp32` または IDE Boards Manager から最新版に更新。

- **Wio 書き込み後に何も表示されない**
  → ブートローダモードで止まっている可能性あり。電源スイッチを左に1回スライドしてリセット。

- **`./arduino-cli: cannot execute binary file`**
  → リポジトリ同梱の Linux 版バイナリが ARM ホストや非互換 glibc 環境で動かない場合。`curl … install.sh` で別途インストールする。
