#!/bin/sh
# Live ビュワー用 gtnlv-rpid 起動: tmpfs 出力 + in-memory ring buffer 5 min。
# SD カードに書かず、永続データは残らない (= ビュワー専用、記録なし)。
#
# Usage:
#   tools/run_live.sh                    # default: robot-id 0、無限ラン
#   tmux new -d -s live tools/run_live.sh    # tmux 永続化
#
# 出力先: /dev/shm/gtnlv_live/<HHMMSS>/  (tmpfs、RasPi 再起動で消失)
# dashboard 側は OUT_GLOB で `/dev/shm/gtnlv_live/*` も拾うので自動表示。

set -eu

ROBOT_ID="${ROBOT_ID:-0}"
SNIFFER_PORT="${SNIFFER_PORT:-/dev/ttyUSB0}"
PPS_DEVICE="${PPS_DEVICE:-/dev/pps0}"
KEEP_SEC="${KEEP_SEC:-300}"   # 5 min

TAG="live_$(date +%H%M%S)"
OUT="/dev/shm/gtnlv_live/${TAG}"
mkdir -p "${OUT}"

# tmpfs の親 dir を確保 (/dev/shm は tmpfs)
ln -sfn "${OUT}" "/dev/shm/gtnlv_live/latest" 2>/dev/null || true

echo "[run_live] starting gtnlv-rpid (live viewer mode)"
echo "  TAG       = ${TAG}"
echo "  OUT       = ${OUT}    (tmpfs、RasPi 再起動で消失)"
echo "  keep      = ${KEEP_SEC} sec (file は常に直近 ${KEEP_SEC}s 分のみ)"
echo "  sniffer   = ${SNIFFER_PORT}"
echo "  pps       = ${PPS_DEVICE}"
echo

exec python3 -u ~/gtnlv/gtnlv_rpid.py \
  --robot-ids "${ROBOT_ID}" \
  --duration 0 \
  --keep-recent-s "${KEEP_SEC}" \
  --sniffer-port "${SNIFFER_PORT}" \
  --sniffer-baud 2000000 \
  --pps-device "${PPS_DEVICE}" \
  --out-dir "${OUT}"
