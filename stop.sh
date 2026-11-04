#!/usr/bin/env bash
# stop.sh — stop the full stack started by ./start.sh
set -euo pipefail
cd "$(dirname "$0")"

WEB_PORT=54003
if [[ -f "${CC_CONFIG:-./config.json}" ]]; then
  WEB_PORT="$(python3 ./cc_config.py "${CC_CONFIG:-./config.json}" --env DEMO_WEB_PORT 2>/dev/null || echo 54003)"
fi

echo "[stop] youtube_eye / chromium…"
pkill -f "youtube_player_cc.py" 2>/dev/null || true
pkill -f "chrome-headless-shell" 2>/dev/null || true
fuser -k "${WEB_PORT}/tcp" 2>/dev/null || true

echo "[stop] cc_injector…"
pkill -x cc_injector 2>/dev/null || true

sleep 0.5
echo "[stop] done"
pgrep -a cc_injector 2>/dev/null || echo "  (no injector)"
pgrep -af youtube_player_cc 2>/dev/null || echo "  (no youtube_eye)"
