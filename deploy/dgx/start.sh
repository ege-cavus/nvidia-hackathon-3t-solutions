#!/usr/bin/env bash
# Start the SURU API on the DGX Spark. Model servers must already be running (see docs/CONSOLE_UPDATES.md §B3).
set -euo pipefail
cd "$(dirname "$0")/../.."

ENV_FILE="deploy/dgx/.env"
[[ -f "$ENV_FILE" ]] || { echo "Missing $ENV_FILE (copy deploy/dgx/env.example)"; exit 1; }
set -a; source "$ENV_FILE"; set +a

if [[ "${LLM_MODE:-mock}" == "live" ]]; then
  for url in "$QWEN_BASE_URL" "$NEMOTRON_BASE_URL"; do
    curl -fsS --max-time 5 "$url/models" >/dev/null \
      || { echo "Model server not answering at $url/models"; exit 1; }
  done
fi
[[ -n "${UR_API_TOKEN:-}" ]] || echo "WARNING: UR_API_TOKEN is empty; anyone reaching the tunnel can run the pipeline."

exec uv run urban-response serve --host "${SURU_HOST:-0.0.0.0}" --port "${SURU_PORT:-8080}"
