"""Shared SURU contract. Coordinates supplied by the team; assets are local placeholders.

Incidents are keyed by incident ID. Vehicle timers are integer ticks; only IDLE_AT_BASE units are dispatchable.
Hazards are flood/fire/trauma. blocked_edges holds OSM (u, v, key) tuples.
Every caller owns its returned mutable state. No global CITY_STATE singleton.
"""
from copy import deepcopy

HERO_CAMERAS = {
    "CAM_01": {"name": "Sheaf St / Sheffield Station", "lat": 53.3782, "lon": -1.4628},
    "CAM_08": {"name": "Park Square Roundabout", "lat": 53.3831, "lon": -1.4589},
    "CAM_10": {"name": "Penistone Rd / Shalesmoor", "lat": 53.3912, "lon": -1.4815},
    "CAM_13": {"name": "Bernard Rd / Sheffield Parkway", "lat": 53.3805, "lon": -1.4450},
    "CAM_21": {"name": "Meadowhall Way", "lat": 53.4141, "lon": -1.4116},
}
for _camera_id, _camera in HERO_CAMERAS.items():
    _camera["clean_video_path"] = f"assets/hero_cams_clean/{_camera_id}.mp4"
    _camera["disaster_video_path"] = f"assets/hero_cams_disaster/{_camera_id}.mp4"

EMERGENCY_HUBS = {
    "POLICE_HQ_SHEPCOTE": {"name": "SYP Ops Complex (Shepcote Ln)", "type": "police", "lat": 53.4021, "lon": -1.4125},
    "POLICE_SNIG_HILL": {"name": "Snig Hill Police Station", "type": "police", "lat": 53.3846, "lon": -1.4678},
    "FIRE_CENTRAL_EYRE": {"name": "Central Fire Station (Eyre St)", "type": "fire", "lat": 53.3772, "lon": -1.4746},
    "FIRE_RIVELIN": {"name": "Rivelin Valley Fire Station", "type": "fire", "lat": 53.4015, "lon": -1.5220},
    "NHS_NORTHERN_GEN": {"name": "Northern General Hospital A&E", "type": "ambulance", "lat": 53.4098, "lon": -1.4562},
    "NHS_ROYAL_HALLAM": {"name": "Royal Hallamshire Hospital", "type": "ambulance", "lat": 53.3785, "lon": -1.4942},
}


def is_unit_available(unit: dict) -> bool:
    return unit["status"] == "IDLE_AT_BASE" and unit["total_busy_ticks"] == 0


def get_initial_city_state() -> dict:
    """Return a fresh, JSON-friendly simulation state (except edge tuples)."""
    units = {}
    assignments = {
        "POL_1": "POLICE_HQ_SHEPCOTE", "POL_2": "POLICE_SNIG_HILL",
        "FIRE_1": "FIRE_CENTRAL_EYRE", "FIRE_2": "FIRE_RIVELIN",
        "AMB_1": "NHS_NORTHERN_GEN", "AMB_2": "NHS_ROYAL_HALLAM",
    }
    for unit_id, hub_id in assignments.items():
        hub = EMERGENCY_HUBS[hub_id]
        units[unit_id] = {
            "type": hub["type"], "home_hub": hub_id,
            "lat": hub["lat"], "lon": hub["lon"], "status": "IDLE_AT_BASE",
            "target_cam": None, "eta_ticks": 0, "on_scene_ticks_remaining": 0,
            "cooldown_ticks_remaining": 0, "total_busy_ticks": 0,
        }
    return deepcopy({
        "tick": 0, "mode": "AI_AUTONOMOUS", "score_ai": 0.0, "score_human": 0.0,
        "rl_priority_weights": {"flood": 1.0, "fire": 1.0, "trauma": 1.0},
        "active_incidents": {}, "units": units, "blocked_edges": [], "comms_log": [],
    })
