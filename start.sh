#!/usr/bin/env bash
# start.sh — run the full Al Jazeera Closed Caption stack from config.json
#
#   ./start.sh
#   ./start.sh ./config.json
#   YT_URL='https://www.youtube.com/watch?v=XXXX' ./start.sh
#
# Starts:
#   1) cc_injector  (MPEG-TS in → captions → MPEG-TS out, UDP :54001)
#   2) youtube_eye  (YouTube on-screen CC → UDP + web monitor :54003)
#
# Logs:
#   /tmp/cc_injector.log
#   /tmp/youtube_eye.log
#
# Stop: Ctrl+C  or  ./stop.sh
#
set -euo pipefail
cd "$(dirname "$0")"
ROOT="$(pwd)"

CONFIG="${CC_CONFIG:-./config.json}"
if [[ "${1-}" == *.json || "${1-}" == */*.json ]]; then
  CONFIG="$1"
  shift || true
fi
export CC_CONFIG="$CONFIG"

if [[ ! -f "$CONFIG" ]]; then
  echo "[start] config not found: $CONFIG" >&2
  exit 1
fi

json_get() { python3 "$ROOT/cc_config.py" "$CONFIG" --env "$1"; }
cfg_val() { python3 "$ROOT/cc_config.py" "$CONFIG" "$1" 2>/dev/null || true; }

IN="$(json_get IN)"
OUT="$(json_get OUT)"
ROWS="$(json_get ROWS)"
ROW="$(json_get ROW)"
WEB_PORT="$(json_get DEMO_WEB_PORT)"
YT_URL="${YT_URL:-$(cfg_val youtube.url)}"
POLL="${YT_POLL:-$(cfg_val youtube.poll_sec_direct)}"
POLL="${POLL:-0.25}"
if [[ -z "$YT_URL" ]]; then
  echo "[start] youtube.url missing in $CONFIG" >&2
  exit 1
fi

INJECTOR_LOG="${INJECTOR_LOG:-/tmp/cc_injector.log}"
EYE_LOG="${EYE_LOG:-/tmp/youtube_eye.log}"

cleanup() {
  echo
  echo "[start] stopping…"
  pkill -f "youtube_player_cc.py" 2>/dev/null || true
  pkill -f "chrome-headless-shell" 2>/dev/null || true
  # Only kill injector we started if still our child pattern — stop all local injectors
  pkill -x cc_injector 2>/dev/null || true
  fuser -k "${WEB_PORT}/tcp" 2>/dev/null || true
  echo "[start] stopped"
}
trap cleanup EXIT INT TERM

echo "[start] ══════════════════════════════════════════"
echo "[start] Al Jazeera Closed Caption — full stack"
echo "[start] config : $CONFIG"
echo "[start] IN     : $IN"
echo "[start] OUT    : $OUT"
echo "[start] YouTube: $YT_URL"
echo "[start] rows=$ROWS row=$ROW poll=${POLL}s"
echo "[start] ══════════════════════════════════════════"

# --- stop previous ---
echo "[start] cleaning old processes…"
pkill -f "youtube_player_cc.py" 2>/dev/null || true
pkill -f "chrome-headless-shell" 2>/dev/null || true
pkill -x cc_injector 2>/dev/null || true
fuser -k "${WEB_PORT}/tcp" 2>/dev/null || true
sleep 1

# --- build injector if needed ---
if [[ ! -x "$ROOT/cc_injector" ]]; then
  echo "[start] building cc_injector…"
  g++ -std=c++17 -O2 "$ROOT/cc_injector.cpp" \
    $(pkg-config --cflags --libs libavformat libavcodec libavutil libswresample) \
    -o "$ROOT/cc_injector"
fi

# --- injector (background) ---
echo "[start] starting cc_injector → $INJECTOR_LOG"
: >"$INJECTOR_LOG"
# run without exec so we can background; duplicate start_injector logic via script
bash -c "cd '$ROOT' && ./start_injector.sh '$CONFIG'" >>"$INJECTOR_LOG" 2>&1 &
for i in $(seq 1 30); do
  if pgrep -x cc_injector >/dev/null; then
    break
  fi
  sleep 0.25
done
if ! pgrep -x cc_injector >/dev/null; then
  echo "[start] ERROR: cc_injector failed — see $INJECTOR_LOG" >&2
  tail -n 30 "$INJECTOR_LOG" >&2 || true
  exit 1
fi
echo "[start] cc_injector pid=$(pgrep -x cc_injector | head -1)"

# --- youtube eye + web (foreground) ---
echo "[start] starting youtube_eye → $EYE_LOG"
echo "[start] web monitor: http://$(hostname -I 2>/dev/null | awk '{print $1}'):${WEB_PORT}/"
echo "[start]             http://127.0.0.1:${WEB_PORT}/"
echo "[start] VLC: udp://@${OUT#udp://}"
echo "[start] Ctrl+C to stop everything"
echo

# Ensure venv/playwright via youtube_eye wrapper
./youtube_eye --config "$CONFIG" --url "$YT_URL" --poll "$POLL" --rows "$ROWS" "$@" 2>&1 | tee "$EYE_LOG"
