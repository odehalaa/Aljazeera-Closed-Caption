#!/usr/bin/env bash
# HTTP gateway so other machines send captions without UDP.
# Injector still uses local UDP; this machine only needs TCP 54002 open.
set -euo pipefail
cd "$(dirname "$0")"

LISTEN="${LISTEN:-0.0.0.0:54002}"
CC_UDP="${CC_UDP:-127.0.0.1:54001}"
IP="$(hostname -I 2>/dev/null | awk '{print $1}')"

if ! pgrep -x cc_injector >/dev/null 2>&1; then
  echo "Warning: cc_injector not running — start ./run_injector.sh first" >&2
fi

# replace previous gateway if any
pkill -f 'caption_http.py' 2>/dev/null || true
sleep 0.3

echo "HTTP captions:  http://${IP}:54002/caption"
echo "From other PC:"
echo "  curl -d 'HELLO WORLD' http://${IP}:54002/caption"
echo "  curl -d 'line one|line two' http://${IP}:54002/caption"
echo "  curl -d 'CLEAR' http://${IP}:54002/caption"
echo
echo "Interactive typing from other PC (SSH, not UDP):"
echo "  ssh -t root@${IP} 'cd /root/CC_608 && ./run_live_type.sh'"
echo

exec python3 ./caption_http.py --listen="$LISTEN" --cc-udp="$CC_UDP"
