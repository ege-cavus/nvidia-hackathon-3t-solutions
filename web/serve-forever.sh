#!/usr/bin/env bash
# Always-on local dashboard: http://localhost:3100 -> DGX Spark backend through an SSH tunnel.
# Meant to be run by the LaunchAgent web/com.suru.dashboard.plist (starts at login, restarts on
# failure), so nobody has to touch a terminal: open http://localhost:3100 and press "Run full demo".
set -uo pipefail
cd "$(dirname "$0")"
SPARK="${SPARK:-user10@spark10.local}"
PORT="${PORT:-3100}"

# Tunnel keeper: reconnects whenever the link drops (Wi-Fi blip, Spark reboot).
(
  while true; do
    if ! curl -fsS --max-time 3 localhost:8080/healthz >/dev/null 2>&1; then
      pkill -f "ssh .*-L 8080:localhost:8080" 2>/dev/null
      ssh -N -o BatchMode=yes -o ExitOnForwardFailure=yes -o ServerAliveInterval=15 -o ServerAliveCountMax=2 \
          -o ConnectTimeout=10 -L 8080:localhost:8080 "$SPARK" &
    fi
    sleep 10
  done
) &
KEEPER=$!
trap 'kill $KEEPER 2>/dev/null; pkill -f "ssh .*-L 8080:localhost:8080"' EXIT

exec python3 -m http.server "$PORT" --bind 127.0.0.1
