#!/bin/zsh
# Run the session server against a simulated headset on a pseudo-terminal.
#   scripts/serve_pty_demo.sh <http-port> <out-dir> [hang-up-after-seconds]
# Leaves both processes running; stop the server with
#   lsof -ti tcp:<http-port> | xargs kill
# and the simulator with the pid printed on the first line.
# Never touches a real serial port.
set -u
root="${0:A:h:h}"
http="$1"
out="$2"
hang="${3:-}"
log="${TMPDIR:-/tmp}/oc-demo-$http"
mkdir -p "$log"
if [[ -n "$hang" ]]; then
  "$root/.venv/bin/python" "$root/scripts/run_pty_device.py" "$hang" > "$log/device.txt" 2>&1 &
else
  "$root/.venv/bin/python" "$root/scripts/run_pty_device.py" > "$log/device.txt" 2>&1 &
fi
echo "simulator pid $!"
sleep 2
pty="$(head -1 "$log/device.txt")"
echo "device $pty"
OPEN_COGNIONICS_OUT="$out" "$root/target/debug/open-cognionics" "$pty" "$http" > "$log/server.txt" 2>&1 &
echo "server pid $! (log $log/server.txt)"
