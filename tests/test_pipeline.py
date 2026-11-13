import asyncio
import json
import sqlite3
from dataclasses import replace
from datetime import timedelta

import pytest

from urban_response.agents import conflict_resolver as cr_mod
from urban_response.agents import orchestrator as orch_mod
from urban_response.agents import specialist as spec_mod
from urban_response.agents.conflict_resolver import CRDecision
from urban_response.agents.orchestrator import DedupDraft, EvaluationDraft, Orchestrator, RoutingDraft
from urban_response.briefing import build_brief
from urban_response.config import ROOT
from urban_response.llm import LLMError, extract_json
from urban_response.resources import crew_for
from urban_response.schemas import Agency, ExecutedBy, Incident, Location

from .conftest import make_incident


def burst():
    return [Incident(**x) for x in json.loads((ROOT / "scenarios" / "burst_park_hill.json").read_text())]


async def test_mock_demo_full_loop(db, settings):
    orch = Orchestrator(db, settings)
    raw = json.loads((ROOT / "scenarios" / "sheffield_demo.json").read_text())
    results = [await orch.handle(Incident(**x)) for x in raw]
    plans = [r.response for r in results]

    # no unit and no person is committed twice
    active = db._conn.execute("SELECT unit_id FROM allocations WHERE released_at IS NULL").fetchall()
    assert len(active) == len({r["unit_id"] for r in active})
    people = db._conn.execute("SELECT person_id FROM crew_assignments WHERE released_at IS NULL").fetchall()
    assert len(people) == len({r["person_id"] for r in people})

    assert any(p.hard_conflicts for p in plans)
    assert any(a.preempted_from == plans[0].incident_id for p in plans for a in p.allocations)
    assert any(c.code == "ENTRY_RESTRICTED_BUT_REQUIRED" for c in plans[1].soft_conflicts)
    # every response was evaluated and every incident finalised to the backlog
    assert all(p.evaluation and p.evaluation.metrics for p in plans)
    assert all(db.get_incident(r.incident_id)["status"] == "backlog" for r in results)


async def test_every_dispatched_vehicle_is_fully_crewed(db, settings):
    r = (await Orchestrator(db, settings).handle(make_incident(category="high_rise_fire", severity=5, casualties_reported=4, people_trapped=2))).response
    assert r.allocations
    for a in r.allocations:
        need = crew_for(a.resource_type)
        got = {}
        for m in a.crew:
            got[m.speciality] = got.get(m.speciality, 0) + 1
        assert got == need, (a.callsign, got, need)


async def test_crew_shortfall_blocks_vehicles(db, settings):
    # Take every firearms officer off shift: ARVs exist but cannot roll.
    db._conn.execute("UPDATE personnel SET on_shift = 0 WHERE speciality = 'firearms_officer'")
    db._conn.commit()
    r = (await Orchestrator(db, settings).handle(make_incident(category="firearms", severity=4, weapons_involved=True, casualties_reported=1))).response
    assert any(c.kind == "crew_shortfall" and c.resource_type == "firearms_officer" for c in r.hard_conflicts)
    assert not any(a.resource_type == "armed_response_vehicle" for a in r.allocations)
    assert any(u["resource_type"] == "armed_response_vehicle" for u in r.unmet)


async def test_duplicate_reports_are_merged_not_dispatched(db, settings):
    orch = Orchestrator(db, settings)
    a, b, c, d = burst()
    ra = await orch.handle(a)
    rb = await orch.handle(b)
    rc = await orch.handle(c)
    rd = await orch.handle(d)
    assert ra.outcome == "dispatched"
    assert rb.outcome == "merged" and rb.dedup.match_incident_id == a.incident_id
    assert rc.outcome == "merged"  # high_rise_fire vs structure_fire, 100 m away: same incident
    assert rd.outcome == "dispatched" and rd.dedup.verdict.value == "related"  # crash nearby: different incident
    assert db.get_incident(a.incident_id)["report_count"] == 3
    assert db.get_incident(b.incident_id)["status"] == "merged"
    assert not db._conn.execute("SELECT 1 FROM responses WHERE incident_id = ?", (b.incident_id,)).fetchone()
    assert rd.response.related_incidents == [a.incident_id]


async def test_simultaneous_duplicates_dispatch_once(db, settings):
    """Two callers ring at the same instant: ingestion is atomic, so only one dispatch happens."""
    orch = Orchestrator(db, settings)
    a, b, _, _ = burst()
    results = await asyncio.gather(orch.handle(a), orch.handle(b))
    assert sorted(r.outcome for r in results) == ["dispatched", "merged"]
    assert db._conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0] == 1


async def test_life_safety_floor_overrides_cr_trim(db, settings, monkeypatch):
    """CR tries to send 1 ambulance to 4 casualties; code raises it to the floor."""

    async def fake_llm(ep, system, user, out, **k):
        if out is RoutingDraft:
            return RoutingDraft(agencies=[Agency.ambulance], lead_agency=Agency.ambulance, rationale="r"), 1
        if out is EvaluationDraft:
            return EvaluationDraft(score=80, verdict="resolved", findings=["ok"], headline="Crash", dek="d"), 1
        if out is CRDecision:
            return CRDecision.model_validate({
                "manifest": [{"resource_type": "emergency_ambulance", "quantity": 1, "for_agency": "ambulance", "reason": "trim"}],
                "directives": [{"agency": "ambulance", "action": "triage on arrival", "priority": "immediate"}],
            }), 1
        return out.model_validate({"resource_requests": [{"resource_type": "emergency_ambulance", "quantity": 4}],
                                   "scene_constraints": {}, "mission_details": {"summary": "s"}}), 1

    for mod in (orch_mod, spec_mod, cr_mod):
        monkeypatch.setattr(mod, "chat_json", fake_llm)
    r = (await Orchestrator(db, replace(settings, llm_mode="live")).handle(make_incident(category="rtc", severity=3, casualties_reported=4))).response
    amb = sum(m.quantity for m in r.manifest if m.resource_type == "emergency_ambulance")
    assert amb == 4 and any("life-safety floor" in n for n in r.manifest_adjustments)
    assert r.directives[0].action == "Triage on arrival."  # normalised
    assert r.evaluation.executed_by == ExecutedBy.qwen


async def test_cr_trims_overlapping_requests(db, settings, monkeypatch):
    """Three agencies each ask for a drone; CR sends one."""

    async def fake_llm(ep, system, user, out, **k):
        if out is RoutingDraft:
            return RoutingDraft(agencies=[Agency.police, Agency.fire], lead_agency=Agency.fire, rationale="r"), 1
        if out is EvaluationDraft:
            return EvaluationDraft(score=90, verdict="resolved", headline="h", dek="d"), 1
        if out is CRDecision:
            assert "LIFE-SAFETY FLOORS" in user and "PERSONNEL FREE" in user
            return CRDecision.model_validate({
                "manifest": [{"resource_type": "drone", "quantity": 1, "for_agency": "fire", "reason": "one shared"},
                             {"resource_type": "fire_engine", "quantity": 2, "for_agency": "fire", "reason": "PDA"},
                             {"resource_type": "patrol_car", "quantity": 2, "for_agency": "police", "reason": "cordon"}],
                "directives": [{"agency": "all", "action": "Single RVP at the station", "priority": "immediate"}],
            }), 1
        return out.model_validate({"resource_requests": [{"resource_type": "drone", "quantity": 1, "priority": 3},
                                                         {"resource_type": "fire_engine" if "A3" in system else "patrol_car", "quantity": 2}],
                                   "scene_constraints": {}, "mission_details": {"summary": "s"}}), 1

    for mod in (orch_mod, spec_mod, cr_mod):
        monkeypatch.setattr(mod, "chat_json", fake_llm)
    r = (await Orchestrator(db, replace(settings, llm_mode="live")).handle(make_incident(category="structure_fire", severity=3))).response
    assert r.requested_total["drone"] == 2
    assert sum(1 for a in r.allocations if a.resource_type == "drone") == 1
    assert r.evaluation.metrics["trimmed_units"] == 1


async def test_ledger_and_autonomous_return(db, settings, monkeypatch):
    orch = Orchestrator(db, settings)
    res = await orch.handle(make_incident(category="rtc", severity=2, casualties_reported=1))
    n_units = len(res.response.allocations)
    n_people = sum(len(a.crew) for a in res.response.allocations)
    led = db.ledger(500, res.incident_id)
    assert sum(1 for x in led if x["event"] == "dispatched" and x["item_kind"] == "unit") == n_units
    assert sum(1 for x in led if x["event"] == "dispatched" and x["item_kind"] == "person") == n_people

    # Fast-forward: everything is due back.
    db._conn.execute("UPDATE allocations SET expected_release_at = '2000-01-01T00:00:00+00:00'")
    db._conn.commit()
    released = db.release_due()
    assert len(released) == n_units
    assert db.get_incident(res.incident_id)["status"] == "closed"
    assert all(p["committed"] == 0 for p in db.personnel_inventory())
    assert sum(1 for x in db.ledger(500, res.incident_id) if x["event"] == "returned") == n_units + n_people


async def test_phone_brief_is_normalised(db, settings):
    orch = Orchestrator(db, settings)
    a, b, *_ = burst()
    await orch.handle(a)
    await orch.handle(b)
    brief = build_brief(db, b.incident_id)  # opening a duplicate shows the parent's brief
    assert brief["incident_id"] == a.incident_id and brief["opened_from_duplicate"] == b.incident_id
    assert brief["headline"] and brief["dateline"].startswith("SHEFFIELD")
    assert brief["facts"]["Reports"] == 2 and len(brief["merged_reports"]) == 1
    assert brief["units"] and all(u["crew"] for u in brief["units"])
    kinds = [l["kind"] for l in brief["log"]]
    assert kinds[0] == "received" and "completed" in kinds and kinds[-1] == "merged"  # second caller rang after dispatch
    assert all(d["action"][0].isupper() for ds in brief["orders"].values() for d in ds)


async def test_live_mode_falls_back_when_models_fail(db, settings, monkeypatch):
    async def boom(*a, **k):
        raise LLMError("server down")

    for mod in (orch_mod, spec_mod, cr_mod):
        monkeypatch.setattr(mod, "chat_json", boom)
    r = (await Orchestrator(db, replace(settings, llm_mode="live")).handle(make_incident(severity=4, casualties_reported=2, people_trapped=1))).response
    assert r.routing.executed_by == ExecutedBy.fallback
    assert all(x.executed_by == ExecutedBy.fallback for x in r.agent_responses)
    assert r.cr_executed_by == ExecutedBy.fallback and r.evaluation.executed_by == ExecutedBy.fallback
    assert r.allocations


async def test_ambiguous_dedup_goes_to_qwen(db, settings, monkeypatch):
    calls = []

    async def fake_llm(ep, system, user, out, **k):
        calls.append(out)
        if out is DedupDraft:
            return DedupDraft(verdict="related", reason="different building"), 1
        raise LLMError("only dedup is faked")

    for mod in (orch_mod, spec_mod, cr_mod):
        monkeypatch.setattr(mod, "chat_json", fake_llm)
    orch = Orchestrator(db, replace(settings, llm_mode="live"))
    base = make_incident(category="structure_fire", severity=3)
    await orch.handle(base)
    # same family (vehicle fire vs building fire), 250 m away: ambiguous band
    near = make_incident(category="vehicle_fire", severity=3,
                         location=Location(lat=base.location.lat + 0.00225, lon=base.location.lon, address="Car park"))
    res = await orch.handle(near)
    assert DedupDraft in calls and res.dedup.executed_by == ExecutedBy.qwen and res.outcome == "dispatched"


async def test_foreign_key_and_close(db, settings):
    res = await Orchestrator(db, settings).handle(make_incident(category="rtc", severity=3, casualties_reported=1))
    with pytest.raises(sqlite3.IntegrityError):
        db.record_response(make_incident(incident_id="NOPE"),
                           res.response.model_copy(update={"response_id": "RSP-X", "incident_id": "NOPE"}))
    assert db.close_incident(res.incident_id) == len(res.response.allocations)
    assert all(r["committed"] == 0 for r in db.inventory())


async def test_claude_code_provider_dispatch_and_trace(tmp_path, monkeypatch):
    from urban_response import llm, llm_claude_code
    from urban_response.config import ModelEndpoint

    async def fake_complete(ep, system, messages):
        return '```json\n{"agencies": ["police"], "lead_agency": "police", "rationale": "r"}\n```', {"models": ["m"]}

    monkeypatch.setattr(llm_claude_code, "complete", fake_complete)
    trace = tmp_path / "t.jsonl"
    monkeypatch.setenv("UR_TRACE_FILE", str(trace))
    ep = ModelEndpoint(base_url="claude -p", model="default", provider="claude-code")
    out, _ = await llm.chat_json(ep, "sys", "route this", RoutingDraft, label="Qwen route")
    assert out.lead_agency == Agency.police
    assert json.loads(trace.read_text().splitlines()[0])["models"] == ["m"]


def test_extract_json_strips_reasoning():
    assert extract_json('<think>hmm {"no": 1}</think>\n```json\n{"a": 1}\n```') == {"a": 1}
