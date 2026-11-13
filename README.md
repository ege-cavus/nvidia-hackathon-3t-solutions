# SURU: Sheffield Urban Response Unit

**Multi-agent emergency dispatch that runs entirely on one NVIDIA DGX Spark.**
999 calls come in as free text. A Qwen orchestrator triages and de-duplicates them. Three Nemotron specialists (police, ambulance and fire) assess the scene in parallel. A Nemotron coordinator makes the final call on what to send. Code then checks that call against the city's real vehicles, crews and street network. A graph world model of Sheffield shows what happens next: vehicles drive real roads, fires spread or get contained, and units only come back once their crews have finished.

Built in one day at an NVIDIA DGX Spark hackathon (13 Nov 2026). Everything runs locally on the Spark: no cloud models and no API keys.

> All incidents, stations and quantities are fictional or illustrative. This is a research demo, not operational guidance.

---

## What it does

```
999 calls ──► Qwen INTAKE      canonical incident → triage (category, severity, location)
               │                MiniLM proposes duplicates, Qwen decides: duplicate / related / new
               ▼
              Qwen ROUTE       which agencies, who leads
               ▼
              Nemotron A1 police · A2 ambulance · A3 fire     (parallel, each with its own SOP skill + KB)
               ▼  resource requests, cordons, staging, mission notes
              Nemotron CR      final call: manifest, conflict resolutions, one order per agency
               ▼
              CODE             life-safety floors, hard/soft conflict checks, crewed-vehicle allocator
               ▼
              WORLD MODEL      OSM street graph: travel, on-scene work, spread, containment, return, cooldown
               ▼                Qwen proposes outcome effects each minute; code validates and applies them
              Qwen EVALUATE    code-computed metrics → score, verdict, one-line headline
```

- **One click demo.** The console's **▶ Run demo** resets the city, puts an earlier tower-block fire on scene, then sends three callers reporting the same Park Hill fire seconds apart. The duplicates merge and one incident goes through all five agents. On the Spark this takes about **50–65 s end to end with no fallbacks**, or about 8 s in rules-only mode.
- **Models propose, code decides.** Every agent output is schema-constrained JSON. A deterministic layer enforces life-safety minimums (for example one ambulance per casualty, or a rescue tender when anyone is trapped), finds hard conflicts (crew shortfalls, contention, ETA misses) and soft conflicts (staging inside another agency's cordon). It re-validates whatever the coordinator picks. Each output is tagged `nemotron`, `qwen`, `rules` or `fallback`, so a failed model call can never pass as a success.
- **A world model, not a timer.** In world mode, vehicles move along routes on the real OpenStreetMap graph of Sheffield. Incidents have typed work requirements and spread links to neighbouring nodes. Units are released only after travel, work, hospital handover and restock have actually finished. Qwen judges what happened on scene; versioned, replay-safe proposals stop it from double-counting work or releasing vehicles early.
- **Readable for a narrator.** The 999 console shows the pipeline as nine compact steps, with a one-line rotating log, live inventory per agency, and an agents panel with per-agent cards. The satellite map can be hidden, and tabs show the NLP intake, the incident log and the decision log.

## Model stack on the DGX Spark

| Role | Model | Notes |
|---|---|---|
| Intake, routing, evaluation, world outcomes | **Qwen 3.6 35B-A3B** | one endpoint, separate prompts per role |
| A1 police · A2 ambulance · A3 fire · CR | **Nemotron 3 Nano 30B-A3B** | shared endpoint; persona, skill file and allowed resources differ |
| Duplicate proposals | `all-MiniLM-L6-v2` | embeddings + location/time/registration adjustments, complete-linkage clustering |
| Incident imagery (optional) | **Cosmos** generator NIM | reference-conditioned images; only human-approved images reach Qwen |

Models are served through an OpenAI-compatible API: the Spark's Ollama, or the bundled vLLM compose file. Reasoning is off and decoding is constrained to a JSON schema.

## Quick start

```bash
uv sync
uv run pytest                                  # 114 tests, no GPU or network needed
uv run urban-response demo --reset             # five Sheffield scenarios, rules-only
LLM_MODE=mock uv run urban-response serve --port 8080
open http://localhost:8080/console             # press ▶ Run demo
```

**Live models on a DGX Spark.** Copy `deploy/dgx/env.example` to `deploy/dgx/.env`, then run `./deploy/dgx/setup.sh` and `./deploy/dgx/start.sh`. `uv run urban-response smoke` checks every stage end to end and fails if any stage fell back. To keep the UI on a laptop, tunnel the API port and run `web/run-local.sh`. See [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md).

**World mode** (graph-backed Sheffield; needs the cached OSM graph from `python sheffield_sim.py --download-osm --ticks 0`):

```bash
UR_WORLD_SCENARIO=assets/scenarios/sheffield_reports_demo/scenario.json \
UR_DB_PATH=data/integrated-demo.db uv run urban-response serve
```

See [docs/INTEGRATED_DEMO.md](docs/INTEGRATED_DEMO.md).

## Repository

```
urban_response/            the agent backend (FastAPI + SQLite)
  agents/                  Qwen orchestrator, Nemotron specialists (A1–A3) and coordinator (CR), rule fallbacks
  conflicts/               hard accounting + allocator, soft scene-constraint rules
  dedup.py triage.py       intake: MiniLM proposals, Qwen decision, gazetteer geocoding
  world.py outcome_world.py  graph world integration, Qwen outcome proposals + validation
  evaluation.py briefing.py  code-computed metrics, phone brief
  static/index.html        the 999 console (also at web/console/)
report_world.py            authored reports → graph world: spread, work, containment
sheffield_sim.py           OSM street graph, routing, vehicle lifecycle
cosmos_client.py           Cosmos NIM client and Qwen evidence review
nemotron_resource_skills/  per-agency SOP skills used as agent system knowledge
knowledge_base/            per-agency guidance retrieved with BM25
assets/scenarios/          authored Sheffield report scenario and follow-up calls
deploy/dgx/                Spark setup, start script, vLLM compose, systemd unit
web/                       standalone dashboard + console for a separately hosted UI
tests/                     114 tests: pipeline, conflicts, world, outcomes, API concurrency, console contract
```

## Documentation

| Doc | What's in it |
|---|---|
| [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) | pipeline stages, data model, de-duplication, conflicts, crews, safety floors, API |
| [docs/INTEGRATED_DEMO.md](docs/INTEGRATED_DEMO.md) | the graph world: setup, resource and outcome authority, Qwen reassessment loop |
| [docs/REPORT_PIPELINE.md](docs/REPORT_PIPELINE.md) | authored reports, spread causality, Cosmos media generation and review |
| [docs/INCIDENT_RESPONSE_JSON.md](docs/INCIDENT_RESPONSE_JSON.md) | the canonical incident and response JSON contracts |
| [docs/DEPLOYMENT.md](docs/DEPLOYMENT.md) | running on the DGX Spark, laptop console over SSH, Vercel |
| [docs/GAME_ARCHITECTURE.md](docs/GAME_ARCHITECTURE.md) | planned human-vs-agents mode on the same world |
| [docs/archive/](docs/archive/) | design notes from the hackathon (UI brief, console review, product notes) |

## Team

Built by GitHub users **ege-cavus** (agent pipeline, DGX Spark deployment, console story mode), **BenDz143** (Sheffield graph simulation, report world model, Cosmos media, integration), **Severian0** (command dashboard and 999 console) and **Joshua Marshall-law** (MiniLM de-duplication pipeline, Nemotron resource skills).

## Limitations

- Station locations, unit counts, durations and work thresholds are illustrative game assumptions.
- Travel times use inferred street speeds, not traffic-aware emergency ETAs.
- The Spark's Ollama serves one request at a time, so the three specialists queue rather than run truly in parallel. A vLLM deployment removes that bottleneck.
- Live scores vary from run to run, because the agents are sampled models.
