# SURU team and agent boundaries

SURU uses authored incident reports and demo media -> Qwen selective routing ->
Nemotron proposals -> validated allocation -> report-driven world/outcome updates.
No RL/PPO training is used.

## Shared contracts

state_schema.py owns cameras, hubs, incident constructors and fresh city state.
knowledge_base.py owns probability priors, public source links and simulation SOPs.
Incidents are keyed by cam_id (not arbitrary incident ID); resource_db replaces
units, and message_board replaces comms_log. All components share one state object.
Never duplicate or silently rename contract fields. Coordinate schema changes
through Track 4's GitHub issue before updating consumers.

Each incident has location, hazard, severity 1..10, road_blocked, local Cosmos
video_path, qwen_report and pipeline status. Qwen selects FIRE/POLICE/AMBULANCE;
only selected models run. Safety floors may add a required agency. Board senders:
QWEN_TRIAGE, NEMOTRON_FIRE, NEMOTRON_POLICE, NEMOTRON_AMBULANCE,
QWEN_GOLD_ARBITER. Entries record tick, incident_cam, proposal_text, proposed_units
and consensus_status. Deadlock, timeout, malformed model output, unavailable units
or coverage violations cannot bypass the allocator. Qwen decisions are binding
only after the same deterministic validation as consensus. Failure defers dispatch.

## Strict File-Lease Rule (Zero Merge Conflicts)

| Track | Branch | ONLY edit |
| --- | --- | --- |
| 1 | feat/track1-sim | sheffield_sim.py |
| 2 | feat/track2-ollama | agent_orchestrator.py |
| 3 | feat/track3-ui | app.py |
| 4 | feat/track4-data | state_schema.py, knowledge_base.py, generate_assets.py |

Leases apply to humans and coding agents. feat/track1-rl is a compatibility branch
synced to main; new simulation work belongs on feat/track1-sim. Shared dependency,
test and documentation changes belong to coordinated integration on main.

## 4-stage vehicle lifecycle and station geography

POL_1/POL_2 start at Shepcote/Snig Hill; FIRE_1/FIRE_2 at Eyre/Rivelin;
AMB_1/AMB_2 at Northern General/Royal Hallamshire. These are team-supplied
coordinates and demonstration deployments, not a verified live fleet.

AVAILABLE -> DEPLETED_EN_ROUTE -> DEPLETED_ON_SCENE -> DEPLETED_RETURNING
-> DEPLETED_COOLDOWN -> AVAILABLE. Ambulances transport to Northern General,
complete hospital handover, then return to home_hub if different (another
DEPLETED_RETURNING leg). Fire restocks after returning. Only is_unit_available(u)
allows dispatch. East-to-west deployments deplete the resource for the complete
outward route + on-scene work + return/hospital/base legs + handover/restock.
Timers are nonnegative integer one-minute ticks. total_depletion_ticks_remaining
sums travel_to_scene_ticks + on_scene_ticks + return_to_base_ticks +
cooldown_ticks_remaining. On-scene work can extend if the hazard escalates; police hold until severity <4. The
nominal total is a forecast, not an unconditional release guarantee.

Directed graph routes exclude blocked_edges (u,v,key). In OSM mode edge travel
times derive from street lengths and inferred speeds: graph-exact calculations
with estimated speeds, not real-world exact arrival times. Round-trip routes are
snapshotted at dispatch; mid-trip road-closure rerouting is follow-up work.
Offline mode uses explicitly synthetic links and a 30 km/h assumption.

## GitHub issues are the communication channel

Architecture/integration: #1. Track 1: #2. Track 2: #3. Track 3: #4. Track 4: #5.
Post interface requests, blockers, test evidence and handoff notes on the relevant
issue. Link commits and cross-track issues. Do not change a teammate's leased
file to resolve a disagreement; record the requested contract change first.
Repository: https://github.com/ege-cavus/nvidia-hackathon-3t-solutions/issues

## Provenance and demo limits

NODE_HISTORICAL_PROBABILITIES retains the requested API name but contains
SYNTHETIC_UNCALIBRATED values. EA flood zones and STATS19 are source context;
no spatial join or historical fit has been performed. Numeric pumps, cordons,
reserve thresholds and two-tick cooldowns are simulation rules, not verified
SYFR/SYP/YAS SOPs. JESIP supplies shared-risk and coordination context; NHS ARP
Category 1 uses a seven-minute population mean, not seven ticks per patient.
Royal Hallamshire is a demo base, not asserted to be an adult A&E. Verify hospital
capabilities before extending routing. Never present simulated output as advice.
The model names are user-specified defaults; verify installed Ollama model tags
and set SURU_QWEN_MODEL / SURU_NEMOTRON_MODEL on the demo host.
The 212-camera GeoJSON and five Cosmos videos must be supplied locally with
provenance/licensing; missing assets are shown explicitly. Preserve .gitkeep;
MP4 files remain ignored. No model weights or live CCTV feeds are bundled.

## Future solo game architecture (issue #6)

game_session.py is a shared integration-owned module, not part of a track lease.
See docs/GAME_ARCHITECTURE.md before wiring a human-versus-agents mode. Human controls
and agents must use DispatchCommand/apply_command; world validation must never
require a Qwen report. Model reports are hypotheses and must not overwrite world
hazard/severity. Keep separate human/agent worlds with identical event tapes,
road graphs, rules and ticks; do not compare independent conditional RNG streams.
The UI must complete both decision windows before advancing. command_log enables
replay. Agree the four-profile incident catalogue and stage progression with
Track 4/Track 1; current severity logic is not yet capability-aware progression.

## Active report-driven integration path

The authored-report pipeline is documented in docs/REPORT_PIPELINE.md. report_world.py
is integration-owned; it reuses graph/resource/scoring helpers from Track 1.
It does not run legacy random spawning or intensity escalation. Caller reports
validate against schemas/incident.schema.json (the supplied jsonformal.txt).
Scenario-private spread links and response requirements stay outside report JSON
and outside the Qwen observation export. New affected nodes emit fresh timed
reports with causal links in the separate event ledger. Response success derives
from observed on-scene capabilities/work, not an LLM grading itself.

Tracks 2/3 should integrate qwen_observation(incident_id) and the report/outcome
ledgers; current legacy adapters do not consume ReportDrivenWorld automatically.
Demo images are pre-generated assets referenced per report; missing media is
explicit. Preserve these interfaces and coordinate schema/profile changes with
Track 4. Legacy sheffield_sim.py scenarios remain experimental regression tools.

## Final-demo integration

The user authorised integration on integrate/final-demo, including narrow hub
adaptation in sheffield_sim.py. See docs/INTEGRATED_DEMO.md. Active backend code
is urban_response/: canonical Incident/IncidentResponse, SQLite concrete fleet
and crews, three parallel agency agents and distinct Qwen intake/evaluator roles.
The report-driven bridge owns graph movement, observed work and resource release;
Qwen dispatch assessment never implies actual incident resolution. Legacy root
adapters and the six-unit solo-game prototype remain regression paths.

World mode uses UR_WORLD_SCENARIO and a fresh demo DB, pauses ticks during model
decisions, and can accelerate with UR_TIME_SCALE/UR_WORLD_AUTORUN. Only explicitly
approved_media scenario assets are sent to vision. Preserve both backend and
legacy test suites. Do not reinstate independent wall-clock release in world mode.

## Validated Qwen outcome authority (integration-owned)

The user authorised post-dispatch model effects on integrate/final-demo. In the
active backend, OutcomeProposal (urban_response/outcomes.py) and its validator
(urban_response/outcome_world.py) are the only model-to-world effect boundary.
Proposals name the incident/node, current world version/tick and unique ID.
Only observed typed on-scene capabilities over a complete working minute permit
one work credit; extra resources give no bonus. Accepted credits change severity,
containment and resolution under configurable game thresholds. The model may
defer work; malformed, contradictory, stale or replayed replies cannot mutate
the graph. Log accepted/rejected proposals with evidence in world_outcomes.

Reassess after dispatch, changed attendance or follow-up observations, and working
ticks. Apply effects before same-tick spread. Keep model windows paused; preserve
return/hospital/cooldown locks. No fresh post-dispatch pixels are assumed. Caller
claims never directly rewrite world truth. The earlier prohibition on raw model
hazard/severity overwrites still applies; validated bounded work effects are the
authorised exception in this integration. Legacy/solo-game worlds retain their
deterministic policy; a future match must use one policy for both competitors.
