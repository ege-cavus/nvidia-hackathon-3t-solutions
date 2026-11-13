"""Hard conflicts: deterministic resource accounting.

Detection, the default resolution policy and the allocator are all pure code.
The orchestrator LLM may only choose among the options this module reports as
valid; whatever it picks is re-validated here before units are assigned.
"""

from __future__ import annotations

from collections import defaultdict
from dataclasses import dataclass

from ..db import Database, eta_minutes, haversine_km
from ..resources import crew_for
from ..schemas import (
    AgentResponse,
    Agency,
    CrewMember,
    ExecutedBy,
    HardConflict,
    HardConflictKind,
    HardResolution,
    Case,
    ResolutionAction,
    ResourceRequest,
    UnitAllocation,
)

# Acceptable stand-ins when a type runs out. Order = preference.
SUBSTITUTES: dict[str, list[str]] = {
    "rescue_tender": ["fire_engine"],
    "critical_care_car": ["rapid_response_vehicle"],
    "rapid_response_vehicle": ["emergency_ambulance"],
    "public_order_van": ["patrol_car"],
    "roads_policing_unit": ["patrol_car"],
    "mobile_command_unit": ["patrol_car"],
}

# Units may be pre-empted only from incidents at least this much less severe.
PREEMPT_SEVERITY_GAP = 2


@dataclass
class Demand:
    agency: Agency
    request: ResourceRequest


def as_demands(items) -> list[Demand]:
    """Accept agent responses (raw requests) or Demands (e.g. CR's manifest)."""
    out: list[Demand] = []
    for x in items:
        if isinstance(x, Demand):
            out.append(x)
        else:
            out.extend(Demand(x.agency, req) for req in x.resource_requests)
    return out


def _demand_by_type(items) -> dict[str, list[Demand]]:
    out: dict[str, list[Demand]] = defaultdict(list)
    for d in as_demands(items):
        out[d.request.resource_type].append(d)
    return out


def crew_demand(items, db: Database) -> dict[str, int]:
    """People needed per speciality to crew every vehicle that could actually be sent."""
    need: dict[str, int] = defaultdict(int)
    for rtype, demands in _demand_by_type(items).items():
        vehicles = min(sum(d.request.quantity for d in demands), len(db.available_units(rtype, 0, 0)))
        for spec, k in crew_for(rtype).items():
            need[spec] += k * vehicles
    return dict(need)


def detect(incident: Case, responses: list[AgentResponse], db: Database) -> list[HardConflict]:
    catalogue = db.resource_types()
    conflicts: list[HardConflict] = []
    loc = incident.location

    for rtype, demands in _demand_by_type(responses).items():
        agencies = sorted({d.agency for d in demands}, key=lambda a: a.value)
        requested = sum(d.request.quantity for d in demands)

        if rtype not in catalogue:
            conflicts.append(
                HardConflict(
                    kind=HardConflictKind.unknown_resource,
                    resource_type=rtype,
                    requested=requested,
                    available=0,
                    agencies=agencies,
                    detail=f"'{rtype}' does not exist in the Resource DB",
                )
            )
            continue

        owner = catalogue[rtype]
        for d in demands:
            if owner not in (Agency.shared.value, d.agency.value):
                conflicts.append(
                    HardConflict(
                        kind=HardConflictKind.ownership,
                        resource_type=rtype,
                        requested=d.request.quantity,
                        available=0,
                        agencies=[d.agency, Agency(owner)],
                        detail=f"{d.agency.value} requested {owner}-owned '{rtype}'; must be tasked via {owner}",
                    )
                )

        units = db.available_units(rtype, loc.lat, loc.lon)
        available = len(units)
        if requested > available:
            preemptible = [
                {"unit_id": u["unit_id"], "incident_id": u["incident_id"], "severity": u["severity"]}
                for u in db.committed_units(rtype)
                if u["severity"] <= incident.severity - PREEMPT_SEVERITY_GAP
            ]
            kind = HardConflictKind.contention if len(agencies) > 1 else HardConflictKind.shortfall
            conflicts.append(
                HardConflict(
                    kind=kind,
                    resource_type=rtype,
                    requested=requested,
                    available=available,
                    agencies=agencies,
                    detail=(
                        f"{requested} {rtype} requested"
                        + (f" by {', '.join(a.value for a in agencies)}" if len(agencies) > 1 else "")
                        + f", {available} available"
                    ),
                    preemptible=preemptible,
                )
            )
            continue

        # Enough units overall; check each ETA-bounded request against units in range.
        for d in demands:
            if d.request.max_eta_min is None:
                continue
            in_range = sum(1 for u in units if u["eta_min"] <= d.request.max_eta_min)
            if in_range < d.request.quantity:
                conflicts.append(
                    HardConflict(
                        kind=HardConflictKind.eta_unmet,
                        resource_type=rtype,
                        requested=d.request.quantity,
                        available=in_range,
                        agencies=[d.agency],
                        detail=(
                            f"{d.agency.value} needs {d.request.quantity} {rtype} within "
                            f"{d.request.max_eta_min:g} min; only {in_range} in range"
                        ),
                    )
                )
    # People: enough vehicles is not enough if nobody with the right speciality is free to crew them.
    by_type = _demand_by_type(responses)
    for spec, needed in crew_demand(responses, db).items():
        free = len(db.available_personnel(spec))
        if needed > free:
            agencies = sorted({d.agency for t, ds in by_type.items() if spec in crew_for(t) for d in ds}, key=lambda a: a.value)
            types = sorted(t for t in by_type if spec in crew_for(t))
            conflicts.append(
                HardConflict(
                    kind=HardConflictKind.crew_shortfall,
                    resource_type=spec,
                    requested=needed,
                    available=free,
                    agencies=agencies,
                    detail=f"{needed} {spec.replace('_', ' ')}s needed to crew {', '.join(types)}; {free} on shift and free",
                )
            )
    return conflicts


def demand_totals(responses: list[AgentResponse]) -> dict[str, int]:
    """Total units requested per resource type in this plan."""
    return {t: sum(d.request.quantity for d in ds) for t, ds in _demand_by_type(responses).items()}


def spare_substitutes(rtype: str, db: Database, demand: dict[str, int] | None = None) -> list[tuple[str, int]]:
    """Substitute types with units left over after this plan's own demand for them."""
    demand = demand or {}
    out = []
    for sub in SUBSTITUTES.get(rtype, []):
        spare = len(db.available_units(sub, 0, 0)) - demand.get(sub, 0)
        if spare > 0:
            out.append((sub, spare))
    return out


def resolution_options(conflict: HardConflict, db: Database, demand: dict[str, int] | None = None) -> list[ResolutionAction]:
    """Actions that are mechanically valid for this conflict."""
    if conflict.kind == HardConflictKind.unknown_resource:
        return [ResolutionAction.accept_partial]
    if conflict.kind == HardConflictKind.ownership:
        return [ResolutionAction.accept_partial]  # allocate anyway, flagged for the owning agency
    if conflict.kind == HardConflictKind.eta_unmet:
        return [ResolutionAction.relax_eta, ResolutionAction.accept_partial]
    if conflict.kind == HardConflictKind.crew_shortfall:
        return [ResolutionAction.mutual_aid, ResolutionAction.accept_partial]
    opts = [ResolutionAction.accept_partial, ResolutionAction.mutual_aid]
    if conflict.preemptible:
        opts.insert(0, ResolutionAction.preempt)
    if spare_substitutes(conflict.resource_type, db, demand):
        opts.append(ResolutionAction.substitute)
    return opts


def default_resolution(
    conflict: HardConflict, incident: Case, db: Database, demand: dict[str, int] | None = None
) -> HardResolution:
    """Rule-based policy used in mock mode and whenever the orchestrator fails."""
    opts = resolution_options(conflict, db, demand)
    gap = conflict.requested - conflict.available
    rtype = conflict.resource_type
    aid = incident.severity >= 3  # request mutual aid for anything left uncovered

    def res(action: ResolutionAction, rationale: str, **kw) -> HardResolution:
        return HardResolution(resource_type=rtype, action=action, rationale=rationale, executed_by=ExecutedBy.rules, **kw)

    if ResolutionAction.relax_eta in opts:
        return res(ResolutionAction.relax_eta, "Units exist but are further away; send nearest available.")
    if ResolutionAction.preempt in opts and incident.severity >= 4:
        take = [p["unit_id"] for p in conflict.preemptible[:gap]]
        short = gap - len(take)
        return res(ResolutionAction.preempt,
                   f"Severity {incident.severity} incident; reassign {len(take)} unit(s) from lower-severity incidents"
                   + (f", mutual aid for remaining {short}." if short and aid else "."),
                   preempt_unit_ids=take, mutual_aid_for_remainder=aid and short > 0)
    if ResolutionAction.substitute in opts:
        sub, spare = spare_substitutes(rtype, db, demand)[0]
        short = gap - spare
        return res(ResolutionAction.substitute,
                   f"Cover the {gap} missing {rtype} with {sub}" + (f", mutual aid for remaining {short}." if short > 0 and aid else "."),
                   substitute_type=sub, mutual_aid_for_remainder=aid and short > 0)
    if ResolutionAction.mutual_aid in opts and aid:
        return res(ResolutionAction.mutual_aid, f"Request {gap} {rtype} from neighbouring services.")
    return res(ResolutionAction.accept_partial, "Dispatch what is available and log the gap.")


def validate_resolution(
    res: HardResolution, conflict: HardConflict, db: Database, demand: dict[str, int] | None = None
) -> HardResolution | None:
    """Reject LLM choices that are not mechanically valid."""
    if res.action not in resolution_options(conflict, db, demand):
        return None
    if res.action == ResolutionAction.preempt:
        allowed = {p["unit_id"] for p in conflict.preemptible}
        res.preempt_unit_ids = [u for u in res.preempt_unit_ids if u in allowed]
        if not res.preempt_unit_ids:
            return None
    if res.action == ResolutionAction.substitute:
        if res.substitute_type not in {t for t, _ in spare_substitutes(conflict.resource_type, db, demand)}:
            return None
    return res


def allocate(
    incident: Case,
    responses,
    resolutions: list[HardResolution],
    lead: Agency,
    db: Database,
    tasks: dict[tuple[str, str], str] | None = None,
) -> tuple[list[UnitAllocation], list[dict]]:
    """Assign concrete, fully crewed units.

    Deterministic: essential first, lead agency first, nearest vehicle first; each vehicle
    gets the crew its type requires, from its own station first, then the nearest stations.
    A vehicle that cannot be crewed is skipped.
    """
    loc = incident.location
    res_by_type = {r.resource_type: r for r in resolutions}
    crew_aid = any(r.action == ResolutionAction.mutual_aid for r in resolutions if r.resource_type not in db.resource_types())
    catalogue = db.resource_types()
    tasks = tasks or {}
    taken: set[str] = set()
    taken_people: set[str] = set()
    people_cache: dict[str, list[dict]] = {}
    allocations: list[UnitAllocation] = []
    unmet: list[dict] = []

    def pool(rtype: str, max_eta: float | None) -> list[dict]:
        units = [u for u in db.available_units(rtype, loc.lat, loc.lon) if u["unit_id"] not in taken]
        return [u for u in units if max_eta is None or u["eta_min"] <= max_eta]

    def pick_crew(unit: dict) -> list[CrewMember] | None:
        crew: list[CrewMember] = []
        for spec, k in crew_for(unit["resource_type"]).items():
            if spec not in people_cache:
                people_cache[spec] = db.available_personnel(spec)
            free = [p for p in people_cache[spec] if p["person_id"] not in taken_people]
            free.sort(key=lambda p: (p["station"] != unit["station"], haversine_km(p["lat"], p["lon"], unit["lat"], unit["lon"])))
            if len(free) < k:
                return None
            crew += [CrewMember(person_id=p["person_id"], speciality=spec, station=p["station"]) for p in free[:k]]
        return crew

    def assign(u: dict, rtype: str, agency: Agency, crew: list[CrewMember], preempted_from: str | None = None) -> None:
        taken.add(u["unit_id"])
        taken_people.update(m.person_id for m in crew)
        allocations.append(
            UnitAllocation(
                unit_id=u["unit_id"], callsign=u["callsign"], resource_type=rtype, agency=Agency(u["agency"]),
                station=u["station"], eta_min=u["eta_min"], requested_by=agency, preempted_from=preempted_from,
                crew=crew, task=tasks.get((agency.value, rtype), ""),
            )
        )

    def take_from(candidates: list[dict], rtype: str, agency: Agency, need: int) -> tuple[int, int]:
        """Assign up to `need` crewed vehicles. Returns (assigned, skipped_for_crew)."""
        got = skipped = 0
        for u in candidates:
            if got >= need:
                break
            crew = pick_crew(u)
            if crew is None:
                skipped += 1
                continue
            assign(u, rtype, agency, crew)
            got += 1
        return got, skipped

    demands = as_demands(responses)
    demands.sort(key=lambda d: (d.request.priority.value, d.agency != lead, d.agency.value))

    for d in demands:
        rtype, need = d.request.resource_type, d.request.quantity
        res = res_by_type.get(rtype)
        if rtype not in catalogue and not (res and res.action == ResolutionAction.substitute):
            unmet.append({"resource_type": rtype, "quantity": need, "requested_by": d.agency.value, "reason": "unknown resource"})
            continue

        max_eta = d.request.max_eta_min
        if res and res.action == ResolutionAction.relax_eta:
            max_eta = None
        crew_blocked = 0
        if rtype in catalogue:
            got, crew_blocked = take_from(pool(rtype, max_eta), rtype, d.agency, need)
            need -= got

        if need and res and res.action == ResolutionAction.preempt:
            for uid in list(res.preempt_unit_ids):
                if not need:
                    break
                if uid in taken:
                    continue
                unit = db.unit(uid)
                holder = next((c for c in db.committed_units(rtype) if c["unit_id"] == uid), None)
                if unit and holder:
                    unit["eta_min"] = eta_minutes(holder["lat"], holder["lon"], loc.lat, loc.lon)
                    crew = [CrewMember(person_id=m["person_id"], speciality=m["speciality"], station=m["station"])
                            for m in db.crew_of_unit(uid)]  # the crew moves with its vehicle
                    assign(unit, rtype, d.agency, crew, preempted_from=holder["incident_id"])
                    need -= 1

        if need and res and res.action == ResolutionAction.substitute and res.substitute_type:
            got, _ = take_from(pool(res.substitute_type, None), res.substitute_type, d.agency, need)
            need -= got

        if need:
            aid = res and (res.action == ResolutionAction.mutual_aid or res.mutual_aid_for_remainder)
            if crew_blocked and not aid:
                reason = "crew mutual aid requested" if crew_aid else "no crew available"
            else:
                reason = "mutual aid requested" if aid else "insufficient resources"
            unmet.append({"resource_type": rtype, "quantity": need, "requested_by": d.agency.value, "reason": reason})

    return allocations, unmet
