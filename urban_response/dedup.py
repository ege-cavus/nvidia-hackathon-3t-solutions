"""Duplicate detection at ingestion.

Several callers often report the same incident within minutes, from slightly different
positions and in different words. Each new report is scored against recent incidents:

  score = 0.35 * proximity + 0.20 * time + 0.30 * category + 0.15 * wording

  score >= DUPLICATE  -> duplicate: merged into the existing incident, nothing dispatched
  AMBIGUOUS..DUPLICATE -> the orchestrator (Qwen) decides; rules decide in mock mode
  nearby but lower     -> related: a different incident close to an active one; dispatched,
                         with the neighbour passed to the agents as scene context
  nothing nearby       -> new

Scoring is deterministic; only the borderline band goes to the LLM.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from .db import haversine_km
from .schemas import DedupDecision, DedupVerdict, ExecutedBy, Incident

RADIUS_M = 500  # beyond this, never the same incident
WINDOW_MIN = 60  # reports further apart in time are not compared
DUPLICATE = 0.72
AMBIGUOUS = 0.50
RELATED = 0.35  # beyond 400 m, a weaker match than this is treated as unrelated

CATEGORY_FAMILIES = [
    {"structure_fire", "high_rise_fire", "vehicle_fire", "wildfire"},
    {"rtc", "vehicle_fire"},
    {"assault", "stabbing", "firearms", "robbery", "public_disorder"},
    {"hazmat", "gas_leak", "chemical_spill"},
    {"medical", "cardiac_arrest", "overdose", "fall"},
]
_STOP = set("a an the and or of on in at to is was were with near outside by from for has have one two".split())


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9]+", text.lower()) if t not in _STOP and len(t) > 2}


def category_score(a: str, b: str) -> float:
    if a == b:
        return 1.0
    if any(a in f and b in f for f in CATEGORY_FAMILIES):
        return 0.6
    return 0.0


@dataclass
class Candidate:
    incident_id: str
    status: str
    incident: Incident
    score: float
    distance_m: float
    minutes_apart: float
    parts: dict


def score(new: Incident, old: Incident) -> Candidate | None:
    d_m = haversine_km(new.location.lat, new.location.lon, old.location.lat, old.location.lon) * 1000
    minutes = abs((new.received_at - old.received_at).total_seconds()) / 60
    if d_m > RADIUS_M or minutes > WINDOW_MIN:
        return None
    a, b = _tokens(new.description + " " + new.location.address), _tokens(old.description + " " + old.location.address)
    parts = {
        "proximity": round(1 - d_m / RADIUS_M, 3),
        "time": round(1 - minutes / WINDOW_MIN, 3),
        "category": category_score(new.category, old.category),
        "wording": round(len(a & b) / len(a | b), 3) if a | b else 0.0,
    }
    total = 0.35 * parts["proximity"] + 0.20 * parts["time"] + 0.30 * parts["category"] + 0.15 * parts["wording"]
    return Candidate(old.incident_id, "", old, round(total, 3), round(d_m), round(minutes, 1), parts)


def candidates(new: Incident, recent: list[dict]) -> list[Candidate]:
    out = []
    for r in recent:
        if r["incident_id"] == new.incident_id:
            continue
        c = score(new, r["incident"])
        if c:
            c.status = r["status"]
            out.append(c)
    return sorted(out, key=lambda c: c.score, reverse=True)


def rule_decision(new: Incident, cands: list[Candidate]) -> DedupDecision:
    """Deterministic verdict, used outright outside the ambiguous band and as the LLM fallback."""
    if not cands or (cands[0].distance_m > 400 and cands[0].score < RELATED):
        return DedupDecision(incident_id=new.incident_id, verdict=DedupVerdict.new,
                             reason="No comparable active incident within 500 m in the last hour.")
    best = cands[0]
    base = dict(incident_id=new.incident_id, match_incident_id=best.incident_id, score=best.score,
                distance_m=best.distance_m, minutes_apart=best.minutes_apart, executed_by=ExecutedBy.rules)
    same_scene = best.parts["category"] >= 0.6 and best.distance_m <= 150 and best.minutes_apart <= 15
    if best.score >= DUPLICATE or (best.score >= AMBIGUOUS and (same_scene or (best.parts["category"] == 1.0 and best.distance_m <= 200))):
        return DedupDecision(**base, verdict=DedupVerdict.duplicate,
                             reason=f"Same kind of incident {best.distance_m:.0f} m and {best.minutes_apart:.0f} min from {best.incident_id}.")
    return DedupDecision(**base, verdict=DedupVerdict.related,
                         reason=f"Different incident {best.distance_m:.0f} m from {best.incident_id}; sharing scene context.")


def is_ambiguous(cands: list[Candidate]) -> bool:
    return bool(cands) and AMBIGUOUS <= cands[0].score < DUPLICATE
