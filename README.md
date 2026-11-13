# Sheffield Urban Response Unit (SURU)

Emergency command digital twin hackathon starter for Sheffield, UK, using Python and Ollama.
See [AGENTS.md](AGENTS.md) for team ownership and the shared state contract.

## Setup

Python 3.11 or newer:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install -e .
```

## Run

```powershell
python generate_assets.py
python agent_orchestrator.py
python sheffield_env.py
python train_rl.py --steps 2048
streamlit run app.py
```

The first two commands run offline demos using only the Python standard library.
Other commands need the project dependencies. Downloading the Sheffield road graph
is explicit: call `sheffield_env.load_sheffield_graph()` with internet access.

For live Ollama triage, start your local Ollama server, pull a vision-capable model
such as `llama3.2-vision`, and provide a prepared quad-view image:

```powershell
python agent_orchestrator.py --live --image quad.jpg --model llama3.2-vision
```

Track 2 exposes `stitch_quad` for producing the quad from four OpenCV BGR frames.
Place CAM_XX.mp4 files under assets/hero_cams_clean or assets/hero_cams_disaster.
MP4 files stay local. The fifth hero camera is selectable in the four-panel monitor.

Track 4 can validate a locally supplied 212-point camera GeoJSON or prepare a clip:

```powershell
python generate_assets.py --geojson cameras.geojson
python generate_assets.py --source input.mp4 --destination assets/hero_cams_clean/CAM_01.mp4
```

The bootstrap includes synthetic demo incidents, placeholder assets and integration
TODOs. It does not bundle live council data, videos, model weights or a trained policy.

## Station deployment and turnaround

Six units start at six separate stations defined in `EMERGENCY_HUBS`.
Only `is_unit_available` units can be dispatched. Travel, on-scene work,
return and cooldown all lock a vehicle. The map shows station locations and
the sidebar shows status and busy ticks. Routed travel and countdowns are
Track 1 integration work; UI dispatch is disabled until they are implemented.
