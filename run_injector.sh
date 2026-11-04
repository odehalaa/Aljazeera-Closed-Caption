#!/usr/bin/env bash
# Interactive CEA-608 + CEA-708 caption injector launcher.
set -euo pipefail
cd "$(dirname "$0")"

ask() {
  # ask VAR "Prompt" "default"
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
  # ask_choice VAR "Prompt" "default" opt1 opt2 ...
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

if [[ ! -t 0 ]]; then
  echo "Need an interactive terminal. Run: ./run_injector.sh" >&2
  exit 1
fi

echo "========================================"
echo "  CC Injector — setup"
echo "========================================"
echo

ask IN "Input UDP URL" \
  "udp://239.195.1.17:4900?localaddr=192.168.1.20&reuse=1&fifo_size=120000&buffer_size=65536&overrun_nonfatal=1&timeout=5000000"

ask OUT "Output UDP URL" \
  "udp://239.195.11.11:4900?pkt_size=1316&ttl=16&localaddr=192.168.1.20"

# Listen on all interfaces so another PC can send captions to this machine:54001
# Local senders still use 127.0.0.1:54001
CC_UDP="0.0.0.0:54001"
echo "Caption UDP: $CC_UDP  (accepts LAN → this_host:54001)"
echo "  This machine IP: $(hostname -I 2>/dev/null | awk '{print $1}')"
echo "  From other PC:  ./run_live_type.sh $(hostname -I 2>/dev/null | awk '{print $1}')"

ask_choice CC "Closed caption mode" "both" 608 708 both

ask_choice PEN_SIZE "CEA-708 pen size" "large" small standard large

ask FONT "CEA-708 font 0=default 1=mono 2=prop 3=mono_sans 4=casual 5=cursive 6=small_caps 7=prop_sans" "4"

ask_choice COLOR "Text color" "yellow" white green blue cyan red yellow magenta

ask_choice ROWS "Visible rows (CEA-608 max is 4; match live_type)" "2" 2 3 4
ask WIDTH "Max chars per line (8-32)" "32"
ask TIMEOUT_MS "Idle clear timeout (ms, 0=off)" "6000"
ask BASE_ROW "CEA-608 base row 1-15 (15=bottom)" "15"

ask_choice BOOTSTRAP "Bootstrap 'CC ONLINE' on start" "0" 0 1

echo
echo "---------- summary ----------"
echo "IN:        $IN"
echo "OUT:       $OUT"
echo "CC UDP:    $CC_UDP"
echo "cc:        $CC"
echo "pen_size:  $PEN_SIZE"
echo "font:      $FONT"
echo "color:     $COLOR"
echo "rows:      $ROWS  width: $WIDTH  timeout_ms: $TIMEOUT_MS  row: $BASE_ROW"
echo "bootstrap: $BOOTSTRAP"
echo "-----------------------------"
echo
read -r -p "Start injector now? [Y/n]: " GO || true
GO="${GO:-Y}"
if [[ ! "$GO" =~ ^[Yy]$ ]]; then
  echo "Cancelled."
  exit 0
fi

if [[ ! -x ./cc_injector ]]; then
  echo "Building cc_injector..."
  g++ -std=c++17 -O2 cc_injector.cpp \
    $(pkg-config --cflags --libs libavformat libavcodec libavutil libswresample) \
    -o cc_injector
fi

if pgrep -x cc_injector >/dev/null 2>&1; then
  echo "Stopping existing cc_injector..."
  pkill -x cc_injector || true
  sleep 1
fi

echo
echo "Starting... (Ctrl+C to stop)"
echo "VLC: Subtitle → CC1 (608) or DTVCC / Service 1 (708)"
echo "Live type: ./run_live_type.sh"
echo

exec nice -n -5 ./cc_injector "$IN" "$OUT" \
  --cc-udp="$CC_UDP" \
  --rows="$ROWS" --width="$WIDTH" --timeout_ms="$TIMEOUT_MS" --row="$BASE_ROW" \
  --cc="$CC" --pen_size="$PEN_SIZE" --font="$FONT" \
  --fast --color="$COLOR" --bootstrap="$BOOTSTRAP"
