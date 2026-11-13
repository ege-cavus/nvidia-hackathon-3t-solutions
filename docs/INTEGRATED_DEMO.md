# Integrated authored-report demo

This integration combines the graph/report work, canonical agent backend, web
dashboard and docs/INCIDENT_RESPONSE_JSON.md. Run from the source checkout.
The older Streamlit app and game-session tests remain available as prototypes.

## Setup

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e '.[dev]'
python sheffield_sim.py --download-osm --ticks 0
python -m pytest -q
```

The OSM graph is cached at assets/sheffield.graphml and ignored by Git. World
mode requires that real cache; it fails explicitly instead of silently using
synthetic streets. The test suite uses labelled synthetic graphs and an injected
deterministic embedder, and never downloads model weights. One existing live
MiniLM test is opt-in. For real multi-report intake, prefetch MiniLM while online:

```powershell
urban-response prefetch
```

## Report-to-world command-line check

```powershell
python -m urban_response.world_demo --node CAM_01 --ticks 360
```

This uses an isolated in-memory database, the backend's seeded vehicles/crews,
the real cached graph, authored work/spread rules, and mock agent responses.
Results and ledger go to ignored demo_output/integrated_world.json. Omit --node
to run all four initial reports. The source fixture keeps its own report IDs;
the backend assigns database IDs and records their graph-node bindings separately.

To use the already-installed models on the Spark through an SSH tunnel:

```powershell
ssh -N -L 18034:127.0.0.1:11434 USER@SPARK
```

In another terminal:

```powershell
$env:QWEN_BASE_URL = 'http://127.0.0.1:18034/v1'
$env:NEMOTRON_BASE_URL = 'http://127.0.0.1:18034/v1'
$env:CR_BASE_URL = 'http://127.0.0.1:18034/v1'
$env:QWEN_MODEL = 'qwen3.6:35b-a3b'
$env:NEMOTRON_MODEL = 'nemotron-3.5-lightning:30b'
$env:CR_MODEL = 'nemotron-3.5-lightning:30b'
$env:UR_REASONING_EFFORT = 'none'
$env:UR_LLM_TIMEOUT_S = '300'
$env:UR_TRACE_FILE = 'demo_output/integration_live_trace.jsonl'
python -m urban_response.world_demo --live --node CAM_01 --ticks 360 --output demo_output/integration_live.json
```

Tags above were verified on this demo Spark, not asserted to exist on every host.
Both Qwen roles share one configured endpoint/model with separate request
contexts and role prompts. Three service agents assess concurrently; the CR role
uses the same Nemotron model by default. Installed tags and providers remain
configurable. No keys, host passwords or model weights are included.

## Command dashboard and accelerated clock

Use a **fresh demo database**, separate from any teammate's existing run. World
state is in memory and is not resumed from SQLite after a restart. The API refuses
world startup with active reservations that it cannot reconstruct.

```powershell
$env:UR_DB_PATH = 'data/integrated-demo.db'
$env:UR_WORLD_SCENARIO = 'assets/scenarios/sheffield_reports_demo/scenario.json'
$env:LLM_MODE = 'mock'
urban-response serve
```

Open the server URL printed by the CLI. The dashboard shows **Dispatch pending
reports** and **Advance one minute** in world mode. Incident detail shows observed
containment/work separately from the dispatch assessment. Vehicle map positions
come from the graph. The old arbitrary-incident demo controls are hidden because
this mode uses authored graph-node bindings.

For automatic playback, set UR_WORLD_AUTORUN=1 and UR_TIME_SCALE=20 before startup.
One simulated minute then advances every three real seconds. All model decision
windows pause the world clock. Cosmos generation runs separately and never
advances it. To run live models, retain the model endpoint variables above and
set LLM_MODE=live. Runtime model failures are labelled as fallbacks, not successes.

API equivalents: GET /world/state, POST /world/dispatch-due, POST /world/advance.
The compact dashboard endpoints are GET /ui/world, POST /ui/world/dispatch and
POST /ui/world/advance. New affected nodes emit new reports; dispatching pending
reports processes them through the same intake/agent/allocator path. Automatic
playback does this on its next decision window.

## Resource and outcome authority

- SQLite stores concrete vehicle/crew reservations with unique active-allocation
  indexes. The graph world uses those exact unit IDs, resource types and bases.
- Actual allocations, verified against database identity and crew speciality,
  become graph dispatch commands. A failed database commit restores world state.
- Graph routes determine movement and travel estimates. Road closures are
  respected. Hospital/base return and per-type cooldown complete before release.
  Scene durations use the backend's proposed durations, bounded below by the
  scenario's resolution work; work thresholds remain separate authored game rules.
  Internal scenario overrides are cooldown_minutes (resource type -> integer
  minutes), hospital_handover_minutes, and per-node deployment_scene_minutes.
  Per-node required_resource_types selects exact type/count thresholds. These
  fields do not alter the canonical caller/response JSON contracts.
- Required resource types must be on scene together for effective work. Partial
  suitable attendance slows spread, not resolution. Extra units get no extra work
  or suppression bonus. Busy units cannot be preempted or manually released in
  world mode. Failed or unfilled responses remain visible for follow-up.
- Expected release times are forecasts. Elapsed wall time and an immediate Qwen
  verdict cannot release vehicles. The world releases vehicles and crews together
  when actual travel/work/return/cooldown phases finish.
- Qwen's dispatch score/verdict assesses a plan. evaluation.metrics.world_outcome
  records subsequent observed work, containment and resolution. It never turns
  an estimated ETA into an observed arrival. Historical SYFR targets are reference
  context; risk mapping/compliance remain unassigned/unassessed.
- Deduplication uses scenario time. A new authored affected node cannot be merged
  into the source node merely because its report describes a similar nearby fire.

## Approved images

The default integrated demo is report-only. Existing Cosmos drafts are not
automatically approved. To add a reviewed JPEG/PNG, set an internal scenario
approved_media mapping (not a field on the canonical caller report):

```json
{"approved_media": {"20261113000002": {"approved": true, "image_path": "assets/incident_media/20261113000002.jpg"}}}
```

Only explicit approvals load image bytes for Qwen Intake. Paths must stay inside
the checkout. Image content is sent through the same OpenAI-compatible vision
request; caller claims remain unverified. Keep licence credits and capture time
with each asset. The Park Square reference trial remains experimental and is
not selected by this mapping. See REPORT_PIPELINE.md for generation/review.

## Demo limitations

Seed station/crew quantities and durations are illustrative. Real street routes
use inferred speeds and nearest-street access, not traffic-aware emergency ETAs.
Some seeded stations lie beyond the cached central-city graph and need access
verification/expanded coverage before using them in a wider-city demonstration.
Typed work requirements, spread multipliers and resolution thresholds are game
assumptions, not physical or clinical predictions. The future solo-game prototype
has not been connected to the backend fleet. This is not operational guidance.
The existing twelve alternate-caller observations remain a report-runner fixture;
automatic re-assessment/reinforcement of an already dispatched backend incident
is separate follow-up work. New spread-node reports do traverse this integration.
