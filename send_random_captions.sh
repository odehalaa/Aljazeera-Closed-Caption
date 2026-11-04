#!/usr/bin/env bash
# Send rolling test captions to cc_injector (stable width = less flicker)
HOST="${1:-127.0.0.1}"
PORT="${2:-54001}"
INTERVAL="${3:-1}"

WORDS=(Hello World Live Caption Test Stream News Update Breaking Sports Weather Market Report Studio Focus Alert Action Replay Score Board Panel Guest Host)

# Fixed-ish width (~28) so overwrite/roll doesn't thrash on length changes
fit_line() {
  local s="$1"
  s="${s:0:28}"
  printf '%-28s' "$s"
}

rand_line() {
  local n=$((RANDOM % 3 + 2))
  local out="" i w
  for ((i=0; i<n; i++)); do
    w="${WORDS[RANDOM % ${#WORDS[@]}]}"
    if [[ -n "$out" ]]; then out+=" "; fi
    out+="$w"
  done
  fit_line "$out"
}

echo "[caption-sender] udp://${HOST}:${PORT} every ${INTERVAL}s (stable width)"
n=0
while true; do
  n=$((n + 1))
  line="$(rand_line)"
  # Count prefix helps confirm freshness on air
  msg=$(printf 'T%03d %s' "$n" "${line:5}")
  msg="${msg:0:28}"
  printf '%s\n' "$msg" | nc -u -w0 "$HOST" "$PORT"
  echo "[$(date '+%H:%M:%S')] sent: $msg"
  sleep "$INTERVAL"
done
