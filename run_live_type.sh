#!/usr/bin/env bash
# Live-type captions to injector.
#   ./run_live_type.sh                 → this machine (127.0.0.1:54001)
#   ./run_live_type.sh 192.168.1.20    → other machine / remote injector
#   ./run_live_type.sh demo            → RSS demo (local)
#   ./run_live_type.sh demo 192.168.1.20
set -euo pipefail
cd "$(dirname "$0")"

MODE="interactive"
HOST="127.0.0.1"
PORT="54001"

if [[ "${1-}" == "demo" ]]; then
  MODE="demo"
  shift || true
fi

# Optional injector host (IP or hostname)
if [[ -n "${1-}" && "${1-}" != -* ]]; then
  HOST="$1"
  shift || true
fi

ask() {
  local _var="$1" _prompt="$2" _def="${3-}" _ans
  if [[ -n "$_def" ]]; then
    read -r -p "$_prompt [$_def]: " _ans || true
    _ans="${_ans:-$_def}"
  else
    read -r -p "$_prompt: " _ans || true
  fi
  printf -v "$_var" '%s' "$_ans"
}

ask_choice() {
  local _var="$1" _prompt="$2" _def="$3"
  shift 3
  local _opts=("$@") _ans _ok=0
  echo "$_prompt"
  local i=1
  for o in "${_opts[@]}"; do
    if [[ "$o" == "$_def" ]]; then
      echo "  $i) $o  (default)"
    else
      echo "  $i) $o"
    fi
    ((i++)) || true
  done
  read -r -p "Choice [$_def]: " _ans || true
  if [[ -z "$_ans" ]]; then
    _ans="$_def"
  elif [[ "$_ans" =~ ^[0-9]+$ ]] && (( _ans >= 1 && _ans <= ${#_opts[@]} )); then
    _ans="${_opts[$((_ans - 1))]}"
  fi
  for o in "${_opts[@]}"; do
    [[ "$o" == "$_ans" ]] && _ok=1 && break
  done
  if [[ $_ok -ne 1 ]]; then
    echo "Invalid choice, using default: $_def" >&2
    _ans="$_def"
  fi
  printf -v "$_var" '%s' "$_ans"
}

if [[ "$MODE" == "demo" ]]; then
  echo "========================================"
  echo "  Live type — RSS DEMO"
  echo "========================================"
  echo
  echo "Target:  udp://${HOST}:${PORT}"
  echo

  if [[ ! -t 0 ]]; then
    ROWS=2; WIDTH=32; TIMEOUT_MS=6000; CPS=14; PAUSE=1.2; LOOP=1
    FEED_URL="https://feeds.bbci.co.uk/news/rss.xml"
  else
    ask_choice ROWS "Rows (must match injector)" "2" 2 3 4
    ask WIDTH "Width (match injector)" "32"
    ask TIMEOUT_MS "Idle clear timeout ms" "6000"
    ask FEED_URL "RSS feed URL" "https://feeds.bbci.co.uk/news/rss.xml"
    ask CPS "Typing speed (chars/sec)" "14"
    ask PAUSE "Pause after each Enter (sec)" "1.2"
    ask_choice LOOP "Loop forever (refetch)" "1" 1 0
  fi

  LOOP_FLAG=()
  [[ "$LOOP" == "1" ]] && LOOP_FLAG=(--loop)

  if [[ -t 0 ]]; then
    read -r -p "Start RSS demo now? [Y/n]: " GO || true
    GO="${GO:-Y}"
    [[ "$GO" =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }
  fi

  exec ./rss_demo.py "$HOST" "$PORT" \
    --feed "$FEED_URL" \
    --rows="$ROWS" --width="$WIDTH" --timeout-ms="$TIMEOUT_MS" \
    --cps="$CPS" --pause="$PAUSE" \
    "${LOOP_FLAG[@]}"
fi

if [[ ! -t 0 ]]; then
  echo "Need an interactive terminal." >&2
  echo "  Local:  ./run_live_type.sh" >&2
  echo "  Remote: ./run_live_type.sh 192.168.1.20" >&2
  exit 1
fi

echo "========================================"
echo "  Live type — setup"
echo "========================================"
echo
echo "Target:  udp://${HOST}:${PORT}"
if [[ "$HOST" == "127.0.0.1" || "$HOST" == "localhost" ]]; then
  echo "Tip:     From another PC on the LAN:"
  echo "           ./run_live_type.sh 192.168.1.20"
  echo "         (injector must listen on 0.0.0.0:54001)"
fi
echo

ask_choice ROWS "Rows (must match injector)" "2" 2 3 4
ask WIDTH "Width (match injector, usually 32)" "32"
ask TIMEOUT_MS "Idle clear timeout ms (match injector)" "6000"

echo
echo "---------- summary ----------"
echo "Target:  udp://${HOST}:${PORT}"
echo "rows:    $ROWS   width: $WIDTH   timeout_ms: $TIMEOUT_MS"
echo "-----------------------------"
echo

read -r -p "Start live typing now? [Y/n]: " GO || true
GO="${GO:-Y}"
[[ "$GO" =~ ^[Yy]$ ]] || { echo "Cancelled."; exit 0; }

echo
exec ./live_type.py "$HOST" "$PORT" \
  --rows="$ROWS" --width="$WIDTH" --timeout-ms="$TIMEOUT_MS" \
  --coalesce-ms=0
