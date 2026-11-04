#!/usr/bin/env bash
# Start/restart cc_injector using config.json (env vars override JSON).
#
#   ./start_injector.sh
#   ./start_injector.sh ./config.json
#   TIMEOUT_MS=5000 ROWS=3 ./start_injector.sh
set -euo pipefail
cd "$(dirname "$0")"

CONFIG="${CC_CONFIG:-./config.json}"
if [[ "${1-}" == *.json || "${1-}" == */*.json ]]; then
  CONFIG="$1"
  shift || true
fi
export CC_CONFIG="$CONFIG"

if [[ ! -f "$CONFIG" ]]; then
  echo "[injector] config not found: $CONFIG" >&2
  exit 1
fi

json_get() { python3 ./cc_config.py "$CONFIG" --env "$1"; }

IN="${IN:-$(json_get IN)}"
OUT="${OUT:-$(json_get OUT)}"
CC_UDP="${CC_UDP:-$(json_get CC_UDP)}"
ROWS="${ROWS:-$(json_get ROWS)}"
ROW="${ROW:-$(json_get ROW)}"
WIDTH="${WIDTH:-$(json_get WIDTH)}"
TIMEOUT_MS="${TIMEOUT_MS:-$(json_get TIMEOUT_MS)}"
COLOR="${COLOR:-$(json_get COLOR)}"
FONT="${FONT:-$(json_get FONT)}"
PEN_SIZE="${PEN_SIZE:-$(json_get PEN_SIZE)}"
CC="${CC:-$(json_get CC)}"
BOOTSTRAP="${BOOTSTRAP:-$(json_get BOOTSTRAP)}"
SEI_EVERY="${SEI_EVERY:-$(json_get SEI_EVERY)}"
CLEAR_FRAMES="${CLEAR_FRAMES:-$(json_get CLEAR_FRAMES)}"

if [[ ! -x ./cc_injector ]]; then
  echo "[injector] building…"
  g++ -std=c++17 -O2 cc_injector.cpp \
    $(pkg-config --cflags --libs libavformat libavcodec libavutil libswresample) \
    -o cc_injector
fi

if pgrep -x cc_injector >/dev/null 2>&1; then
  echo "[injector] stopping existing…"
  pkill -x cc_injector || true
  sleep 1
fi

echo "[injector] config: $CONFIG"
echo "[injector] IN=$IN"
echo "[injector] OUT=$OUT"
echo "[injector] rows=$ROWS row=$ROW width=$WIDTH timeout_ms=$TIMEOUT_MS"
echo "[injector] color=$COLOR font=$FONT pen_size=$PEN_SIZE cc=$CC"
echo "[injector] captions udp://$CC_UDP"

# Note: avoid --fast here — it forces --quiet and hides [cc] recv/inject logs.
# Keep the same performance knobs explicitly so caption UDP is visible in the log.
exec nice -n -5 ./cc_injector "$IN" "$OUT" \
  --cc-udp="$CC_UDP" \
  --rows="$ROWS" --width="$WIDTH" --row="$ROW" \
  --timeout_ms="$TIMEOUT_MS" \
  --cc="$CC" --pen_size="$PEN_SIZE" --font="$FONT" \
  --color="$COLOR" --bootstrap="$BOOTSTRAP" \
  --sei_every="$SEI_EVERY" --clear_frames="$CLEAR_FRAMES" \
  --cc_every=1 --line_gap=2
