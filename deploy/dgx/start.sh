#!/usr/bin/env bash
# Start the SURU API on the DGX Spark. Model servers must already be running (see docs/DEPLOYMENT.md).
set -euo pipefail
cd "$(dirname "$0")/../.."

ENV_FILE="deploy/dgx/.env"
[[ -f "$ENV_FILE" ]] || { echo "Missing $ENV_FILE (copy deploy/dgx/env.example)"; exit 1; }
set -a; source "$ENV_FILE"; set +a

if [[ "${LLM_MODE:-mock}" == "live" ]]; then
  check() {  # base_url model
    local body
    body=$(curl -fsS --max-time 5 "$1/models") || { echo "Model server not answering at $1/models (docker compose -f deploy/dgx/models.compose.yml ps)"; exit 1; }
    grep -q "\"id\":\"$2\"" <<<"${body// /}" || { echo "$1 is up but does not serve '$2': $body"; exit 1; }
  }
  check "$QWEN_BASE_URL" "$QWEN_MODEL"
  check "$NEMOTRON_BASE_URL" "$NEMOTRON_MODEL"
  [[ -n "${CR_BASE_URL:-}" ]] && check "$CR_BASE_URL" "${CR_MODEL:-$NEMOTRON_MODEL}"
fi
if [[ "${HF_HUB_OFFLINE:-0}" == "1" ]]; then
  HF_HUB_OFFLINE=1 uv run python -c "from huggingface_hub import snapshot_download as s; import os; m=os.getenv('UR_MINILM_MODEL','sentence-transformers/all-MiniLM-L6-v2'); os.path.isdir(m) or s(m, local_files_only=True)" 2>/dev/null \
    || { echo "MiniLM is not cached; run 'uv run urban-response prefetch' once while online (or set HF_HUB_OFFLINE=0)"; exit 1; }
fi
[[ -n "${UR_API_TOKEN:-}" ]] || echo "WARNING: UR_API_TOKEN is empty; anyone reaching the tunnel can run the pipeline."

exec uv run urban-response serve --host "${SURU_HOST:-0.0.0.0}" --port "${SURU_PORT:-8080}"
