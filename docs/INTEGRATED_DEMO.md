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

The updated **999 Console** is also available at `/console`. It includes the
animated agency network, solid vehicle icons, satellite-map option and hideable
panels. Both surfaces use the same canonical backend and graph world. The console
uses actual graph routes and vehicle positions in world mode, refreshes mutable
assessments/outcomes, and receives authored-report jobs through the normal event
stream. Its new-call form (outside authored world mode) submits caller JSON with
unknowns preserved; category and severity come from intake triage.

The latest urban-response local console (8ff3975) is integrated on both `/console`
and `web/console/`: compact pipeline steps, hover detail, live ticker, inventory
changes and the hide-map control. The source branch and main remain unchanged.
For a fresh local Windows run after activating your environment:

```powershell
.\scripts\run-world-demo.ps1
```

Open http://localhost:8082/console, click **Dispatch pending reports**, then
**Advance one minute**. The script defaults to mock agents with the same validated
proposal path used by Qwen. Add `-Live` after configuring all model endpoints above,
or `-Autorun` for an accelerated clock. `-Python` can select your venv executable.

For a separately hosted dashboard, set UR_CORS_ORIGINS to the explicit frontend
origins before starting the API (for example http://localhost:3100). Same-origin
previews need no CORS setting. The Vercel proxy permits the compact `/ui/world`
routes used by these controls. Neither surface bypasses lifecycle release locks.

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

## Post-dispatch Qwen effects and reassessment

The API defaults to `UR_WORLD_OUTCOME_MODE=model` in authored-world mode. Qwen
Outcome uses the same configured Qwen endpoint as Intake, with a separate prompt
and request context. The model returns `OutcomeProposal` in urban_response/outcomes.py:
proposal_id, incident_id, node_id, world_version, tick, outcome, work_credit, reason.
This is a separate internal contract; canonical incident/response JSON is unchanged.

The loop is: move vehicles/receive reports -> collect observations -> Qwen proposal
-> validate/apply -> spread -> update metrics -> release completed vehicles. It runs
after dispatch and as the clock advances. A full minute with all required typed
resources present at both ends permits at most one work credit. The model can
award or withhold that credit. It must classify the result using work **after**
its proposed credit, with explicit projections included to prevent off-by-one errors.
Validated credits reduce severity, cross containment thresholds (stopping spread),
and eventually resolve the incident and reopen its roads. Extra resources add no
work bonus. Relevant partial attendance still slows spread under the graph policy,
without reducing severity or earning work. Model failure grants no work credit;
the clock, hazard spread and occupied-resource lockouts continue.

Version/tick/node checks reject stale proposals. A unique proposal ID and per-node
credit guard prevent replay/double work. Requirements and thresholds are authored
game assumptions; the model cannot rewrite them. Accepted/rejected proposals and
their evidence persist in SQLite `world_outcomes` and appear in GET /world/state,
the console's Outcome tab/decision log and response evaluation world_outcome metrics.
Effect mutation rolls back if its audit write fails. An initial dispatch grade
never constitutes observed containment or success.

Reassessment runs immediately on changed unit phases (including reinforcements),
new same-incident caller observations, every on-scene working minute and every
five transit minutes. Unchanged repeated requests in the same minute are skipped.
**Reassess response**, POST /world/reassess and POST /ui/world/reassess trigger a
due assessment without advancing time. Set UR_WORLD_FOLLOW_UPS to the existing
same_incident_reports.json bundle to schedule those observations; claims remain
separate from observed world state. Outcome reassessment does not automatically
ask Nemotron for a new reinforcement dispatch; that is a separate command path.

Set UR_WORLD_OUTCOME_MODE=rules for the deterministic comparison policy. The CLI
world_demo defaults to model proposals too; use `--outcomes rules` for comparison.
The legacy ReportDrivenWorld and solo-game prototype retain deterministic rules.
A future game must evaluate both human and AI worlds with the same outcome policy.

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
Same-incident caller observations can now trigger outcome reassessment when their
bundle is configured. Automatic Nemotron reinforcement allocation remains follow-up
work. New spread-node reports traverse the intake/agency/allocator path.
