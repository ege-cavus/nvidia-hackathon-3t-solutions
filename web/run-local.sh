#!/usr/bin/env bash
# Run the dashboard on http://localhost:3100 with the DGX Spark as backend.
#   ./web/run-local.sh            (SPARK=user@host to override)
set -euo pipefail
cd "$(dirname "$0")"
SPARK="${SPARK:-user10@spark10.local}"
PORT="${PORT:-3100}"

if curl -fsS --max-time 3 localhost:8080/healthz >/dev/null 2>&1; then
  echo "tunnel: localhost:8080 already answering"
else
  echo "tunnel: opening localhost:8080 -> $SPARK:8080"
  ssh -f -N -o ExitOnForwardFailure=yes -o ServerAliveInterval=30 -L 8080:localhost:8080 "$SPARK"
  sleep 1
fi
health=$(curl -fsS --max-time 5 localhost:8080/ui/health) || { echo "Spark API not answering. On the Spark: tmux attach -t suru"; exit 1; }
echo "backend: $health"

echo "console: http://localhost:$PORT/console/  dashboard: http://localhost:$PORT   (Ctrl-C stops the web server; the tunnel keeps running: pkill -f 'L 8080:localhost:8080')"
(sleep 1; open "http://localhost:$PORT/console/" 2>/dev/null || true) &
exec python3 -m http.server "$PORT" --bind 127.0.0.1
