#!/bin/sh
# Remove old build artifacts
rm -f ./ESP32C5Controller.ino.*

# Create config.h if it doesn't exist
if [ ! -e config.h ]; then
  touch config.h
fi

# Compile for XIAO ESP32C5
# Note: バージョン文字列はソース側 #define CONTROLLER_VERSION "LOCAL" が常に使われる。
# CI のリリース時のみワークフローが ./ESP32C5Controller.ino を sed で書き換えてから
# このスクリプトを呼ぶ (.github/workflows/auto-release.yml 参照)。
arduino-cli compile -b esp32:esp32:XIAO_ESP32C5 ./ESP32C5Controller.ino --output-dir ./

echo "Build completed: ESP32C5Controller.ino.bin"

# CI: コンテナは root でビルドするため、生成物 (build/ 等) をマウント元の所有者に戻す。
# これをしないと self-hosted runner で actions/checkout の clean が root 所有ファイルを
# 削除できず EACCES になる (ローカル docker では no-op)。
chown -R "$(stat -c '%u:%g' /ESP32C5Controller)" /ESP32C5Controller 2>/dev/null || true
