"""CR's final declaration of what is sent: the manifest.

A1-A3 each plan for their own speciality, so their combined requests overlap (three
agencies each asking for a drone and a command unit) and include nice-to-haves. CR
declares the final quantities. Code then enforces two guardrails:

  life-safety floors   CR may trim, but never below what life safety requires
                       (an ambulance per casualty, a rescue tender for trapped people,
                       two armed vehicles for a weapons call, ...). Floors only rise.
  sanity caps          no type above twice what the agencies asked for (or its floor),
                       and only types that exist in the Resource DB.

rule_manifest() is the deterministic policy used in mock mode and as CR's fallback.
"""

from __future__ import annotations

import math
from collections import defaultdict

from .agents.heuristics import CRIME_CATS, FIRE_CATS, HAZ_CATS
from .conflicts.hard import Demand
from .schemas import AgentResponse, Agency, Case, ManifestItem, RequestPriority, ResourceRequest, RoutingDecision

# One of these serves the whole scene, whichever agency asked.
SHARED_SCENE_TYPES = {"drone", "mobile_command_unit"}


def casualty_ambulances(n: int) -> int:
    return n if n <= 4 else 4 + math.ceil((n - 4) / 2)


def life_safety_floors(incident: Case, routing: RoutingDecision) -> dict[str, tuple[int, Agency, str]]:
    """resource_type -> (minimum quantity, agency it is for, reason). Only for tasked agencies."""
    tasked = set(routing.agencies)
    cat, floors = incident.category, {}
    casualties = max(incident.casualties_reported, incident.people_trapped)
    hazmat = cat in HAZ_CATS or any(k in h.lower() for h in incident.hazards for k in ("chem", "gas", "toxic"))
    armed = incident.weapons_involved or cat == "firearms"

    if Agency.ambulance in tasked and casualties:
        floors["emergency_ambulance"] = (casualty_ambulances(casualties), Agency.ambulance,
                                         f"one ambulance per casualty ({casualties} reported)")
        if hazmat or armed:
            floors["hart_team"] = (1, Agency.ambulance, "casualties inside a hazardous area")
    if Agency.ambulance in tasked and cat == "cardiac_arrest":
        floors["rapid_response_vehicle"] = (1, Agency.ambulance, "category 1 call")
    if Agency.fire in tasked:
        if cat in FIRE_CATS or hazmat:
            floors["fire_engine"] = (4 if cat == "high_rise_fire" else 2, Agency.fire, "pre-determined attendance")
        if cat == "high_rise_fire":
            floors["aerial_ladder_platform"] = (1, Agency.fire, "high-rise rescue")
        if incident.people_trapped:
            floors["rescue_tender"] = (1, Agency.fire, f"{incident.people_trapped} trapped")
        if hazmat:
            floors["hazmat_unit"] = (1, Agency.fire, "hazardous substance")
    if Agency.police in tasked and armed:
        floors["armed_response_vehicle"] = (2, Agency.police, "weapons reported")
    if Agency.police in tasked and (cat in CRIME_CATS or incident.severity >= 3):
        floors.setdefault("patrol_car", (2, Agency.police, "scene and cordon"))
    return floors


def requested_totals(responses: list[AgentResponse]) -> dict[str, int]:
    out: dict[str, int] = defaultdict(int)
    for r in responses:
        for q in r.resource_requests:
            out[q.resource_type] += q.quantity
    return dict(out)


def rule_manifest(incident: Case, routing: RoutingDecision, responses: list[AgentResponse]) -> list[ManifestItem]:
    """Merge overlapping requests, drop nice-to-haves on smaller incidents, keep essentials."""
    per: dict[tuple[str, str], ManifestItem] = {}
    shared: dict[str, ManifestItem] = {}
    for r in responses:
        for q in r.resource_requests:
            if q.priority == RequestPriority.desirable and incident.severity <= 3:
                continue
            if q.resource_type in SHARED_SCENE_TYPES:
                cur = shared.get(q.resource_type)
                owner = routing.lead_agency if any(
                    x.agency == routing.lead_agency and any(y.resource_type == q.resource_type for y in x.resource_requests)
                    for x in responses) else r.agency
                if not cur or q.quantity > cur.quantity:
                    shared[q.resource_type] = ManifestItem(resource_type=q.resource_type, quantity=max(q.quantity, cur.quantity if cur else 0),
                                                           for_agency=owner, priority=q.priority,
                                                           reason="one shared across agencies at the scene")
                continue
            key = (r.agency.value, q.resource_type)
            if key in per:
                per[key].quantity += q.quantity
                per[key].priority = min(per[key].priority, q.priority, key=lambda p: p.value)
            else:
                per[key] = ManifestItem(resource_type=q.resource_type, quantity=q.quantity, for_agency=r.agency,
                                        priority=q.priority, reason=(q.justification or "as requested")[:140])
    return list(per.values()) + list(shared.values())


def enforce(manifest: list[ManifestItem], incident: Case, routing: RoutingDecision,
            responses: list[AgentResponse], catalogue: dict[str, str]) -> tuple[list[ManifestItem], list[str]]:
    """Apply sanity caps and life-safety floors over a manifest. Returns (manifest, adjustments)."""
    notes: list[str] = []
    requested = requested_totals(responses)
    floors = life_safety_floors(incident, routing)
    out: list[ManifestItem] = []
    for m in manifest:
        if m.resource_type not in catalogue:
            notes.append(f"Dropped {m.resource_type}: not in the Resource DB.")
            continue
        cap = max(2 * requested.get(m.resource_type, 0), floors.get(m.resource_type, (0,))[0], 2)
        if m.quantity > cap:
            notes.append(f"Capped {m.resource_type} at {cap} (requested {requested.get(m.resource_type, 0)}).")
            m = m.model_copy(update={"quantity": cap})
        if m.quantity > 0:
            out.append(m)
    totals: dict[str, int] = defaultdict(int)
    for m in out:
        totals[m.resource_type] += m.quantity
    for rtype, (minimum, agency, why) in floors.items():
        have = totals.get(rtype, 0)
        if have < minimum:
            notes.append(f"Raised {rtype} from {have} to {minimum}: life-safety floor, {why}.")
            existing = next((m for m in out if m.resource_type == rtype and m.for_agency == agency), None)
            if existing:
                existing.quantity += minimum - have
                existing.priority = RequestPriority.essential
            else:
                out.append(ManifestItem(resource_type=rtype, quantity=minimum - have, for_agency=agency,
                                        priority=RequestPriority.essential, reason=f"life-safety floor: {why}"))
    return out, notes


def to_demands(manifest: list[ManifestItem], responses: list[AgentResponse]) -> list[Demand]:
    """Turn the manifest into allocator demands, keeping the agencies' ETA limits and durations."""
    out = []
    for m in manifest:
        src = [q for r in responses for q in r.resource_requests if q.resource_type == m.resource_type and r.agency == m.for_agency] \
            or [q for r in responses for q in r.resource_requests if q.resource_type == m.resource_type]
        etas = [q.max_eta_min for q in src if q.max_eta_min]
        out.append(Demand(m.for_agency, ResourceRequest(
            resource_type=m.resource_type, quantity=m.quantity, priority=m.priority,
            max_eta_min=min(etas) if etas else None,
            expected_duration_min=max((q.expected_duration_min for q in src), default=60),
            justification=m.reason,
        )))
    return out


def tasks(manifest: list[ManifestItem]) -> dict[tuple[str, str], str]:
    return {(m.for_agency.value, m.resource_type): m.reason for m in manifest}
