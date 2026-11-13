# Deployment

SURU's backend (the agents, the databases and the world model) runs on the DGX Spark. The console can be served from the Spark itself, or from a laptop that reaches the Spark over SSH.

```
laptop browser ──► console (localhost:3100, or the Spark's own /console)
                      │  HTTP + SSE
                      ▼
DGX Spark:  SURU API :8080 ──► Qwen + Nemotron (Ollama :11434, or vLLM :8001/:8002)
              SQLite · MiniLM · OSM graph
```

## 1. Model servers

Both options expose an OpenAI-compatible `/v1` API. Choose one in `deploy/dgx/.env` (copy it from [env.example](../deploy/dgx/env.example)).

| Option | When | Settings |
|---|---|---|
| **Existing Ollama** (used at the hackathon) | models are already pulled, or you have no Docker access | `UR_MODEL_SERVERS=external`, both base URLs `http://127.0.0.1:11434/v1`, tags from `/api/tags`, `UR_REASONING_EFFORT=none`, `UR_LLM_TIMEOUT_S=240` |
| **vLLM** ([models.compose.yml](../deploy/dgx/models.compose.yml)) | dedicated Spark, best throughput | Qwen3.5-35B-A3B-FP8 on :8001 (0.40 of memory) and Nemotron-3-Nano-30B-A3B-FP8 on :8002 (0.38); needs `HF_TOKEN` |

Calls use `response_format=json_schema` and switch thinking off. If a server rejects either, set `UR_GUIDED_JSON=0` or `UR_DISABLE_THINKING=0`. CR shares the Nemotron endpoint unless you set `CR_BASE_URL` and `CR_MODEL`.

Ollama often serves one request at a time. If so, the three specialists queue, and a live cluster run takes about 50–65 s. vLLM runs them concurrently.

## 2. SURU API on the Spark

```bash
./deploy/dgx/setup.sh        # preflight, model servers (or check external ones), uv sync, MiniLM cache, seed, smoke test
./deploy/dgx/start.sh        # API on $SURU_HOST:$SURU_PORT (default :8080); checks the served model names first
uv run urban-response smoke  # any time: fails if any stage comes back tagged `fallback`
```

To keep the API running without a terminal, install [suru-api.service](../deploy/dgx/suru-api.service) as a systemd unit, or run `start.sh` in a restart loop inside `tmux`. Set `SURU_HOST=127.0.0.1` when the API is reached only through SSH.

## 3. Console on a laptop

```bash
SPARK=USER@SPARK ./web/run-local.sh      # opens the tunnel localhost:8080 → Spark:8080 and serves web/ on :3100
```

The page opens at `http://localhost:3100/console/`. [web/console/suru-config.js](../web/console/suru-config.js) points the console at `http://localhost:8080`. On the Spark, add the console's origin to `UR_CORS_ORIGINS` (for example `http://localhost:3100,http://127.0.0.1:3100`).

For an always-on setup, [web/serve-forever.sh](../web/serve-forever.sh) reconnects the tunnel whenever it drops. [web/com.suru.dashboard.plist](../web/com.suru.dashboard.plist) is a macOS LaunchAgent that starts it at login.

## 4. Hosted dashboard (Vercel)

`web/` also deploys to Vercel as a static site. [web/api/proxy.js](../web/api/proxy.js) forwards to the Spark and sends `SURU_BACKEND_TOKEN`, which must match the API's `UR_API_TOKEN`. The setup and the demo-day runbook are in [archive/CONSOLE_UPDATES.md](archive/CONSOLE_UPDATES.md) (Part B).

## Security notes

- `.env` is gitignored. Never commit `HF_TOKEN` or `UR_API_TOKEN`.
- Requests that arrive through the proxy or tunnel need the API token. Direct clients on the LAN don't.
- MiniLM runs with `HF_HUB_OFFLINE=1` once it is cached, so a dropped venue connection can't stall de-duplication.
