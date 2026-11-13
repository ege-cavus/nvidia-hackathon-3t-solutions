#!/usr/bin/env bash
# One-shot bring-up on the DGX Spark: preflight -> model servers -> Python env -> smoke test.
# Safe to re-run. Usage (from anywhere):  ./deploy/dgx/setup.sh [--no-smoke]
set -euo pipefail
cd "$(dirname "$0")/../.."
DGX=deploy/dgx
ENV_FILE="$DGX/.env"
COMPOSE=(docker compose -f "$DGX/models.compose.yml" --env-file "$ENV_FILE")

step() { printf '\n==> %s\n' "$*"; }
die()  { echo "ERROR: $*" >&2; exit 1; }

step "Preflight"
[[ -f "$ENV_FILE" ]] || die "missing $ENV_FILE (cp $DGX/env.example $ENV_FILE and fill it in)"
set -a; source "$ENV_FILE"; set +a
EXTERNAL=0; [[ "${UR_MODEL_SERVERS:-compose}" == "external" ]] && EXTERNAL=1
command -v uv >/dev/null || die "uv not installed (curl -LsSf https://astral.sh/uv/install.sh | sh)"
[[ "$(uname -m)" == "aarch64" ]] || echo "WARNING: expected aarch64 (DGX Spark), got $(uname -m)"
if (( ! EXTERNAL )); then
command -v docker >/dev/null || die "docker not installed"
docker info >/dev/null 2>&1 || die "cannot talk to docker (add yourself to the docker group: sudo usermod -aG docker \$USER; newgrp docker)"
docker compose version >/dev/null || die "docker compose plugin missing"
command -v nvidia-smi >/dev/null && nvidia-smi --query-gpu=name,driver_version --format=csv,noheader || echo "WARNING: nvidia-smi not found"
[[ -n "${HF_TOKEN:-}" ]] || echo "WARNING: HF_TOKEN empty; downloads only work if the weights are already in ${HF_CACHE:-~/.cache/huggingface}"
python3 - <<EOF || die "QWEN_GPU_MEM + NEMOTRON_GPU_MEM must be <= 0.85"
import sys; sys.exit(0 if float("${QWEN_GPU_MEM:-0.40}") + float("${NEMOTRON_GPU_MEM:-0.38}") <= 0.85 else 1)
EOF
fi

if (( EXTERNAL )); then
  step "Model servers: external (UR_MODEL_SERVERS=external), not starting containers"
  for u in "$QWEN_BASE_URL" "$NEMOTRON_BASE_URL"; do
    curl -fsS --max-time 5 "$u/models" >/dev/null || die "no model server at $u/models"
    echo "ok: $u"
  done
else

step "Nemotron reasoning parser plugin"
mkdir -p "$DGX/plugins"
PLUGIN="$DGX/plugins/nano_v3_reasoning_parser.py"
if [[ ! -s "$PLUGIN" ]]; then
  curl -fsSL -o "$PLUGIN" \
    "https://huggingface.co/${NEMOTRON_HF_MODEL:-nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-FP8}/resolve/main/nano_v3_reasoning_parser.py" \
    || die "could not download nano_v3_reasoning_parser.py"
fi
echo "ok: $PLUGIN"

step "Model servers (first run downloads ~70 GB of weights; can take a while)"
"${COMPOSE[@]}" pull
"${COMPOSE[@]}" up -d
wait_for() {  # name url
  local name=$1 url=$2 t=0
  printf 'waiting for %s ' "$name"
  until curl -fsS --max-time 3 "$url" >/dev/null 2>&1; do
    if [[ "$(docker inspect -f '{{.State.Status}}' "$name" 2>/dev/null)" =~ ^(exited|dead)$ ]]; then
      echo; docker logs --tail 60 "$name"; die "$name exited"
    fi
    (( t += 10 )); (( t > 1800 )) && { echo; docker logs --tail 60 "$name"; die "$name not healthy after 30 min"; }
    printf '.'; sleep 10
  done
  echo " up (${t}s)"
}
wait_for suru-qwen "${QWEN_BASE_URL%/v1}/health"
wait_for suru-nemotron "${NEMOTRON_BASE_URL%/v1}/health"
command -v nvidia-smi >/dev/null && nvidia-smi --query-compute-apps=pid,used_memory --format=csv || true
fi

step "Python environment + MiniLM cache"
uv sync --frozen
HF_HUB_OFFLINE=0 uv run urban-response prefetch
uv run urban-response seed

if [[ "${1:-}" != "--no-smoke" ]]; then
  step "Smoke test"
  uv run urban-response smoke --trace "data/smoke-$(date +%Y%m%d-%H%M%S).jsonl"
fi

step "Done. Start the API with: ./deploy/dgx/start.sh   (or install deploy/dgx/suru-api.service)"
