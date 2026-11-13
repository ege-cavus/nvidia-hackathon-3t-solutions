import asyncio
import json
import os
import re
import sqlite3
from dataclasses import replace
from datetime import datetime, timedelta, timezone

import jsonschema
import pytest

from urban_response import dedup
from urban_response.agents import conflict_resolver as cr_mod
from urban_response.agents import orchestrator as orch_mod
from urban_response.agents import specialist as spec_mod
from urban_response.agents.conflict_resolver import CRDecision
from urban_response.agents.orchestrator import DedupDraft, EvaluationDraft, Orchestrator, RoutingDraft, TriageDraft
from urban_response.briefing import build_brief
from urban_response.config import ROOT
from urban_response.llm import LLMError, extract_json
from urban_response.resources import crew_for
from urban_response.schemas import Agency, ExecutedBy, Incident, IncidentLocation

from .conftest import make_report

SCHEMA = json.loads((ROOT / "examples" / "incident.schema.json").read_text())
ID_RE = re.compile(r"^20\d{6}\d{6}$")


def load(name):
    raw = json.loads((ROOT / "scenarios" / name).read_text())
    return [Incident(**x) for x in (raw if isinstance(raw, list) else [raw])]


def burst():
    xs = load("burst_park_hill.json")
    now = datetime.now(timezone.utc)
    for i, x in enumerate(xs):
        x.received_at = now + timedelta(seconds=2 * i)
    return xs


# ---- canonical incident --------------------------------------------------------- #


def test_scenarios_and_example_are_canonical():
    reports = load("sheffield_demo.json") + load("simple_rtc.json") + load("burst_park_hill.json")
    reports.append(Incident(**json.loads((ROOT / "examples" / "incident.example.json").read_text())))
    for r in reports:
        jsonschema.validate(r.canonical_json() | {"incident_id": r.incident_id or 20261113000001}, SCHEMA)


def test_location_needs_somewhere():
    with pytest.raises(ValueError):
        IncidentLocation(location_notes="somewhere")


async def test_stored_incident_and_response_are_canonical_and_numeric(db, settings):
    res = await Orchestrator(db, settings).handle(load("simple_rtc.json")[0])
    assert isinstance(res.incident_id, int) and ID_RE.match(str(res.incident_id))
    assert isinstance(res.response.response_id, int) and ID_RE.match(str(res.response.response_id))
    row = db.get_incident(res.incident_id)
    jsonschema.validate(row["incident"], SCHEMA)  # stored exactly in the canonical shape
    assert "category" not in row["incident"] and row["triage"]["category"] == "rtc"
    stored = db.get_response(res.response.response_id)
    jsonschema.validate(stored["incident"], SCHEMA)
    assert stored["incident_id"] == res.incident_id and stored["triage"]["severity"] >= 1


async def test_ids_are_sequential_per_day(db, settings):
    orch = Orchestrator(db, settings)
    a = await orch.handle(make_report("rtc", lat=53.33, lon=-1.48))
    b = await orch.handle(make_report("medical", lat=53.42, lon=-1.40))
    assert b.incident_id == a.incident_id + 1


# ---- triage ------------------------------------------------------------------------ #


async def test_landmark_only_report_is_located_by_gazetteer(db, settings):
    rep = Incident(channel="text", location=IncidentLocation(landmark="Park Hill flats", location_source="caller_stated"),
                   description="Fire in our block, smoke in the stairwell.")
    res = await Orchestrator(db, settings).handle(rep)
    t = res.response.triage
    assert t.location_resolved_by == "gazetteer" and abs(t.lat - 53.38) < 0.01
    assert t.category == "structure_fire"


async def test_unknown_location_is_flagged(db, settings):
    rep = Incident(location=IncidentLocation(address="Somewhere nobody knows"), description="Man collapsed and not breathing.")
    res = await Orchestrator(db, settings).handle(rep)
    assert res.response.triage.location_resolved_by == "default"
    assert any(l["kind"] == "flag" for l in db.incident_log(res.incident_id))


# ---- full loop ----------------------------------------------------------------------- #


async def test_mock_demo_full_loop(db, settings):
    orch = Orchestrator(db, settings)
    results = [await orch.handle(r) for r in load("sheffield_demo.json")]
    plans = [r.response for r in results]
    assert all(r.outcome != "merged" for r in results)  # five different incidents: nothing merged
    active = db._conn.execute("SELECT unit_id FROM allocations WHERE released_at IS NULL").fetchall()
    assert len(active) == len({r["unit_id"] for r in active})
    people = db._conn.execute("SELECT person_id FROM crew_assignments WHERE released_at IS NULL").fetchall()
    assert len(people) == len({r["person_id"] for r in people})
    assert any(p.hard_conflicts for p in plans)
    assert plans[1].triage.category == "firearms" and plans[1].triage.weapons
    assert any(c.code == "ENTRY_RESTRICTED_BUT_REQUIRED" for c in plans[1].soft_conflicts)
    assert all(p.evaluation and p.evaluation.metrics for p in plans)
    assert all(db.get_incident(r.incident_id)["status"] == "backlog" for r in results)
    # Five major incidents exhaust the city: the last one goes to mutual aid.
    assert plans[-1].unmet and all("mutual aid" in u["reason"] or "crew" in u["reason"] for u in plans[-1].unmet)


async def test_every_dispatched_vehicle_is_fully_crewed(db, settings):
    r = (await Orchestrator(db, settings).handle(make_report("high_rise_fire", casualties_reported=4, people_trapped_reported=2))).response
    assert r.allocations
    for a in r.allocations:
        got = {}
        for m in a.crew:
            got[m.speciality] = got.get(m.speciality, 0) + 1
        assert got == crew_for(a.resource_type), a.callsign


async def test_crew_shortfall_blocks_vehicles(db, settings):
    db._conn.execute("UPDATE personnel SET on_shift = 0 WHERE speciality = 'firearms_officer'")
    db._conn.commit()
    r = (await Orchestrator(db, settings).handle(make_report("firearms", weapons_mentioned=True, casualties_reported=1))).response
    assert any(c.kind == "crew_shortfall" and c.resource_type == "firearms_officer" for c in r.hard_conflicts)
    assert not any(a.resource_type == "armed_response_vehicle" for a in r.allocations)


# ---- de-duplication (MiniLM pipeline; offline embedder in tests) ---------------------- #


async def test_burst_dedup(db, settings):
    orch = Orchestrator(db, settings)
    res = [await orch.handle(x) for x in burst()]
    a = res[0]
    # The offline stand-in is a bag of words, weaker than MiniLM on paraphrase (caller D);
    # test_burst_dedup_real_minilm checks all five with the real model.
    assert [res[i].outcome for i in (0, 1, 2, 4)] == ["dispatched", "merged", "merged", "dispatched"]
    assert all(r.dedup.executed_by == ExecutedBy.minilm for r in res)
    assert all(r.dedup.match_incident_id == a.incident_id for r in res[1:3])
    assert db.get_incident(a.incident_id)["report_count"] >= 3
    crash = res[4]
    assert crash.dedup.verdict.value == "related" and crash.response.related_incidents == [a.incident_id]
    assert not db._conn.execute("SELECT 1 FROM responses WHERE incident_id = ?", (res[1].incident_id,)).fetchone()


async def test_category_guard_only_prevents_merges(db, settings, monkeypatch):
    monkeypatch.setattr(dedup, "CATEGORY_GUARD", False)
    orch = Orchestrator(db, settings)
    xs = burst()
    first = await orch.handle(xs[0])
    crash = await orch.handle(xs[4])
    # without the guard the clustering result stands, whatever it is
    assert crash.dedup.verdict.value in ("duplicate", "related", "new")
    assert crash.dedup.executed_by == ExecutedBy.minilm and first.outcome == "dispatched"


async def test_simultaneous_duplicates_dispatch_once(db, settings):
    orch = Orchestrator(db, settings)
    a, b = burst()[:2]
    results = await asyncio.gather(orch.handle(a), orch.handle(b))
    assert sorted(r.outcome for r in results) == ["dispatched", "merged"]
    assert db._conn.execute("SELECT COUNT(*) FROM responses").fetchone()[0] == 1


@pytest.mark.skipif(os.getenv("RUN_MINILM") != "1", reason="set RUN_MINILM=1 to run with the real MiniLM model")
async def test_burst_dedup_real_minilm(db, settings):
    dedup.set_embedder(None)
    orch = Orchestrator(db, settings)
    res = [await orch.handle(x) for x in burst()]
    assert [r.outcome for r in res] == ["dispatched", "merged", "merged", "merged", "dispatched"]
    assert db.get_incident(res[0].incident_id)["report_count"] == 4
    assert res[4].dedup.verdict.value == "related"  # the crash: kept separate (category guard)


# ---- LLM roles (faked) ---------------------------------------------------------------- #


def fake_llm_factory(cr_payload, triage=None, agencies=(Agency.ambulance,), lead=Agency.ambulance, agent_requests=None,
                     dedup_answer=None, seen=None):
    async def fake_llm(ep, system, user, out, **k):
        if out is DedupDraft:
            if seen is not None:
                seen.append(user)
            if isinstance(dedup_answer, Exception):
                raise dedup_answer
            return DedupDraft.model_validate(dedup_answer(user) if callable(dedup_answer) else dedup_answer), 1
        if out is TriageDraft:
            return TriageDraft.model_validate(triage), 1
        if out is RoutingDraft:
            return RoutingDraft(agencies=list(agencies), lead_agency=lead, rationale="r"), 1
        if out is EvaluationDraft:
            return EvaluationDraft(score=88, verdict="resolved", findings=["ok"], headline="Crash on Test Street", dek="d"), 1
        if out is CRDecision:
            assert "INCIDENT (canonical) AND TRIAGE" in user
            return CRDecision.model_validate(cr_payload), 1
        assert '"incident"' in user and '"triage"' in user  # agents see canonical incident + triage
        return out.model_validate({"resource_requests": agent_requests(system), "scene_constraints": {}, "mission_details": {"summary": "s"}}), 1
    return fake_llm


async def test_life_safety_floor_overrides_cr_trim(db, settings, monkeypatch):
    fake = fake_llm_factory(
        {"manifest": [{"resource_type": "emergency_ambulance", "quantity": 1, "for_agency": "ambulance", "reason": "trim"}],
         "directives": [{"agency": "ambulance", "action": "triage on arrival", "priority": "immediate"}]},
        triage={"category": "rtc", "severity": 3, "casualties": 4, "trapped": 0, "weapons": False, "rationale": "r"},
        agent_requests=lambda s: [{"resource_type": "emergency_ambulance", "quantity": 4}])
    for mod in (orch_mod, spec_mod, cr_mod):
        monkeypatch.setattr(mod, "chat_json", fake)
    res = await Orchestrator(db, replace(settings, llm_mode="live")).handle(make_report("rtc", casualties_reported=4))
    r = res.response
    assert r.triage.executed_by == ExecutedBy.qwen
    assert sum(m.quantity for m in r.manifest if m.resource_type == "emergency_ambulance") == 4
    assert any("life-safety floor" in n for n in r.manifest_adjustments)
    assert r.directives[0].action == "Triage on arrival."


async def test_qwen_triage_cannot_lower_caller_counts(db, settings, monkeypatch):
    fake = fake_llm_factory(
        {"manifest": [], "directives": [{"agency": "all", "action": "Go", "priority": "high"}]},
        triage={"category": "nonsense", "severity": 2, "casualties": 0, "trapped": 0, "weapons": False,
                "lat": 51.5, "lon": -0.1, "rationale": "r"},
        agent_requests=lambda s: [])
    for mod in (orch_mod, spec_mod, cr_mod):
        monkeypatch.setattr(mod, "chat_json", fake)
    rep = make_report("rtc", casualties_reported=3, people_trapped_reported=1)
    t = (await Orchestrator(db, replace(settings, llm_mode="live")).handle(rep)).response.triage
    assert t.category == "rtc" and t.casualties == 3 and t.trapped == 1  # unknown category and lowered counts rejected
    assert abs(t.lat - 53.3811) < 1e-6  # caller GPS beats a model's coordinates


async def test_cr_trims_overlapping_requests(db, settings, monkeypatch):
    fake = fake_llm_factory(
        {"manifest": [{"resource_type": "drone", "quantity": 1, "for_agency": "fire", "reason": "one shared"},
                      {"resource_type": "fire_engine", "quantity": 2, "for_agency": "fire", "reason": "PDA"},
                      {"resource_type": "patrol_car", "quantity": 2, "for_agency": "police", "reason": "cordon"}],
         "directives": [{"agency": "all", "action": "Single RVP at the station", "priority": "immediate"}]},
        triage={"category": "structure_fire", "severity": 3, "casualties": 0, "trapped": 0, "weapons": False, "rationale": "r"},
        agencies=(Agency.police, Agency.fire), lead=Agency.fire,
        agent_requests=lambda s: [{"resource_type": "drone", "quantity": 1, "priority": 3},
                                  {"resource_type": "fire_engine" if "A3" in s else "patrol_car", "quantity": 2}])
    for mod in (orch_mod, spec_mod, cr_mod):
        monkeypatch.setattr(mod, "chat_json", fake)
    r = (await Orchestrator(db, replace(settings, llm_mode="live")).handle(make_report("structure_fire"))).response
    assert r.requested_total["drone"] == 2 and sum(1 for a in r.allocations if a.resource_type == "drone") == 1
    assert r.evaluation.metrics["trimmed_units"] == 1


async def test_live_mode_falls_back_when_models_fail(db, settings, monkeypatch):
    async def boom(*a, **k):
        raise LLMError("server down")

    for mod in (orch_mod, spec_mod, cr_mod):
        monkeypatch.setattr(mod, "chat_json", boom)
    r = (await Orchestrator(db, replace(settings, llm_mode="live")).handle(
        make_report("structure_fire", casualties_reported=2, people_trapped_reported=1))).response
    assert r.triage.executed_by == ExecutedBy.fallback and r.routing.executed_by == ExecutedBy.fallback
    assert r.cr_executed_by == ExecutedBy.fallback and r.evaluation.executed_by == ExecutedBy.fallback
    assert r.allocations


# ---- the agent makes the final duplicate call ------------------------------------------ #

FIRE_TRIAGE = {"category": "structure_fire", "severity": 4, "casualties": 0, "trapped": 2, "weapons": False, "rationale": "r"}
SIMPLE_CR = {"manifest": [{"resource_type": "fire_engine", "quantity": 2, "for_agency": "fire", "reason": "PDA"}],
             "directives": [{"agency": "fire", "action": "Attack the fire", "priority": "immediate"}]}


def live(settings):
    return replace(settings, llm_mode="live")


def patch_all(monkeypatch, fake):
    for mod in (orch_mod, spec_mod, cr_mod):
        monkeypatch.setattr(mod, "chat_json", fake)


async def test_agent_overrides_minilm_merge_when_probably_different(db, settings, monkeypatch):
    seen = []
    fake = fake_llm_factory(SIMPLE_CR, triage=FIRE_TRIAGE, agencies=(Agency.fire,), lead=Agency.fire,
                            agent_requests=lambda s: [{"resource_type": "fire_engine", "quantity": 2}], seen=seen,
                            dedup_answer=lambda u: {"verdict": "related", "match_incident_id": int(re.search(r'"incident_id": (\d+)', u.split("CANDIDATES")[1]).group(1)),
                                                    "reason": "Different building on the same estate."})
    patch_all(monkeypatch, fake)
    orch = Orchestrator(db, live(settings))
    a, b = burst()[:2]
    ra = await orch.handle(a)
    rb = await orch.handle(b)
    assert rb.dedup.minilm_verdict.value == "duplicate"            # MiniLM proposed a merge
    assert db.get_incident(rb.incident_id)["dedup"]["executed_by"] == "qwen"
    assert rb.dedup.verdict.value == "related" and rb.dedup.executed_by == ExecutedBy.qwen  # the agent decided
    assert rb.outcome == "dispatched" and rb.response.related_incidents == [ra.incident_id]
    assert '"minilm_clustered": true' in seen[0] and "MINILM PROPOSAL: duplicate" in seen[0]


async def test_agent_merges_what_minilm_kept_apart(db, settings, monkeypatch):
    """The landmark case: MiniLM does not cluster two callers, the agent sees the nearby candidate and merges."""
    fake = fake_llm_factory(SIMPLE_CR, triage=FIRE_TRIAGE, agencies=(Agency.fire,), lead=Agency.fire,
                            agent_requests=lambda s: [{"resource_type": "fire_engine", "quantity": 2}],
                            dedup_answer=lambda u: {"verdict": "duplicate", "match_incident_id": int(re.search(r'"incident_id": (\d+)', u.split("CANDIDATES")[1]).group(1)),
                                                    "reason": "Same block, same smoke, two callers."})
    patch_all(monkeypatch, fake)
    orch = Orchestrator(db, live(settings))
    xs = burst()
    ra = await orch.handle(xs[2])          # caller C: "near the station"
    rd = await orch.handle(xs[3])          # caller D: text, landmark "Park Hill flats" -> landmark conflict for MiniLM
    assert rd.dedup.minilm_verdict.value != "duplicate"
    assert rd.outcome == "merged" and rd.dedup.match_incident_id == ra.incident_id
    stored = db.get_incident(rd.incident_id)["dedup"]
    assert stored["verdict"] == "duplicate" and stored["minilm_verdict"] != "duplicate" and stored["candidates"]
    assert db.get_incident(ra.incident_id)["report_count"] == 2


async def test_invalid_or_failed_agent_falls_back_to_minilm(db, settings, monkeypatch):
    for answer in ({"verdict": "duplicate", "match_incident_id": 12345, "reason": "x"}, LLMError("down")):
        fake = fake_llm_factory(SIMPLE_CR, triage=FIRE_TRIAGE, agencies=(Agency.fire,), lead=Agency.fire,
                                agent_requests=lambda s: [{"resource_type": "fire_engine", "quantity": 2}], dedup_answer=answer)
        patch_all(monkeypatch, fake)
        orch = Orchestrator(db, live(settings))
        a, b = burst()[:2]
        await orch.handle(a)
        rb = await orch.handle(b)
        assert rb.dedup.executed_by == ExecutedBy.minilm and rb.outcome == "merged"
        assert "MiniLM verdict used" in rb.dedup.reason


async def test_no_candidates_means_no_agent_call(db, settings, monkeypatch):
    seen = []
    fake = fake_llm_factory(SIMPLE_CR, triage=FIRE_TRIAGE, agencies=(Agency.fire,), lead=Agency.fire,
                            agent_requests=lambda s: [], dedup_answer={"verdict": "new", "reason": "x"}, seen=seen)
    patch_all(monkeypatch, fake)
    await Orchestrator(db, live(settings)).handle(burst()[0])
    assert seen == []


def test_merge_follows_chains(db):
    from .conftest import make_incident
    a, b, c = (make_incident() for _ in range(3))
    for x in (a, b, c):
        db.insert_incident(x.incident, x.triage)
    assert db.merge_incident(c.incident_id, b.incident_id) == b.incident_id   # c -> b
    assert db.merge_incident(b.incident_id, a.incident_id) == a.incident_id   # b -> a moves c to a as well
    assert db.get_incident(c.incident_id)["duplicate_of"] == a.incident_id
    assert db.get_incident(a.incident_id)["report_count"] == 3


# ---- inventory, brief, db ----------------------------------------------------------- #


async def test_ledger_and_autonomous_return(db, settings):
    res = await Orchestrator(db, settings).handle(load("simple_rtc.json")[0])
    n_units = len(res.response.allocations)
    n_people = sum(len(a.crew) for a in res.response.allocations)
    led = db.ledger(500, res.incident_id)
    assert sum(1 for x in led if x["event"] == "dispatched" and x["item_kind"] == "unit") == n_units
    assert sum(1 for x in led if x["event"] == "dispatched" and x["item_kind"] == "person") == n_people
    db._conn.execute("UPDATE allocations SET expected_release_at = '2000-01-01T00:00:00+00:00'")
    db._conn.commit()
    assert len(db.release_due()) == n_units
    assert db.get_incident(res.incident_id)["status"] == "closed"
    assert all(p["committed"] == 0 for p in db.personnel_inventory())


async def test_phone_brief_is_normalised(db, settings):
    orch = Orchestrator(db, settings)
    a, b, *_ = burst()
    ra = await orch.handle(a)
    rb = await orch.handle(b)
    brief = build_brief(db, rb.incident_id)
    assert brief["incident_id"] == ra.incident_id and brief["opened_from_duplicate"] == rb.incident_id
    assert brief["headline"] and brief["dateline"].startswith("SHEFFIELD")
    assert brief["facts"]["Reports"] == 2 and brief["facts"]["Channel"] == "999"
    jsonschema.validate(brief["incident"], SCHEMA)
    assert brief["units"] and all(u["crew"] for u in brief["units"])
    kinds = [l["kind"] for l in brief["log"]]
    assert kinds[0] == "received" and "completed" in kinds and kinds[-1] == "merged"


async def test_foreign_key_and_close(db, settings):
    res = await Orchestrator(db, settings).handle(load("simple_rtc.json")[0])
    with pytest.raises(sqlite3.IntegrityError):
        bogus = res.response.model_copy(update={"response_id": 1, "incident_id": 99})
        case = db.get_case(res.incident_id)
        db.record_response(case.model_copy(update={"incident": case.incident.model_copy(update={"incident_id": 99})}), bogus)
    assert db.close_incident(res.incident_id) == len(res.response.allocations)


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


# ---- Nemotron skills as knowledge ------------------------------------------------------ #


def test_skills_are_loaded_without_their_io_contract():
    from urban_response import skills
    for a in ("police", "ambulance", "fire"):
        k = skills.service_skill(a)
        assert "Assessment method" in k and "Guardrails" in k and "severity 1â€“5" in k
        assert "proposal_schema" not in k and "police_patrol_car" not in k and "requires_human_review" not in k
    arb = skills.arbiter_skill()
    assert "Respect specialist proposals" in arb and "allocator_output_schema" not in arb
    assert "Human review is mandatory" not in arb


async def test_agents_and_cr_get_their_skills(db, settings, monkeypatch):
    seen = {}

    async def fake(ep, system, user, out, **k):
        seen[k.get("label", "")] = system
        raise LLMError("only capturing prompts")

    patch_all(monkeypatch, fake)
    res = await Orchestrator(db, live(settings)).handle(load("sheffield_demo.json")[0])  # trapped casualties: all three agencies
    assert "fire-and-rescue-focused assessment agent" in seen["A3 fire"] and "resource_type MUST be one of" in seen["A3 fire"]
    assert "ambulance-focused" in seen["A2 ambulance"] and "police-focused" in seen["A1 police"]
    assert "ARBITER SKILL" in seen["CR"] and "Respect specialist proposals" in seen["CR"] and "preempt" in seen["CR"]
    # rule fallbacks still cite the skill as knowledge
    assert any(c.startswith("skill:") for a in res.response.agent_responses for c in a.kb_citations)


# ---- vehicle lifecycle timing ------------------------------------------------------------ #


async def test_vehicle_cycle_and_replenishment(db, settings):
    from datetime import datetime, timedelta, timezone
    from urban_response import timing
    res = await Orchestrator(db, settings).handle(load("simple_rtc.json")[0])  # 1 casualty
    allocs = res.response.allocations
    for a in allocs:
        assert [s["stage"] for s in a.timeline] == list(timing.STAGES)
        assert a.back_in_service_min == timing.total_minutes(a.timeline) > a.eta_min
    amb = [a for a in allocs if a.resource_type == "emergency_ambulance"]
    assert amb and "conveying to" in amb[0].timeline[2]["detail"] and "handover" in amb[0].timeline[3]["detail"]
    car = next(a for a in allocs if a.resource_type == "patrol_car")
    assert car.timeline[3]["minutes"] == timing.COOLDOWN_MIN["patrol_car"][0]

    fleet = {f["unit_id"]: f for f in db.fleet()}
    assert fleet[amb[0].unit_id]["stage"] == "en_route"
    # Move the clock: 1 simulated minute before the cycle ends, the ambulance is in cooldown; after it, it is released.
    start = datetime.now(timezone.utc) - timedelta(minutes=amb[0].back_in_service_min - 1)
    db._conn.execute("UPDATE allocations SET start_at = ? WHERE unit_id = ?", (start.isoformat(), amb[0].unit_id))
    db._conn.commit()
    assert {f["unit_id"]: f for f in db.fleet()}[amb[0].unit_id]["stage"] == "cooldown"
    db._conn.execute("UPDATE allocations SET expected_release_at = '2000-01-01T00:00:00+00:00' WHERE unit_id = ?", (amb[0].unit_id,))
    db._conn.commit()
    assert [u["unit_id"] for u in db.release_due()] == [amb[0].unit_id]
    assert amb[0].unit_id not in {f["unit_id"] for f in db.fleet()}  # back in the inventory


def test_major_trauma_goes_to_northern_general(db):
    from urban_response import timing
    unit = db.available_units("emergency_ambulance", 53.38, -1.47)[0]
    t = timing.timeline(db, unit, "emergency_ambulance", unit["eta_min"], 30, 53.3770, -1.4830, severity=5, conveys=True)
    assert "Northern General" in t[2]["detail"]
