"""Vehicle lifecycle timing: when a dispatched vehicle comes back into the inventory.

Follows the 4-stage lifecycle used on origin/main (EN_ROUTE, ON_SCENE, RETURNING, COOLDOWN,
then IDLE_AT_BASE). A vehicle and its crew are unavailable until the whole cycle ends:

  en_route   travel from its station to the scene (the allocator's ETA)
  on_scene   how long the requesting agency expects to need it (expected_duration_min)
  returning  back to its station; an ambulance carrying a casualty goes via the receiving
             hospital (major trauma centre at severity 4+) and then to its station
  cooldown   restock / clean / handover before it can be dispatched again (per type, below)

Minutes are simulated minutes; db.time_scale() (UR_TIME_SCALE) converts them to wall-clock
time, so a demo can watch the fleet replenish. Figures are illustrative, not operational.
"""

from __future__ import annotations

from datetime import datetime, timezone

from .db import Database, eta_minutes

STAGES = ("en_route", "on_scene", "returning", "cooldown")

COOLDOWN_MIN: dict[str, tuple[int, str]] = {
    "fire_engine": (20, "refill breathing apparatus and water, restow hose"),
    "aerial_ladder_platform": (15, "check and restow the platform"),
    "rescue_tender": (20, "restock rescue equipment"),
    "hazmat_unit": (45, "decontaminate the crew and equipment"),
    "water_rescue_unit": (30, "dry and check boats and suits"),
    "emergency_ambulance": (15, "clean and restock"),
    "rapid_response_vehicle": (5, "restock drugs and kit"),
    "critical_care_car": (10, "restock drugs and kit"),
    "hart_team": (30, "decontaminate and restock"),
    "patrol_car": (5, "complete paperwork, refuel"),
    "armed_response_vehicle": (10, "re-arm and debrief"),
    "dog_unit": (10, "rest and water the dog"),
    "public_order_van": (10, "kit check"),
    "roads_policing_unit": (5, "upload collision evidence"),
    "drone": (10, "swap and charge batteries"),
    "mobile_command_unit": (20, "pack down and restock"),
}
HOSPITAL_HANDOVER_MIN = 15
DEFAULT_ON_SCENE_MIN = 60


def timeline(db: Database, unit: dict, resource_type: str, eta_min: float, on_scene_min: int | None,
             scene_lat: float, scene_lon: float, severity: int, conveys: bool) -> list[dict]:
    """Stages with simulated minutes for one dispatched vehicle."""
    on_scene = float(on_scene_min or DEFAULT_ON_SCENE_MIN)
    stages = [
        {"stage": "en_route", "minutes": round(eta_min, 1), "detail": f"from {unit['station']}"},
        {"stage": "on_scene", "minutes": on_scene, "detail": "as requested by the agency"},
    ]
    if conveys:
        hospitals = db.hospitals(scene_lat, scene_lon)
        trauma = [h for h in hospitals if h["major_trauma"]] if severity >= 4 else []
        h = (trauma or hospitals)[0]
        to_hospital = h["eta_min"]
        back = eta_minutes(h["lat"], h["lon"], unit["lat"], unit["lon"])
        stages.append({"stage": "returning", "minutes": round(to_hospital + back, 1),
                       "detail": f"conveying to {h['name']}, then to {unit['station']}", "hospital": h["name"]})
        cool, why = COOLDOWN_MIN.get(resource_type, (10, "restock"))
        stages.append({"stage": "cooldown", "minutes": float(HOSPITAL_HANDOVER_MIN + cool),
                       "detail": f"patient handover ({HOSPITAL_HANDOVER_MIN} min), then {why}"})
    else:
        back = eta_minutes(scene_lat, scene_lon, unit["lat"], unit["lon"])
        stages.append({"stage": "returning", "minutes": round(back, 1), "detail": f"to {unit['station']}"})
        cool, why = COOLDOWN_MIN.get(resource_type, (10, "restock"))
        stages.append({"stage": "cooldown", "minutes": float(cool), "detail": why})
    return stages


def total_minutes(stages: list[dict]) -> float:
    return round(sum(s["minutes"] for s in stages), 1)


def current_stage(stages: list[dict], start: datetime, scale: float, now: datetime | None = None) -> dict:
    """Where the vehicle is in its cycle now: {stage, stage_left_min, back_in_service_min} in simulated minutes."""
    now = now or datetime.now(timezone.utc)
    elapsed = (now - start).total_seconds() / 60 * scale
    acc = 0.0
    for s in stages:
        if elapsed < acc + s["minutes"]:
            return {"stage": s["stage"], "detail": s.get("detail", ""), "stage_left_min": round(acc + s["minutes"] - elapsed, 1),
                    "back_in_service_min": round(total_minutes(stages) - elapsed, 1)}
        acc += s["minutes"]
    return {"stage": "due_back", "detail": "", "stage_left_min": 0.0, "back_in_service_min": 0.0}
