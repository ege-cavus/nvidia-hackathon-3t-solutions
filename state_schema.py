"""Shared SURU contract. Coordinates supplied by the team; assets are local placeholders.

Incidents are keyed by camera ID. Vehicle timers are integer one-minute ticks; only AVAILABLE units are dispatchable.
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
    _camera["cosmos_video_path"] = f"assets/hero_cams_disaster/{_camera_id}.mp4"
    _camera["frame_path"] = f"assets/hero_cams_disaster/{_camera_id}.jpg"
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


AGENCIES = ("FIRE", "POLICE", "AMBULANCE")


def is_unit_available(unit: dict) -> bool:
    return unit["status"] == "AVAILABLE" and unit["total_depletion_ticks_remaining"] == 0


def make_incident(cam_id: str, hazard: str, severity: int, tick: int, road_blocked=False) -> dict:
    if cam_id not in HERO_CAMERAS or hazard not in ("flood", "fire", "trauma"):
        raise ValueError("Unknown camera or hazard")
    if type(severity) is not int or not 1 <= severity <= 10:
        raise ValueError("Severity must be an integer 1..10")
    cam = HERO_CAMERAS[cam_id]
    return {"cam_id": cam_id, "location": {"lat": cam["lat"], "lon": cam["lon"]},
            "hazard": hazard, "severity": severity, "road_blocked": bool(road_blocked),
            "video_path": cam["cosmos_video_path"], "spawn_tick": tick,
            "qwen_report": {"summary": "", "required_agencies": [], "timestamp": tick},
            "status": "AWAITING_QWEN_TRIAGE", "response_ticks": None,
            "sla_recorded": False, "spillover_recorded": False}


def get_initial_city_state() -> dict:
    from knowledge_base import NODE_HISTORICAL_PROBABILITIES
    assignments = {
        "POL_1": "POLICE_HQ_SHEPCOTE", "POL_2": "POLICE_SNIG_HILL",
        "FIRE_1": "FIRE_CENTRAL_EYRE", "FIRE_2": "FIRE_RIVELIN",
        "AMB_1": "NHS_NORTHERN_GEN", "AMB_2": "NHS_ROYAL_HALLAM",
    }
    resources = {}
    for unit_id, hub_id in assignments.items():
        hub = EMERGENCY_HUBS[hub_id]
        resources[unit_id] = {
            "agency": "AMBULANCE" if hub["type"] == "ambulance" else hub["type"].upper(),
            "home_hub": hub_id, "lat": hub["lat"], "lon": hub["lon"],
            "status": "AVAILABLE", "assigned_cam": None, "travel_to_scene_ticks": 0,
            "on_scene_ticks": 0, "return_to_base_ticks": 0,
            "cooldown_ticks_remaining": 0, "total_depletion_ticks_remaining": 0,
        }
    return deepcopy({
        "tick": 0, "effectiveness_metrics": {
            "overall_score": 100.0, "incidents_resolved": 0, "sla_met_count": 0,
            "sla_missed_count": 0, "spillovers_prevented": 0,
            "spillovers_occurred": 0, "avg_response_ticks": 0.0,
        },
        "node_probabilities": NODE_HISTORICAL_PROBABILITIES,
        "active_incidents": {}, "resource_db": resources, "blocked_edges": [],
        "message_board": [], "routing": {"mode": "not_loaded", "tick_seconds": 60},
    })
