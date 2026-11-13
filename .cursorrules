# SURU team and agent boundaries

SURU is a hackathon simulation of an emergency command digital twin for Sheffield, UK.

## Shared contract

`state_schema.py` is the single source of truth for camera IDs, local asset paths,
emergency hubs and fresh city state. All tracks import `get_initial_city_state`.
Pass the same state dictionary between components when integrating; calling the
factory again creates an independent simulation. Do not duplicate the schema.
Coordinates are team-supplied; video assets and the 212-camera dataset are not bundled.

- Track 1 owns simulation ticks, graph routing, hazard propagation and PPO training.
- Track 2 consumes CCTV and state, runs Gold vision triage and three concurrent
  Bronze advisers, and appends timestamped command messages.
- Track 3 owns Streamlit session state, displays maps/feeds/comms and captures human actions.
- Track 4 owns data preparation, deterministic scenarios and shared schema changes.

Incidents are keyed by incident ID and contain camera_id, hazard (flood/fire/trauma),
severity (1?10), road_blocked, vlm_summary and video_path. Units contain type, lat,
lon, home_hub, status, target_cam (camera ID or None), eta_ticks,
on_scene_ticks_remaining, cooldown_ticks_remaining and total_busy_ticks.
blocked_edges uses OSM (u, v, key) tuples. Comms senders are BRONZE_POLICE,
BRONZE_FIRE, BRONZE_NHS or GOLD_COMMAND; timestamp is a string (simulation tick
in these stubs). Modes are AI_AUTONOMOUS and HUMAN_VS_AI; scores are floats.

## Strict File-Lease Rule (Zero Merge Conflicts)

| Track | Branch | ONLY edit |
| --- | --- | --- |
| 1 | feat/track1-rl | sheffield_env.py, train_rl.py |
| 2 | feat/track2-ollama | agent_orchestrator.py |
| 3 | feat/track3-ui | app.py |
| 4 | feat/track4-data | generate_assets.py, state_schema.py |

These leases apply to humans and coding agents. Do not edit another track's files.
Send schema requests to Track 4 and agree the interface before integration.
Shared configuration, docs and binary asset additions require coordination with
the integration owner; they are not implicitly leased to any track. Keep MP4s
local and preserve the tracked .gitkeep placeholders. Merge through the team's
integration workflow; do not rename keys or change units silently.

## Starter limitations

The environment uses five camera severities and immediate incident resolution;
road routing and spatial spread are extension points. The UI's Beat the AI panel
is a scoring sandbox pending PPO integration. Gold/Bronze output is advisory text
and does not execute dispatches. Storm events are synthetic. Nothing here is an
operational emergency service or verified live CCTV feed.

## 4-Stage Vehicle Lifecycle & Station Geography

Units start at their home_hub in EMERGENCY_HUBS. POL_1/POL_2 start at Shepcote
and Snig Hill; FIRE_1/FIRE_2 at Central Eyre and Rivelin; AMB_1/AMB_2 at
Northern General and Royal Hallamshire respectively.

Only is_unit_available(u) permits dispatch: status must be IDLE_AT_BASE and
 total_busy_ticks must be zero. EN_ROUTE, ON_SCENE, RETURNING and COOLDOWN
units are unavailable, including at stage boundaries where a timer is zero.

Dispatching from East Sheffield (POLICE_HQ_SHEPCOTE) to West Sheffield (CAM_10)
locks the vehicle for travel time + on-scene duration + return-to-station/hospital
trip + restock/handover cooldown. Travel follows unblocked OSMnx streets;
reject unreachable dispatches. Return to home_hub, then complete cooldown.

Timers are nonnegative integer ticks. total_busy_ticks is always eta_ticks +
on_scene_ticks_remaining + cooldown_ticks_remaining. eta_ticks represents the
current outward or return leg; compute the return leg when leaving the scene.
Thus this sum is a current timer summary, not an advance round-trip forecast.
The Track 1 helper documents transitions; routed movement and countdown updates
remain integration work. UI dispatch is disabled until that implementation is
available, and the existing PPO demo still uses abstract incident resolution.
