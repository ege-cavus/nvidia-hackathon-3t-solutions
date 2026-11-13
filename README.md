# nvidia-hackathon-3t-solutions
Urban Response Research: agentic multi-agency incident response for Sheffield, running locally on an NVIDIA DGX Spark (128 GB).

## Architecture

The loop runs from report to backlog with no human step.

```
report ──► 0. INGEST              canonical incident → numeric id → Qwen triage → MiniLM clustering
              │  duplicate ──► merged into the existing incident, logged, nothing dispatched
              │  related   ──► dispatched, with the nearby incident passed to the agents as context
              ▼
           1. ROUTE (Qwen)        which agencies, who leads
              ▼
           2. Nemotron A1 police · A2 ambulance · A3 fire   (parallel, each with its own KB)
              ▼  numeric requests + scene constraints + mission details
           3. Nemotron CR         final call
              · MANIFEST: final quantity per type and agency (consolidates overlaps, drops
                nice-to-haves, errs on abundance for life safety)
              · code enforces life-safety floors and caps over the manifest
              · hard conflicts (vehicles AND crews) → CR picks a valid resolution → code re-checks
              · soft conflicts: rules + CR review of free text
              · ORDERS: normalised, one imperative sentence per agency
              · code allocates fully crewed vehicles → Response DB (incident: dispatched)
              ▼
           4. EVALUATE (Qwen)     reads code-computed success metrics → score, verdict, headline
              ▼
           5. FINALISE            incident → backlog; units and crews return automatically at
                                  their release time; incident closes when all are back
```

### Data model

```
incidents (Incident DB)            responses (Response DB)         agent_responses / allocations /
  incident_id PK ◄─────────────┐     response_id PK ◄──────────────  crew_assignments / conflicts
  status                       └──   incident_id FK                   (all keyed by response_id)
  duplicate_of FK (merged)           manifest, directives, evaluation
  report_count
personnel (speciality, station, on_shift)   inventory_ledger (dispatched / reassigned / returned)
incident_log (plain-language timeline per incident)
```

Incident status: `pending` → `dispatched` → `backlog` → `closed`. A duplicate report becomes `merged`, with `duplicate_of` pointing at the incident it joined.

**Canonical incident.** [examples/incident.schema.json](examples/incident.schema.json) is the incident for the whole pipeline: intake, storage, de-duplication, agents and responses all use it. `incident_id` is a numeric YYYYMMDDNNNNNN assigned by the database on ingest, and responses get a numeric `response_id` in the same format. The incident is never rewritten. What the pipeline derives from it lives in a separate **triage** object, produced by Qwen at intake with rules as the fallback and as a check:
- `category` and `severity`;
- coordinates, taken from caller GPS first, then a built-in Sheffield gazetteer (offline), then Qwen's estimate, then the city centre (flagged in the log);
- counts of casualties and trapped people, which can never be lower than what the caller reported.

Agents are shown `{incident, triage}`, and every response carries both.

**Duplicate detection: MiniLM proposes, the agent decides.** [incident_cluster_handoff.py](incident_cluster_handoff.py), Joshua's MiniLM pipeline from `origin/main`, is vendored byte-for-byte unchanged. It runs on every report with `all-MiniLM-L6-v2` embeddings, location, time and vehicle-registration adjustments, and complete-linkage clustering at threshold **0.85**. [dedup.py](urban_response/dedup.py) feeds it one representative report per live incident (the earliest with caller GPS) plus the new report. It returns MiniLM's proposal and the candidate incidents: those MiniLM clustered the report with, plus active incidents within 500 m that it didn't.
- **Final call:** Qwen, the intake agent, decides between `duplicate` (merged, nothing dispatched), `related` (dispatched separately, with the neighbour as context) and `new`. It is told to merge only when confident; if the reports are probably different incidents, it dispatches separately. It can also merge reports MiniLM kept apart, such as two callers who name different landmarks for the same block.
- **No candidates:** no agent call is made.
- **Fallback:** in rules-only mode, or if the agent fails or picks an incident outside the candidates, MiniLM's verdict stands, with the category guard (`UR_DEDUP_CATEGORY_GUARD`) refusing merges across clearly different incident types.
- **Merges:** reports only merge into earlier incidents, and a report merged into one that later merges elsewhere moves to the root.
- **Audit:** each report stores its decision (MiniLM's proposal, the candidates shown, the final verdict and who made it) on the incident row.
- **Offline use:** run `urban-response prefetch` once, then `HF_HUB_OFFLINE=1`. `UR_MINILM_MODEL` can point to a local model directory.

**Nemotron skills as knowledge** ([skills.py](urban_response/skills.py)): `nemotron_resource_skills/` is vendored unchanged from `origin/main`.
- **A1, A2, A3:** each agent's system prompt includes its service `SKILL.md`: role, assessment method, the 1–5 service severity scale, guardrails and resource-proposal principles. Agents record `service_severity`, `evidence_quotes`, `missing_information` and `contradictions` in `specialist_metadata`.
- **CR:** gets `resource_allocator_skill/SKILL.md` as its decision policy. Specialist quantities are the default, cuts need a specific rationale, there are no cross-service transfers and no arbitrary reserves. Reassigning a unit is allowed only when code offers it as a valid option.
- **Left out:** the parts of the skills that define their own I/O contract (`proposal_schema.json`, their resource names, ranking and totals bookkeeping, human-review flags). They would clash with this pipeline's schemas.
- **Setting:** `UR_SKILLS_DIR` overrides the folder location.

**Vehicle cycle and replenishment** ([timing.py](urban_response/timing.py)) follows `main`'s 4-stage lifecycle:
- **en route:** ETA from the station.
- **on scene:** the requested duration.
- **returning:** back to the station. An ambulance carrying a casualty goes via the receiving hospital (major trauma centre at severity 4 or more).
- **cooldown:** per-type restock or handover, for example breathing-apparatus refill, decontamination or clean-down.

The vehicle and its crew return to the inventory only when the cycle ends. The unit-return loop does this automatically and logs it in the inventory ledger. `/api/state` shows each vehicle's current stage (`fleet`) and what's out by stage per resource type. `UR_TIME_SCALE` speeds up the clock for demos.

**Crews** ([resources.py](urban_response/resources.py)): every vehicle type needs specific specialities (for example a fire engine needs 1 crew commander and 3 firefighters; an ARV needs 2 firearms officers). The allocator crews each vehicle from its own station first, then from the nearest stations. A vehicle that can't be crewed is not sent. A shortfall of people is its own hard conflict (`crew_shortfall`).

**Life-safety floors** ([manifest.py](urban_response/manifest.py)): CR may trim, but never below these minimums:
- one ambulance per casualty (beyond four casualties, one per two);
- a rescue tender when anyone is trapped;
- 2 or 4 fire engines for a fire, plus an aerial platform for a high-rise;
- a hazmat unit for hazardous substances;
- 2 ARVs for a weapons call;
- HART for casualties in a hazardous area.

**Success metrics** ([evaluation.py](urban_response/evaluation.py)), all computed in code:
- essential needs met
- arrival targets met per agency
- vehicles fully crewed
- units asked for vs declared
- mutual aid requests
- units reassigned from other incidents

**Phone brief** ([briefing.py](urban_response/briefing.py), `GET /api/brief/{id}`, `/?brief=INC-…` in the console): headline, dek, dateline, key facts, orders by agency, units with crews, and a timestamped incident log. It is written like a news log, with no model reasoning in it.

Every agent output is tagged `executed_by`: `nemotron`, `qwen`, `rules`, `fallback` (the model call failed and rules filled in) or `mock`.

### Conflict layers

**Hard (deterministic accounting)**, see [conflicts/hard.py](urban_response/conflicts/hard.py):

| kind | meaning |
|---|---|
| `crew_shortfall` | not enough free, on-shift people with a speciality to crew the vehicles |
| `shortfall` | one agency requests more of a type than is free in the Resource DB |
| `contention` | several agencies request the same pool and it cannot cover all of them |
| `ownership` | an agency requests another agency's resource type |
| `unknown_resource` | the type doesn't exist in the Resource DB |
| `eta_unmet` | units exist, but none within the requested `max_eta_min` |

Availability accounts for units committed to every active incident. Possible resolutions are `preempt` (take units from an incident at least 2 severity levels lower), `substitute`, `mutual_aid`, `relax_eta` and `accept_partial`. The code lists which ones are valid for each conflict. CR picks one, the choice is re-validated, and the rule policy takes over if the choice is invalid. The allocator is deterministic: essential requests first, then lead agency first, then nearest unit first.

**Soft (response details)**, see [conflicts/soft.py](urban_response/conflicts/soft.py):

| code | rule |
|---|---|
| `ENTRY_RESTRICTED_BUT_REQUIRED` | one agency closes or controls the scene that another needs to enter |
| `CORDON_INSIDE_EVACUATION_ZONE` | an outer cordon is smaller than another agency's evacuation radius |
| `STAGING_INSIDE_INNER_CORDON` | an agency stages inside another agency's hot zone |
| `ROAD_CLOSURE_BLOCKS_ACCESS` | a closed road is another agency's access route |
| `STAGING_MISMATCH` | agencies propose different RVPs |
| `EVIDENCE_VS_ENTRY` | evidence preservation versus operational entry |
| `UNROUTED_AGENCY_NEEDED` / `NEED_WITHOUT_RESOURCES` | an inter-agency dependency that nobody covers |

After the rules run, CR reads the free-text mission details, reports only contradictions the rules missed (`source: semantic`), and writes the unified directives.

## Quick start

```bash
uv sync
uv run urban-response demo --reset     # 5 Sheffield scenarios, mock mode (no GPU needed)
uv run pytest                          # 21 tests
uv run urban-response incidents        # incident DB
uv run urban-response burst --reset    # 4 near-simultaneous calls around one fire: 2 merged, 1 related
UR_TIME_SCALE=20 uv run urban-response serve   # units return 20x faster, so the loop closes during a demo
uv run urban-response serve            # HTTP API on :8080, docs at /docs
```

The demo runs the scenarios in sequence against shared resources, so later incidents show shortfalls, preemption from the lower-severity RTC, mutual aid, and soft conflicts such as the ambulance staging inside the fire inner cordon.

## Running with the models on the DGX Spark

Default stack ([deploy/dgx/models.compose.yml](deploy/dgx/models.compose.yml)), two vLLM servers from the NGC image built for GB10:

| Role | Checkpoint | Served as | Port | Memory cap |
|---|---|---|---|---|
| Qwen orchestrator (ingest, route, evaluate) | `Qwen/Qwen3.5-35B-A3B-FP8` | `qwen3.5` | 8001 | 0.40 |
| Nemotron A1 police, A2 ambulance, A3 fire, CR | `nvidia/NVIDIA-Nemotron-3-Nano-30B-A3B-FP8` | `nemotron-3-nano` | 8002 | 0.38 |

On the Spark:

```bash
cp deploy/dgx/env.example deploy/dgx/.env   # set HF_TOKEN and UR_API_TOKEN
./deploy/dgx/setup.sh                        # preflight, start both servers, uv sync, cache MiniLM, smoke test
./deploy/dgx/start.sh                        # API on :8080 (or install deploy/dgx/suru-api.service)
uv run urban-response smoke                  # re-check any time: fails if any stage is tagged `fallback`
```

`smoke` checks three things: each endpoint serves the configured model name, a structured probe comes back as valid JSON with no `<think>` trace (latency printed), and one full incident runs on a throwaway DB with every stage served by a model.

All four Nemotron agents (A1-A3 and CR) share one Nemotron endpoint by default. Set `CR_BASE_URL` / `CR_MODEL` to run CR separately. They differ in persona, knowledge base and allowed resource types. Calls use `response_format=json_schema` (guided decoding) and turn thinking off through the chat template. If the server rejects either, set `UR_GUIDED_JSON=0` or `UR_DISABLE_THINKING=0`.

## Testing with Claude Code instead of the Spark

To check the pipeline against a real LLM before the Spark is set up, serve every role (Qwen, A1–A3, CR) with Claude Code in headless mode (`claude -p`). It uses your Claude Code login, so no API key is needed. Tools and MCP servers are switched off for these calls. Prompts, schemas and validation are the same as on the vLLM path.

```bash
LLM_MODE=live LLM_PROVIDER=claude-code uv run urban-response incident scenarios/simple_rtc.json --trace run.jsonl
uv run urban-response trace run.jsonl          # every prompt and reply, per agent
```

`CLAUDE_MODEL` overrides Claude Code's default model, and `CLAUDE_EFFORT` sets the effort level. `--trace` works with any provider. The `qwen` / `nemotron` tags in the output name the role, not the model; the trace records which model actually served each call.

## API

| method | path | |
|---|---|---|
| POST | `/incidents` | register an incident and run it through the pipeline → `IncidentResponse` |
| GET | `/incidents?status=`, `/incidents/{id}`, `/backlog` | incident DB |
| GET | `/responses`, `/responses/{id}`, `/incidents/{id}/responses` | response DB |
| POST | `/incidents/{id}/close`, `/units/{id}/release` | release units |
| GET | `/resources` | inventory: total, committed, available |
| GET | `/resources/{type}/forecast` | availability forecast based on expected release times |
| GET | `/conflicts?incident_id=&response_id=` | conflicts per response (hard, soft, preempted) |

## Layout

```
urban_response/
  schemas.py              canonical Incident / AgentResponse / conflicts / IncidentResponse
  agents/orchestrator.py  Qwen: register incident, route, fan out, hand off to CR
  agents/conflict_resolver.py  Nemotron CR: conflict resolution, final call, writes the response DB
  agents/specialist.py    Nemotron A1/A2/A3: KB retrieval + structured response
  agents/heuristics.py    deterministic stand-ins (mock mode and fallback)
  conflicts/hard.py       accounting, resolution options, allocator
  conflicts/soft.py       scene-constraint rules
  db.py / seed.py         Resource DB + incident log (SQLite), Sheffield seed data
  kb.py                   BM25 retrieval over knowledge_base/<agency>/*.md
knowledge_base/           per-agency guidance (illustrative)
scenarios/                demo incidents
```

> Seed data (stations, unit counts, coordinates) and knowledge-base content are approximate and illustrative, for the hackathon demo only.
