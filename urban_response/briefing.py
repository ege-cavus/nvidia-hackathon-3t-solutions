"""Normalised, human-facing output: the incident log and the phone brief.

Dispatchers and crews read this on a phone, so it is written like a wire-service log:
short declarative sentences, times, numbers, places. No model reasoning appears here.
Log lines are templates over structured data; the only model-written text is CR's
orders (length-capped, imperative) and Qwen's headline and dek.
"""

from __future__ import annotations

import re
from collections import Counter
from datetime import datetime, timedelta, timezone

from .db import Database
from .schemas import Directive, Evaluation, Incident, IncidentResponse

TITLE = {
    "rtc": "Road collision", "structure_fire": "Building fire", "high_rise_fire": "High-rise fire", "vehicle_fire": "Vehicle fire",
    "hazmat": "Hazardous materials incident", "gas_leak": "Gas leak", "flood": "Flooding", "medical": "Medical emergency",
    "cardiac_arrest": "Cardiac arrest", "assault": "Assault", "stabbing": "Stabbing", "firearms": "Firearms incident",
    "public_disorder": "Public disorder",
}


def title(cat: str) -> str:
    return TITLE.get(cat, cat.replace("_", " ").capitalize())


def place(incident: Incident) -> str:
    return incident.location.address or incident.location.district or f"{incident.location.lat:.4f}, {incident.location.lon:.4f}"


def first_sentence(text: str, limit: int = 160) -> str:
    s = re.split(r"(?<=[.!?])\s", text.strip(), maxsplit=1)[0]
    return s if len(s) <= limit else s[: limit - 1].rstrip() + "…"


def clean(text: str, limit: int = 180) -> str:
    text = " ".join(text.split())
    text = text[:1].upper() + text[1:]
    if len(text) > limit:
        text = text[: limit - 1].rstrip(" ,;") + "…"
    return text if text.endswith((".", "…", "!", "?")) else text + "."


def normalise_directives(directives: list[Directive]) -> list[Directive]:
    order = {"immediate": 0, "high": 1, "routine": 2}
    seen, out = set(), []
    for d in directives:
        action = clean(d.action)
        key = (d.agency, action.lower())
        if action and key not in seen:
            seen.add(key)
            out.append(d.model_copy(update={"action": action}))
    return sorted(out, key=lambda d: order[d.priority])


def hhmm(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%H:%M")


# ---- log lines --------------------------------------------------------------- #


def log_received(i: Incident) -> str:
    parts = [f"{title(i.category)} reported at {place(i)}.", first_sentence(i.description)]
    people = []
    if i.casualties_reported:
        people.append(f"{i.casualties_reported} injured")
    if i.people_trapped:
        people.append(f"{i.people_trapped} trapped")
    if people:
        parts.append(", ".join(people).capitalize() + ".")
    return " ".join(parts)


def log_routed(lead: str, agencies: list[str]) -> str:
    others = [a for a in agencies if a != lead]
    return f"{lead.capitalize()} leads." + (f" {', '.join(o.capitalize() for o in others)} assisting." if others else "")


def log_dispatched(r: IncidentResponse) -> str:
    if not r.allocations:
        return "No units could be dispatched."
    stations = {a.station for a in r.allocations}
    people = sum(len(a.crew) for a in r.allocations)
    first = min(a.eta_min for a in r.allocations)
    eta_clock = hhmm(datetime.now(timezone.utc) + timedelta(minutes=first))
    return (f"{len(r.allocations)} vehicles and {people} personnel dispatched from {len(stations)} "
            f"station{'s' if len(stations) != 1 else ''}. First arrival expected {eta_clock} ({first:.0f} min).")


def log_trimmed(requested: int, manifest: int) -> str | None:
    if requested > manifest:
        return f"Agency requests consolidated from {requested} to {manifest} units."
    if manifest > requested:
        return f"Response raised from {requested} to {manifest} units for life safety."
    return None


def log_gaps(r: IncidentResponse) -> str | None:
    if not r.unmet:
        return None
    items = ", ".join(f"{u['quantity']} {u['resource_type'].replace('_', ' ')}" for u in r.unmet[:5])
    aid = any("mutual aid" in u["reason"] for u in r.unmet)
    return f"Shortfall: {items}." + (" Mutual aid requested from neighbouring services." if aid else "")


def log_evaluated(e: Evaluation) -> str:
    verdict = {"resolved": "Response complete.", "resolved_with_gaps": "Response complete with gaps.",
               "follow_up_required": "Follow-up required."}[e.verdict]
    return f"Response rated {e.score}/100 (grade {e.grade}). {verdict}"


def default_headline(i: Incident, r: IncidentResponse | None) -> tuple[str, str]:
    lead = r.routing.lead_agency.value.capitalize() if r else "Crews"
    n = len(r.allocations) if r else 0
    head = f"{title(i.category)} on {place(i).split(',')[0]}: {n} units sent"
    people = []
    if i.casualties_reported:
        people.append(f"{i.casualties_reported} injured")
    if i.people_trapped:
        people.append(f"{i.people_trapped} trapped")
    dek = f"{lead} leads the response" + (f"; {', '.join(people)}" if people else "") + f". Severity {i.severity} of 5."
    return head[:90], dek[:220]


# ---- phone brief -------------------------------------------------------------- #


def build_brief(db: Database, incident_id: str) -> dict | None:
    row = db.get_incident(incident_id)
    if not row:
        return None
    if row.get("duplicate_of"):
        parent = build_brief(db, row["duplicate_of"])
        if parent:
            parent["opened_from_duplicate"] = incident_id
        return parent
    inc = Incident.model_validate(row["incident"])
    resp = None
    if row["responses"]:
        stored = db.get_response(row["responses"][-1])
        resp = IncidentResponse.model_validate(stored) if stored else None
    ev = resp.evaluation if resp else None
    head, dek = default_headline(inc, resp)
    lead_resp = next((a for a in resp.agent_responses if a.agency == resp.routing.lead_agency), None) if resp else None

    units = []
    for a in (resp.allocations if resp else []):
        units.append({
            "callsign": a.callsign, "type": a.resource_type.replace("_", " "), "agency": a.agency.value,
            "station": a.station, "eta_min": a.eta_min, "task": a.task, "reassigned_from": a.preempted_from,
            "crew": [f"{n} {s.replace('_', ' ')}" for s, n in Counter(m.speciality for m in a.crew).items()],
        })
    orders: dict[str, list[dict]] = {}
    for d in (resp.directives if resp else []):
        orders.setdefault(d.agency, []).append({"priority": d.priority, "action": d.action})

    return {
        "incident_id": incident_id,
        "status": row["status"],
        "headline": (ev.headline if ev and ev.headline else head),
        "dek": (ev.dek if ev and ev.dek else dek),
        "dateline": f"SHEFFIELD, {hhmm(inc.received_at)} GMT",
        "category": title(inc.category),
        "severity": inc.severity,
        "place": place(inc),
        "location": {"lat": inc.location.lat, "lon": inc.location.lon},
        "facts": {
            "Lead": resp.routing.lead_agency.value.capitalize() if resp else "-",
            "Agencies": ", ".join(a.value.capitalize() for a in resp.routing.agencies) if resp else "-",
            "Injured": inc.casualties_reported, "Trapped": inc.people_trapped,
            "Hazards": ", ".join(inc.hazards) or "none reported",
            "RVP": (lead_resp.scene_constraints.staging_area if lead_resp and lead_resp.scene_constraints.staging_area else "-"),
            "Reports": row["report_count"],
        },
        "orders": orders,
        "units": units,
        "gaps": [f"{u['quantity']} {u['resource_type'].replace('_', ' ')} ({u['reason']})" for u in (resp.unmet if resp else [])],
        "log": db.incident_log(incident_id),
        "evaluation": ev.model_dump(mode="json") if ev else None,
        "merged_reports": [{"incident_id": m["incident_id"], "received_at": m["received_at"],
                            "place": m["incident"]["location"].get("address", "")} for m in row["merged_reports"]],
        "related": resp.related_incidents if resp else [],
    }
