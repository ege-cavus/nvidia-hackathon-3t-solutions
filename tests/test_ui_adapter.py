"""The demo dashboard's /ui adapter: shapes match web/api.js and the feed tells the agent story."""

from collections import Counter

from urban_response.agents.orchestrator import Orchestrator
from urban_response.ui_adapter import EventFeed, incident_from_form, incident_rows, norm_incident, norm_response
from urban_response import triage

from .test_pipeline import load


async def test_dashboard_shapes_and_feed(db, settings):
    orch, feed = Orchestrator(db, settings), EventFeed()
    for report in load("sheffield_demo.json"):
        ids = {}

        async def on_event(name, payload):
            if name == "ingested":
                ids["id"] = payload["incident"]["incident_id"]
            feed.on_job_event(ids.get("id"), name, payload)

        await orch.handle(report, on_event=on_event)

    incidents = [norm_incident(r) for r in incident_rows(db)]
    assert len(incidents) == 5
    assert all(isinstance(i["id"], str) and i["status"] in {"dispatched", "partial", "failed"} for i in incidents)
    assert all(i["lead_agency"] in i["routed_agencies"] for i in incidents)

    stations = {f"{s['station']}|{s['agency']}" for s in db.stations()}
    rows = db._conn.execute("SELECT response_json FROM responses").fetchall()
    import json
    responses = [norm_response(json.loads(r["response_json"])) for r in rows]
    for r in responses:
        assert r["incident_id"] in {i["id"] for i in incidents}
        assert all(a["station_id"] in stations for a in r["allocations"])
        assert all(a["requested_by"] in {"A1", "A2", "A3"} for a in r["allocations"])
        assert all(isinstance(d, str) for d in r["directives"])
        assert all(q["qty"] >= 1 for ar in r["agent_responses"] for q in ar["requests"])
    assert any(r["hard_conflicts"] for r in responses)

    kinds = Counter((e["agent_id"], e["type"]) for e in feed.since(0))
    for k in [("Qwen", "TRIAGED"), ("Qwen", "ROUTED"), ("CR", "DISPATCHED"), ("Qwen", "EVALUATED")]:
        assert kinds[k] == 5
    assert kinds[("A1", "PROPOSAL")] and kinds[("A2", "PROPOSAL")] and kinds[("A3", "PROPOSAL")]
    ids = [e["id"] for e in feed.since(0)]
    assert ids == sorted(ids) and feed.since(ids[-1]) == []


def test_form_category_survives_rule_triage():
    for cat in ["rtc", "structure_fire", "high_rise_fire", "firearms", "hazmat", "flood"]:
        inc = incident_from_form({"category": cat, "severity": 3, "address": "Division St", "lat": 53.38, "lon": -1.47,
                                  "description": "Caller is distressed", "hazards": []})
        assert triage.classify(inc) == cat, cat
