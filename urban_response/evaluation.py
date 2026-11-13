"""Success metrics for a dispatched response, computed in code.

Qwen reads these numbers and gives the verdict; rule_evaluation() is the deterministic
version used in mock mode and as the fallback. Targets are illustrative.
"""

from __future__ import annotations

from collections import Counter

from .resources import crew_for
from .schemas import Evaluation, ExecutedBy, Incident, IncidentResponse, RequestPriority


def arrival_target_min(agency: str, incident: Incident) -> float:
    urgent = incident.severity >= 4 or incident.weapons_involved or incident.category == "cardiac_arrest"
    return {"ambulance": 7 if urgent else 18, "fire": 8, "police": 10 if urgent else 15}.get(agency, 15)


def compute_metrics(incident: Incident, r: IncidentResponse) -> dict:
    manifest_units = sum(m.quantity for m in r.manifest)
    essential_units = sum(m.quantity for m in r.manifest if m.priority == RequestPriority.essential)
    requested_units = sum(r.requested_total.values())
    dispatched = len(r.allocations)
    essential_gap = sum(u["quantity"] for u in r.unmet
                        if any(m.resource_type == u["resource_type"] and m.priority == RequestPriority.essential for m in r.manifest))
    first: dict[str, float] = {}
    for a in r.allocations:
        k = a.requested_by.value
        first[k] = min(first.get(k, 1e9), a.eta_min)
    eta = {k: {"first_arrival_min": v, "target_min": arrival_target_min(k, incident), "met": v <= arrival_target_min(k, incident)}
           for k, v in first.items()}
    crewed = sum(1 for a in r.allocations if Counter(m.speciality for m in a.crew) == Counter(crew_for(a.resource_type)))
    resolutions_llm = sum(1 for x in r.hard_resolutions if x.executed_by in (ExecutedBy.nemotron, ExecutedBy.qwen))
    return {
        "requested_units": requested_units,
        "manifest_units": manifest_units,
        "trimmed_units": max(requested_units - manifest_units, 0),
        "dispatched_units": dispatched,
        "personnel_dispatched": sum(len(a.crew) for a in r.allocations),
        "fill_rate": round(dispatched / manifest_units, 3) if manifest_units else 1.0,
        "essential_fill_rate": round(max(essential_units - essential_gap, 0) / essential_units, 3) if essential_units else 1.0,
        "essential_gap_units": essential_gap,
        "arrival": eta,
        "arrival_targets_met": round(sum(e["met"] for e in eta.values()) / len(eta), 3) if eta else 0.0,
        "crew_complete_rate": round(crewed / dispatched, 3) if dispatched else 0.0,
        "hard_conflicts": len(r.hard_conflicts),
        "resolved_by_cr": resolutions_llm,
        "rule_fallbacks": sum(1 for x in r.hard_resolutions if x.executed_by == ExecutedBy.fallback),
        "soft_conflicts": len(r.soft_conflicts),
        "mutual_aid_requests": sum(1 for u in r.unmet if "mutual aid" in u["reason"]),
        "preempted_units": sum(1 for a in r.allocations if a.preempted_from),
        "floors_applied": len(r.manifest_adjustments),
    }


def grade(score: int) -> str:
    return "A" if score >= 90 else "B" if score >= 75 else "C" if score >= 60 else "D" if score >= 45 else "F"


def rule_evaluation(incident: Incident, r: IncidentResponse, m: dict, headline: str, dek: str) -> Evaluation:
    fallback_share = m["rule_fallbacks"] / max(len(r.hard_resolutions), 1)
    score = round(100 * (0.45 * m["essential_fill_rate"] + 0.25 * m["arrival_targets_met"]
                         + 0.15 * m["crew_complete_rate"] + 0.15 * (1 - fallback_share)))
    if m["essential_fill_rate"] >= 1 and m["arrival_targets_met"] >= 0.67:
        verdict = "resolved"
    elif m["essential_fill_rate"] < 0.7 or not r.allocations:
        verdict = "follow_up_required"
    else:
        verdict = "resolved_with_gaps"
    findings = []
    late = [f"{k} first arrival {v['first_arrival_min']:.0f} min vs {v['target_min']:.0f} target" for k, v in m["arrival"].items() if not v["met"]]
    if late:
        findings.append("Late: " + "; ".join(late) + ".")
    if m["essential_gap_units"]:
        findings.append(f"{m['essential_gap_units']} essential units not covered locally; mutual aid requested.")
    if m["trimmed_units"]:
        findings.append(f"{m['trimmed_units']} overlapping or non-essential units held back by CR.")
    if m["preempted_units"]:
        findings.append(f"{m['preempted_units']} units reassigned from lower-severity incidents.")
    return Evaluation(score=score, grade=grade(score), verdict=verdict, metrics=m, findings=findings[:4],
                      headline=headline, dek=dek, executed_by=ExecutedBy.rules)
