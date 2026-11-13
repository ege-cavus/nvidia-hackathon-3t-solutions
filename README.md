# Sheffield Urban Response Unit (SURU)

Python/Ollama hackathon digital twin: graph simulation -> Qwen CCTV triage ->
selected Nemotron agency proposals -> consensus or Qwen arbitration -> dispatch.

## Setup and run

Python >=3.11. PowerShell:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
python sheffield_sim.py --ticks 5
python agent_orchestrator.py --ticks 20
streamlit run app.py
python -m unittest discover -s tests -v
```

The default Seed 42 demo uses invented, directed graph links and synthetic triage.
The dashboard advances only when requested; each tick represents one minute.
Probabilistic mode: instantiate SheffieldSim(demo=False, seed=42).
OSM: run `python sheffield_sim.py --download-osm`; successful downloads are cached
at assets/sheffield.graphml and reused. Download failures fall back to a labelled
synthetic graph. Routes exclude blocked_edges; directed return paths are computed
separately. Travel times use inferred speeds and tick rounding. Resource depletion
includes on-scene work, return, hospital handover and fire restock. Escalation can extend on-scene work; police stay
locked while severity >=4. Mid-trip rerouting is not yet implemented.

## Live models and assets

Start Ollama and install appropriate vision/chat weights on the demo host. The
team-requested defaults Qwen3.8-27B and Nemotron-3.5-Bolt-E4B are configurable model
names, not verified Ollama registry tags. Configure your actual installed names:

```powershell
$env:SURU_QWEN_MODEL = 'your-installed-vision-model'
$env:SURU_NEMOTRON_MODEL = 'your-installed-agency-model'
python agent_orchestrator.py --live
```

For each CAM_XX, supply Cosmos-generated MP4 at
assets/hero_cams_disaster/CAM_XX.mp4. Prepare first-frame JPGs with:

```powershell
python generate_assets.py --prepare-heroes
python generate_assets.py --geojson cameras.geojson
python generate_assets.py --source input.mp4 --destination assets/hero_cams_disaster/CAM_01.mp4
```

The GeoJSON loader validates a local 212-point FeatureCollection; no council
pins or videos are fabricated. MP4s are ignored. Cosmos generation/API access
is not bundled. Qwen reads a prepared frame or extracts the first video frame.
Selected agencies run in parallel with bounded model-call timeouts. Structured
outputs, agency ownership, resource availability, round-trip reachability and
simulation coverage rules are validated. Invalid arbiter decisions defer dispatch.

## Data provenance and score

The requested NODE_HISTORICAL_PROBABILITIES API holds explicitly uncalibrated
synthetic priors, not measured probabilities. Public references and simulation
SOPs are in knowledge_base.py. Numerical thresholds are game rules; seven ticks
is a simulation SLA rather than an individual NHS response guarantee.
Score = clamp(100 - 5*SLA misses - 3*spillovers + resolved, 0, 100).
SLA counts first arrival <=7 ticks. Severity >=9 counts one spillover per incident;
resolution before that threshold counts one spillover prevented.

## Team communication

Use [GitHub issues](https://github.com/ege-cavus/nvidia-hackathon-3t-solutions/issues):
architecture #1, graph simulation #2, model agents #3, dashboard #4, data/SOPs #5.
Post contract requests, blockers and test results there. File leases and the
shared contract are documented in [AGENTS.md](AGENTS.md).

## Future Beat the Agents mode

game_session.py provides independent human/agent worlds, a common incident tape
and clock, controller-independent dispatch validation and command replay. The
playable UI is pending. See [GAME_ARCHITECTURE.md](GAME_ARCHITECTURE.md) and
[issue #6](https://github.com/ege-cavus/nvidia-hackathon-3t-solutions/issues/6).
The proposed next step is four incident profiles with type-specific stages,
while retaining the current flood/fire/trauma hazard categories for compatibility.

## Authored reports: new integration pipeline

See [REPORT_PIPELINE.md](REPORT_PIPELINE.md) for schema-valid sample reports,
authored graph spread, per-node report creation and observed response outcomes.
Run from this checkout after installing dependencies and caching the OSM graph:

```powershell
python report_world.py --ticks 6 --output demo_output/uncontrolled
python report_world.py --ticks 60 --scripted-response --output demo_output/responded
```

The first emits the authored secondary-fire report at tick 6; the second uses a
scripted smoke-test response to contain the source and prevent that report.
The runner exports Qwen-ready report/media observations separately from evaluator
rules. It is not yet wired into the legacy Streamlit/Ollama adapters.
