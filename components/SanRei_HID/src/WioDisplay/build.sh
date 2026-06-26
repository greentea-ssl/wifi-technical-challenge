#!/bin/sh
# Remove old build artifacts
rm -f ./WioDisplay.ino.*

# Create config.h if it doesn't exist
if [ ! -e config.h ]; then
  touch config.h
fi

# Compile for WioTerminal
# Note: バージョン文字列はソース側 #define WIO_VERSION "LOCAL" が常に使われる。
# CI のリリース時のみワークフローが ./WioDisplay.ino を sed で書き換えてから
# このスクリプトを呼ぶ (.github/workflows/auto-release.yml 参照)。
arduino-cli compile -b Seeeduino:samd:seeed_wio_terminal ./WioDisplay.ino --output-dir ./

# Convert to UF2 format
python3 /utils/uf2/utils/uf2conv.py -c -b 0x4000 -o ./WioDisplay.ino.uf2 ./WioDisplay.ino.bin

echo "Build completed: WioDisplay.ino.uf2"

# CI: コンテナは root でビルドするため、生成物 (build/ 等) をマウント元の所有者に戻す。
# self-hosted runner で actions/checkout の clean が root 所有ファイルを削除できず
# EACCES になるのを防ぐ (ローカル docker では no-op)。
chown -R "$(stat -c '%u:%g' /WioDisplay)" /WioDisplay 2>/dev/null || true
