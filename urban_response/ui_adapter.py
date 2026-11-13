"""Adapter between the pipeline and the demo dashboard in web/ (Severian0's SURU Command UI).

The dashboard was built against a normalised API (see web/api.js and the in-browser mock in
web/mock.js). This module serves exactly those shapes under /ui/*, translated from the
incident, response and resource databases, and keeps a live agent-event feed that the
dashboard polls. The makeshift engine console (/console, static/index.html) is untouched.

Shapes (ids are strings, because the dashboard compares them with DOM data attributes):
  incident  {id, category, severity, status, lat, lon, address, district, description, casualties,
             trapped, hazards[], weapons, received_at, lead_agency, routed_agencies[], report_count}
  response  {id, incident_id, created_at, status, routing{lead, agencies, rationale, executed_by},
             agent_responses[], allocations[], unmet[], hard_conflicts[], soft_conflicts[], directives[],
             cr_rationale, executed_by, evaluation}
  event     {id, ts, incident_id, agent_id (Qwen|A1|A2|A3|CR|rules), type, summary, payload, executed_by}
"""

from __future__ import annotations

import json
import math
import threading
from datetime import datetime, timezone
from typing import Callable

from fastapi import APIRouter, HTTPException, Request

from .schemas import AGENT_ID, Incident, IncidentLocation

AGENT_OF = {a.value: aid for a, aid in AGENT_ID.items()}
LIVE_EXECUTORS = {"nemotron", "qwen"}

CATEGORY_TEXT = {
    "rtc": "Road traffic collision", "structure_fire": "Structure fire", "high_rise_fire": "High-rise fire",
    "firearms": "Firearms", "assault": "Assault", "medical": "Medical emergency", "hazmat": "Hazmat", "flood": "Flood",
}
# What a caller would say for each '+ Incident' form category, so the category survives triage (Qwen or rules).
CALLER_PHRASE = {
    "rtc": "Road traffic collision", "structure_fire": "Building on fire", "high_rise_fire": "Fire in a tower block",
    "firearms": "Firearm seen", "assault": "Assault", "medical": "Medical emergency, person unwell",
    "hazmat": "Chemical spill", "flood": "Flooding",
}
ACTION_TEXT = {
    "accept_partial": "Dispatch what is free and record the gap",
    "mutual_aid": "Request the remainder from neighbouring services (mutual aid)",
    "relax_eta": "Accept units beyond the requested ETA",
}
# Agents give a staging distance but no bearing; spread agencies around the scene so the pins don't overlap.
STAGING_BEARING = {"fire": 0, "police": 120, "ambulance": 240, "shared": 60}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def type_name(resource_type: str) -> str:
    return resource_type.replace("_", " ").capitalize()


def station_id(station: str, agency: str) -> str:
    # Station names repeat across agencies ("Sheffield Central" is a fire and an ambulance station).
    return f"{station}|{agency}"


def _sid(v) -> str | None:
    return None if v is None else str(v)


# --------------------------------------------------------------------------- #
# Response normalisation
# --------------------------------------------------------------------------- #


def _offset(lat: float, lon: float, metres: float, bearing_deg: float) -> tuple[float, float]:
    b = math.radians(bearing_deg)
    dlat = metres * math.cos(b) / 111_320
    dlon = metres * math.sin(b) / (111_320 * math.cos(math.radians(lat)))
    return lat + dlat, lon + dlon


def norm_agent(ar: dict, lat: float | None = None, lon: float | None = None) -> dict:
    sc, md = ar.get("scene_constraints") or {}, ar.get("mission_details") or {}
    staging = None
    dist = sc.get("staging_distance_m")
    if dist and lat is not None and lon is not None:
        slat, slon = _offset(lat, lon, dist, STAGING_BEARING.get(ar.get("agency"), 0))
        staging = {"lat": slat, "lon": slon, "name": sc.get("staging_area") or f"Staging {dist} m from scene"}
    return {
        "agent_id": ar.get("agent_id"), "agency": ar.get("agency"), "executed_by": ar.get("executed_by"),
        "latency_ms": ar.get("latency_ms"), "confidence": ar.get("confidence"),
        "summary": md.get("summary", ""), "tactical_plan": md.get("tactical_plan", []),
        "hazards": md.get("hazards_identified", []), "safety_notes": md.get("safety_notes", []),
        "kb_citations": ar.get("kb_citations", []),
        "requests": [{"resource_type": q["resource_type"], "type_name": type_name(q["resource_type"]), "qty": q["quantity"],
                      "max_eta_min": q.get("max_eta_min"), "essential": q.get("priority", 1) == 1,
                      "justification": q.get("justification", "")} for q in ar.get("resource_requests", [])],
        "scene_constraints": {"inner_cordon_m": sc.get("cordon_inner_radius_m", 0), "outer_cordon_m": sc.get("cordon_outer_radius_m", 0),
                              "evacuation_radius_m": sc.get("evacuation_radius_m", 0), "staging": staging,
                              "scene_entry": sc.get("scene_entry"), "road_closures": sc.get("road_closures", [])},
    }


def resolution_text(res: dict) -> str:
    action = res.get("action", "")
    if action == "preempt":
        text = f"Pre-empt {', '.join(res.get('preempt_unit_ids') or []) or 'units'} from lower-severity incidents"
    elif action == "substitute":
        text = f"Substitute {type_name(res.get('substitute_type') or 'another type').lower()}"
    else:
        text = ACTION_TEXT.get(action, action.replace("_", " ").capitalize())
    if res.get("mutual_aid_for_remainder") and action != "mutual_aid":
        text += "; mutual aid for the remainder"
    return text


def norm_soft(s: dict) -> dict:
    return {"code": s.get("code"), "title": s.get("detail") or s.get("code"), "severity": s.get("severity"),
            "agencies": s.get("agencies", []), "resolution": s.get("suggested_resolution", ""), "source": s.get("source")}


def norm_hard(conflicts: list[dict], resolutions: list[dict], cr_executed_by: str) -> list[dict]:
    by_type = {r["resource_type"]: r for r in resolutions}
    out = []
    for c in conflicts:
        res = by_type.get(c["resource_type"])
        # "Overridden" = the CR model chose something that failed re-validation and rules stepped in.
        overridden = bool(res) and res.get("executed_by") == "fallback" and cr_executed_by in LIVE_EXECUTORS
        out.append({"kind": c["kind"], "resource_type": c["resource_type"], "type_name": type_name(c["resource_type"]),
                    "requested": c["requested"], "available": c["available"], "agencies": c.get("agencies", []),
                    "resolution": resolution_text(res) if res else c.get("detail", ""),
                    "rationale": (res or {}).get("rationale") or c.get("detail", ""),
                    "overridden": overridden, "fallback": res.get("action") if overridden else None,
                    "executed_by": (res or {}).get("executed_by")})
    return out


def norm_response(r: dict) -> dict:
    tri = r.get("triage") or {}
    lat, lon = tri.get("lat"), tri.get("lon")
    agents = r.get("agent_responses", [])
    max_eta, essential = {}, {}
    for ar in agents:
        for q in ar.get("resource_requests", []):
            key = (ar["agency"], q["resource_type"])
            max_eta[key] = q.get("max_eta_min")
            essential[key] = essential.get(key, False) or q.get("priority", 1) == 1
    for m in r.get("manifest", []):
        if m.get("priority", 1) == 1:
            essential[(m["for_agency"], m["resource_type"])] = True
    mutual_aid = {x["resource_type"] for x in r.get("hard_resolutions", [])
                  if x.get("action") == "mutual_aid" or x.get("mutual_aid_for_remainder")}
    routing = r.get("routing") or {}
    cr_by = r.get("cr_executed_by", "")
    return {
        "id": _sid(r.get("response_id")), "incident_id": _sid(r.get("incident_id")), "created_at": r.get("created_at"),
        "status": r.get("status", "dispatched"),
        "routing": {"lead": routing.get("lead_agency"), "agencies": routing.get("agencies", []),
                    "rationale": routing.get("rationale", ""), "executed_by": routing.get("executed_by")},
        "agent_responses": [norm_agent(ar, lat, lon) for ar in agents],
        "allocations": [{"unit_id": a["unit_id"], "callsign": a["callsign"], "resource_type": a["resource_type"],
                         "type_name": type_name(a["resource_type"]), "agency": a["agency"], "station": a["station"],
                         "station_id": station_id(a["station"], a["agency"]), "eta_min": round(a["eta_min"]),
                         "max_eta_min": max_eta.get((a["requested_by"], a["resource_type"])),
                         "requested_by": AGENT_OF.get(a["requested_by"], a["requested_by"]),
                         "preempted_from": _sid(a.get("preempted_from")), "task": a.get("task", ""),
                         "crew": len(a.get("crew", []))} for a in r.get("allocations", [])],
        "unmet": [{"resource_type": u["resource_type"], "type_name": type_name(u["resource_type"]), "qty": u.get("quantity", 0),
                   "essential": essential.get((u.get("requested_by"), u["resource_type"]), False),
                   "requested_by": AGENT_OF.get(u.get("requested_by"), u.get("requested_by")),
                   "mutual_aid": "Requested via regional mutual aid" if u["resource_type"] in mutual_aid else None}
                  for u in r.get("unmet", [])],
        "hard_conflicts": norm_hard(r.get("hard_conflicts", []), r.get("hard_resolutions", []), cr_by),
        "soft_conflicts": [norm_soft(s) for s in r.get("soft_conflicts", [])],
        "directives": [f"{d['agency'].capitalize()}: {d['action']}" for d in r.get("directives", [])],
        "manifest_adjustments": r.get("manifest_adjustments", []),
        "cr_rationale": r.get("cr_rationale", ""), "executed_by": cr_by,
        "evaluation": r.get("evaluation"),
    }


# --------------------------------------------------------------------------- #
# Incidents
# --------------------------------------------------------------------------- #


def incident_rows(db) -> list[dict]:
    """Every non-merged incident with its canonical record, triage and latest response."""
    rows = db._conn.execute(
        """
        SELECT i.incident_id, i.received_at, i.category, i.severity, i.lat, i.lon, i.place, i.status,
               i.report_count, i.incident_json, i.triage_json,
               (SELECT r.response_json FROM responses r WHERE r.incident_id = i.incident_id
                ORDER BY r.created_at DESC LIMIT 1) AS response_json
        FROM incidents i WHERE i.status != 'merged' ORDER BY i.received_at DESC
        """
    ).fetchall()
    return [dict(r) for r in rows]


def ui_status(db_status: str, response: dict | None, job_state: str | None) -> str:
    """Map the pipeline's lifecycle onto the dashboard's: routing → dispatched | partial | failed → closed.

    The pipeline's own 'backlog' (evaluated, units still out) is an ongoing incident on the dashboard.
    """
    if db_status == "closed":
        return "closed"
    if response:
        return response.get("status", "dispatched")
    if job_state == "error":
        return "failed"
    return "routing"


def norm_incident(row: dict, job_state: str | None = None) -> dict:
    inc = json.loads(row["incident_json"])
    tri = json.loads(row["triage_json"])
    resp = json.loads(row["response_json"]) if row.get("response_json") else None
    routing = (resp or {}).get("routing") or {}
    loc = inc.get("location") or {}
    return {
        "id": str(row["incident_id"]), "category": row["category"], "severity": row["severity"],
        "status": ui_status(row["status"], resp, job_state), "pipeline_status": row["status"],
        "lat": row["lat"], "lon": row["lon"],
        "address": loc.get("address") or loc.get("landmark") or row["place"], "district": tri.get("place") or "",
        "description": inc.get("description", ""), "casualties": tri.get("casualties", 0), "trapped": tri.get("trapped", 0),
        "hazards": tri.get("hazards", []), "weapons": "reported" if tri.get("weapons") else None,
        "received_at": row["received_at"], "lead_agency": routing.get("lead_agency"),
        "routed_agencies": routing.get("agencies", []), "report_count": row["report_count"],
        "channel": inc.get("channel"), "triage_rationale": tri.get("rationale", ""),
    }


def incident_from_form(body: dict) -> Incident:
    """The dashboard's '+ Incident' form and demo scenario send a flat body; the pipeline takes a canonical report.

    Category and severity are triage outputs in our pipeline, so they become part of what the caller said
    rather than being written into triage directly.
    """
    if "location" in body:
        return Incident.model_validate(body)
    desc = str(body.get("description") or "").strip()
    cat = CALLER_PHRASE.get(body.get("category", ""), "")
    if cat and cat.lower() not in desc.lower():
        desc = f"{cat}: {desc}" if desc else cat
    sev = body.get("severity")
    return Incident(
        channel="999",
        location=IncidentLocation(address=body.get("address") or None, landmark=body.get("district") or None,
                                  lat=body.get("lat"), lon=body.get("lon"),
                                  location_source="caller_stated" if body.get("lat") is not None else None),
        description=desc or "Incident reported",
        caller_reported_severity=f"severity {sev} of 5" if sev else None,
        casualties_reported=body.get("casualties") or None,
        people_trapped_reported=body.get("trapped") or None,
        hazards_reported=list(body.get("hazards") or []),
        weapons_mentioned=bool(body.get("weapons")) or body.get("category") == "firearms" or None,
    )


# --------------------------------------------------------------------------- #
# Live agent-event feed
# --------------------------------------------------------------------------- #


class EventFeed:
    """Translates orchestrator job events into the dashboard's agent feed. In memory; cleared on reset."""

    def __init__(self, limit: int = 2000):
        self._events: list[dict] = []
        self._seq = 0
        self._limit = limit
        self._lock = threading.Lock()

    def since(self, since_id: int) -> list[dict]:
        with self._lock:
            return [e for e in self._events if e["id"] > since_id]

    def clear(self) -> None:
        with self._lock:
            self._events.clear()  # keep _seq increasing so open dashboards don't re-read old ids

    def recent_fallback(self, n: int = 40) -> bool:
        with self._lock:
            return any(e["executed_by"] == "fallback" for e in self._events[-n:])

    def add(self, incident_id, agent_id: str, type_: str, summary: str, payload: dict | None = None,
            executed_by: str = "rules") -> None:
        with self._lock:
            self._seq += 1
            self._events.append({"id": self._seq, "ts": _now(), "incident_id": _sid(incident_id), "agent_id": agent_id,
                                 "type": type_, "summary": summary, "payload": payload or {}, "executed_by": executed_by or ""})
            del self._events[:-self._limit]

    # one orchestrator event -> zero or more feed events
    def on_job_event(self, incident_id, name: str, p: dict) -> None:
        if name == "ingested":
            t, d = p.get("triage", {}), p.get("dedup", {})
            cat = CATEGORY_TEXT.get(t.get("category"), type_name(t.get("category", "other")))
            self.add(incident_id, "Qwen", "TRIAGED", f"{cat}, severity {t.get('severity')} at {t.get('place') or 'scene'}",
                     {"rationale": t.get("rationale", ""), **t}, t.get("executed_by"))
            if d.get("verdict") == "related":
                self.add(incident_id, "rules", "RELATED", f"Separate incident near {d.get('match_incident_id')}: {d.get('reason', '')}",
                         d, d.get("executed_by"))
        elif name == "merged":
            self.add(incident_id, "rules", "MERGED", f"Duplicate report, folded into {p.get('into')}",
                     {"rationale": p.get("reason", "")}, "minilm")
        elif name == "routed":
            lead, agencies = p.get("lead_agency"), p.get("agencies", [])
            others = [a.capitalize() for a in agencies if a != lead]
            self.add(incident_id, "Qwen", "ROUTED",
                     f"→ {str(lead).capitalize()} (lead){', ' + ', '.join(others) if others else ''}: {p.get('rationale', '')}",
                     {"lead": lead, "agencies": agencies, "rationale": p.get("rationale", "")}, p.get("executed_by"))
        elif name == "agent_done":
            a = norm_agent(p)
            reqs = ", ".join(f"{q['qty']}× {q['type_name']}" for q in a["requests"]) or "nothing"
            sc = a["scene_constraints"]
            cordon = f"; inner cordon {sc['inner_cordon_m']} m" if sc["inner_cordon_m"] else ""
            self.add(incident_id, a["agent_id"], "PROPOSAL", f"{a['agency'].capitalize()} requests {reqs}{cordon}", a, a["executed_by"])
        elif name == "conflicts_detected":
            for c in p.get("hard", []):
                self.add(incident_id, "rules", "HARD_CONFLICT",
                         f"{c['kind'].replace('_', ' ').capitalize()}: {type_name(c['resource_type'])}, {c['requested']} requested, {c['available']} free",
                         {"rationale": c.get("detail", ""), **c}, "rules")
            for s in p.get("soft_rules", []):
                n = norm_soft(s)
                self.add(incident_id, "rules", "SOFT_CONFLICT", f"{n['title']} → {n['resolution'] or 'CR to resolve'}", n, "rules")
        elif name == "cr_decided":
            by = p.get("executed_by", "")
            for s in p.get("soft_semantic", []):
                n = norm_soft(s)
                self.add(incident_id, "CR", "SOFT_CONFLICT", f"{n['title']} → {n['resolution'] or 'resolve on scene'}", n, by)
            for res in p.get("resolutions", []):
                self.add(incident_id, "CR", "RESOLUTION", f"{type_name(res['resource_type'])}: {resolution_text(res)}", res,
                         res.get("executed_by", by))
                if res.get("executed_by") == "fallback" and by in LIVE_EXECUTORS:
                    self.add(incident_id, "rules", "OVERRIDE",
                             f"CR choice for {type_name(res['resource_type'])} failed re-validation → policy fallback {res['action']}",
                             {"fallback": res["action"]}, "fallback")
            sent = sum(m.get("quantity", 0) for m in p.get("manifest", []))
            asked = sum((p.get("requested_total") or {}).values())
            self.add(incident_id, "CR", "DECIDED", f"Manifest: {sent} units declared ({asked} requested by agencies)",
                     {"rationale": p.get("rationale", ""), "manifest": p.get("manifest", []),
                      "adjustments": p.get("manifest_adjustments", [])}, by)
        elif name == "committed":
            r = norm_response(p)
            short = sum(u["qty"] for u in r["unmet"])
            self.add(incident_id, "CR", "DISPATCHED",
                     f"{len(r['allocations'])} units allocated, status {r['status']}{f', {short} unmet' if short else ''}",
                     {"status": r["status"], "unmet": r["unmet"], "rationale": r["cr_rationale"]}, r["executed_by"])
        elif name == "evaluated":
            self.add(incident_id, "Qwen", "EVALUATED",
                     f"Grade {p.get('grade')} ({p.get('score')}/100): {p.get('headline') or p.get('verdict', '')}",
                     {"rationale": "; ".join(p.get("findings", [])), **p}, p.get("executed_by"))
        elif name == "world_outcome":
            proposal = p.get("proposal",{})
            text = (("Applied " + proposal.get("outcome", "") + ": " + proposal.get("reason", ""))
                    if p.get("accepted") else "Rejected world outcome: " + p.get("error", ""))
            self.add(incident_id,"Qwen","WORLD_OUTCOME",text,p,p.get("executed_by"))
        elif name == "error":
            self.add(incident_id, "rules", "ERROR", p.get("message", "pipeline error"), p, "rules")

    def units_returned(self, released: list[dict]) -> None:
        by_inc: dict = {}
        for u in released:
            by_inc.setdefault(u["incident_id"], []).append(u["callsign"])
        for iid, calls in by_inc.items():
            self.add(iid, "rules", "RETURNED", f"{', '.join(calls)} back in service", {"units": calls}, "rules")


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #


def make_router(state: dict, start_job: Callable, reset: Callable) -> APIRouter:
    """state: the api module's shared state (db, settings, jobs, feed). start_job(incident, mode) runs the pipeline."""
    r = APIRouter(prefix="/ui", tags=["dashboard"])
    db = lambda: state["db"]  # noqa: E731  (state is filled in by the app lifespan)

    def job_states() -> dict[int, str]:
        out = {}
        for j in state["jobs"].values():
            if j.get("incident_id"):
                out[j["incident_id"]] = "error" if j.get("outcome") == "error" else ("done" if j["done"] else "running")
        return out

    def world_summary():
        world = state.get("world")
        if world is None:
            return {"enabled":False}
        return {"enabled":True, "tick":world.state["tick"], "inflight":world.inflight,
                "outcome_mode":world.outcome_mode, "world_version":world.world_version,
                "assessments":world.outcome_events[-20:],
                "active":sum(i["status"]!="RESOLVED" for i in world.state["active_incidents"].values()),
                "outcomes":{str(case_id):{"status":world.state["active_incidents"][node]["status"],
                    "severity":world.state["active_incidents"][node]["severity"],
                    **world.evaluate_response(node)} for case_id,node in world.case_nodes.items()}}

    @r.get("/world")
    async def read_world():
        return world_summary()

    @r.post("/world/dispatch")
    async def dispatch_world():
        from .world import WorldConflict
        world = state.get("world")
        if world is None or world.inflight:
            raise WorldConflict("Authored world is disabled or waiting for a model decision")
        async def event(case_id,name,payload):
            if state.get("world_event"):
                await state["world_event"](case_id,name,payload)
            else:
                state["feed"].on_job_event(case_id,name,payload)
        await world.dispatch_due(state["orch"],event)
        return world_summary()

    @r.post("/world/advance")
    async def advance_world():
        from .world import WorldConflict
        world = state.get("world")
        if world is None:
            raise WorldConflict("Authored world is disabled")
        await world.advance(state["orch"],state.get("world_event"))
        state["feed"].units_returned(world.last_released)
        return world_summary()

    @r.post("/world/reassess")
    async def reassess_world():
        from .world import WorldConflict
        world = state.get("world")
        if world is None or world.outcome_mode != "model":
            raise WorldConflict("Enable model world outcomes to reassess")
        await world.reassess(state["orch"],state.get("world_event"))
        return world_summary()

    @r.get("/health")
    async def health():
        s = state["settings"]
        if not s.live:
            model = None
        elif s.specialist.provider == "claude-code":
            model = f"Claude Code stand-in ({s.specialist.model})"
        else:
            model = f"Qwen {s.orchestrator.model} · Nemotron {s.specialist.model}"
        return {"mode": "live" if s.live else "mock", "model": model, "fallback": state["feed"].recent_fallback(),
                "jobs_running": sum(1 for j in state["jobs"].values() if not j["done"])}

    @r.get("/incidents")
    async def incidents():
        js = job_states()
        return [norm_incident(row, js.get(row["incident_id"])) for row in incident_rows(db())]

    @r.post("/incidents")
    async def create(request: Request):
        try:
            incident = incident_from_form(await request.json())
        except Exception as e:
            raise HTTPException(422, f"Invalid incident: {e}") from e
        mode = "live" if state["settings"].live else "mock"
        return await start_job(incident, mode)

    @r.post("/incidents/{incident_id}/close")
    async def close(incident_id: int):
        if not db().get_incident(incident_id):
            raise HTTPException(404, "Unknown incident")
        n = db().close_incident(incident_id)
        state["feed"].add(incident_id, "rules", "CLOSED", f"Closed by operator; {n} units released", {}, "rules")
        return {"id": str(incident_id), "status": "closed", "units_released": n}

    @r.get("/responses")
    async def responses():
        rows = db()._conn.execute("SELECT response_json FROM responses ORDER BY created_at").fetchall()
        return [norm_response(json.loads(x["response_json"])) for x in rows]

    @r.get("/incidents/{incident_id}/responses")
    async def incident_responses(incident_id: int):
        rows = db()._conn.execute("SELECT response_json FROM responses WHERE incident_id = ? ORDER BY created_at",
                                  (incident_id,)).fetchall()
        return [norm_response(json.loads(x["response_json"])) for x in rows]

    @r.get("/events")
    def events(since_id: int = 0):
        return state["feed"].since(since_id)

    @r.get("/stations")
    async def stations():
        return [{"id": station_id(s["station"], s["agency"]), "name": s["station"], "agency": s["agency"],
                 "lat": s["lat"], "lon": s["lon"], "total": s["total"], "available": s["available"]} for s in db().stations()]

    @r.get("/hospitals")
    async def hospitals():
        return [{"name": h["name"], "ed_capacity": h["ed_capacity"], "major_trauma": bool(h["major_trauma"]),
                 "paediatric": bool(h["paediatric"]), "lat": h["lat"], "lon": h["lon"]}
                for h in db().hospitals(53.3811, -1.4701)]

    @r.get("/units")
    async def units(resource_type: str | None = None, agency: str | None = None):
        q = """
            SELECT u.unit_id, u.callsign, u.resource_type, u.agency, u.station, u.lat, u.lon, u.status, a.incident_id
            FROM units u LEFT JOIN allocations a ON a.unit_id = u.unit_id AND a.released_at IS NULL
        """
        conds, args = [], []
        if resource_type:
            conds.append("u.resource_type = ?")
            args.append(resource_type)
        if agency:
            conds.append("u.agency = ?")
            args.append(agency)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        rows = db()._conn.execute(q + " ORDER BY u.callsign", args).fetchall()
        world = getattr(db(), "graph_world", None)
        if world is not None:
            out = []
            for row in rows:
                u = dict(row)
                moving = world.state["resource_db"].get(u["unit_id"], {})
                out.append({"id":u["unit_id"], "callsign":u["callsign"], "resource_type":u["resource_type"],
                            "agency":u["agency"], "station_id":station_id(u["station"],u["agency"]),
                            "lat":moving.get("lat",u["lat"]), "lon":moving.get("lon",u["lon"]),
                            "status":"committed" if u["incident_id"] else "available" if u["status"]=="available" else "out_of_service",
                            "incident_id":_sid(u["incident_id"]), "world_stage":moving.get("status"),
                            "back_in_service_min":moving.get("total_depletion_ticks_remaining")})
            return out
        return [{"id": u["unit_id"], "callsign": u["callsign"], "resource_type": u["resource_type"], "agency": u["agency"],
                 "station_id": station_id(u["station"], u["agency"]), "lat": u["lat"], "lon": u["lon"],
                 "status": "out_of_service" if u["status"] != "available" else ("committed" if u["incident_id"] else "available"),
                 "incident_id": _sid(u["incident_id"])} for u in rows]

    @r.post("/units/{unit_id}/release")
    async def release(unit_id: str):
        unit = db().unit(unit_id)
        if not unit:
            raise HTTPException(404, "Unknown unit")
        held = db()._conn.execute("SELECT incident_id FROM allocations WHERE unit_id = ? AND released_at IS NULL",
                                  (unit_id,)).fetchone()
        ok = db().release_unit(unit_id)
        if ok:
            state["feed"].add(held["incident_id"] if held else None, "rules", "RELEASED",
                              f"{unit['callsign']} released by operator", {}, "rules")
        return {"id": unit_id, "released": ok}

    @r.get("/resources")
    async def resources():
        return [{"code": t["resource_type"], "name": type_name(t["resource_type"]), "agency": t["agency"], "total": t["total"],
                 "available": t["available"], "committed": t["committed"], "out_of_service": t["total"] - t["in_service"]}
                for t in db().inventory()]

    @r.get("/resources/{resource_type}/forecast")
    async def forecast(resource_type: str):
        if resource_type not in db().resource_types():
            raise HTTPException(404, "Unknown resource type")
        total = sum(t["total"] for t in db().inventory() if t["resource_type"] == resource_type)
        pts = db().availability_forecast(resource_type, 120, 15)
        return {"resource_type": resource_type, "total": total,
                "points": [{"minutes": p["t_plus_min"], "available": p["available"]} for p in pts]}

    @r.get("/scenario")
    def scenario():
        from .config import ROOT
        return json.loads((ROOT / "scenarios" / "sheffield_demo.json").read_text())

    @r.post("/reset")
    async def do_reset():
        out = await reset()
        state["feed"].clear()
        return out

    return r

