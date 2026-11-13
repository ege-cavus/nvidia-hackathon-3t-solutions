"""Model authority is bounded by observed resources, time and replay guards."""
import asyncio
from copy import deepcopy
from dataclasses import replace
from unittest.mock import patch
from datetime import datetime, timedelta

import pytest

from tests.test_world_integration import integrated
from urban_response import outcomes
from urban_response.llm import LLMError
from urban_response.world import WorldConflict


@pytest.mark.asyncio
async def test_model_loop_waits_for_actual_work_then_resolves_and_returns(integrated):
    db,world,orch = integrated
    world.outcome_mode = "model"
    result = (await world.dispatch_due(orch))[0]
    case_id = result.incident_id
    first = world.outcome_events[0]
    assert first["proposal"]["outcome"] == "no_change"
    assert first["executed_by"] == "mock"
    assert world._work[world.reports[0]["incident_id"]] == 0
    arrivals = []
    for _ in range(360):
        before = world.evaluate_response("CAM_01")
        await world.advance(orch)
        after = world.evaluate_response("CAM_01")
        if not before["capability_coverage"] and after["capability_coverage"]:
            arrivals.append(world.state["tick"])
            assert after["effective_work_ticks"] == 0  # no whole working minute yet
        if world.state["active_incidents"]["CAM_01"]["status"] == "RESOLVED":
            break
    assert arrivals
    accepted = [e for e in world.outcome_events if e["accepted"]]
    assert accepted[-1]["proposal"]["outcome"] == "resolved"
    assert sum(e["proposal"]["work_credit"] for e in accepted) == 5
    assert world.spread_events[0]["outcome"] == "PREVENTED"
    assert db._conn.execute("SELECT COUNT(*) FROM allocations WHERE released_at IS NULL").fetchone()[0] > 0
    # Reassessment at the same minute cannot award work twice or produce more model calls.
    count = len(world.outcome_events)
    assert await world.reassess(orch) == []
    assert len(world.outcome_events) == count
    for _ in range(360):
        await world.advance(orch)
    assert db._conn.execute("SELECT COUNT(*) FROM allocations WHERE released_at IS NULL").fetchone()[0] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM crew_assignments WHERE released_at IS NULL").fetchone()[0] == 0
    assert db._conn.execute("SELECT COUNT(*) FROM world_outcomes WHERE accepted=1").fetchone()[0] == count
    stored = db.get_response(result.response.response_id)
    assert stored["evaluation"]["metrics"]["world_outcome"]["assessment"]["proposal"]["outcome"] == "resolved"
    assert world.case_nodes[case_id] == "CAM_01"


@pytest.mark.asyncio
async def test_invalid_stale_duplicate_and_wrong_incident_cannot_mutate_world(integrated):
    _,world,orch = integrated
    result = (await world.dispatch_due(orch))[0]
    evidence = world.outcome_evidence(result.incident_id)
    proposal = outcomes.mock_proposal(evidence)
    before = deepcopy(world.state)
    with pytest.raises(ValueError,match="working tick"):
        world.apply_outcome(proposal.model_copy(update={"work_credit":1,"outcome":"resolved"}),evidence,"qwen")
    with pytest.raises(ValueError,match="incident_id"):
        world.apply_outcome(proposal.model_copy(update={"incident_id":0}),evidence,"qwen")
    with pytest.raises(ValueError,match="thresholds"):
        world.apply_outcome(proposal.model_copy(update={"outcome":"resolved"}),evidence,"qwen")
    assert world.state == before
    world.world_version += 1
    with pytest.raises(ValueError,match="Stale"):
        world.apply_outcome(proposal,evidence,"qwen")
    evidence = world.outcome_evidence(result.incident_id)
    proposal = outcomes.mock_proposal(evidence)
    world.apply_outcome(proposal,evidence,"qwen")
    with pytest.raises(ValueError,match="Duplicate"):
        world.apply_outcome(proposal,evidence,"qwen")


@pytest.mark.asyncio
async def test_qwen_may_withhold_work_and_timeout_does_not_resolve(integrated):
    _,world,orch = integrated
    world.outcome_mode = "model"
    await world.dispatch_due(orch)
    while not world.evaluate_response("CAM_01")["capability_coverage"]:
        await world.advance(orch)
    severity = world.state["active_incidents"]["CAM_01"]["severity"]
    orch.settings = replace(orch.settings,llm_mode="live")
    async def withhold(endpoint,system,user,out,**kwargs):
        import json
        evidence = json.loads(user.split("\n",1)[1])
        assert evidence["eligible_work_credit"] == 1
        assert evidence["units"] and evidence["game_policy"]
        p = outcomes.mock_proposal(evidence)
        return p.model_copy(update={"work_credit":0,"outcome":"no_change","reason":"No confirmed safe access."}),10
    with patch("urban_response.outcomes.chat_json",side_effect=withhold):
        await world.advance(orch)
    assert world._work[world.reports[0]["incident_id"]] == 0
    assert world.state["active_incidents"]["CAM_01"]["severity"] == severity
    assert world.outcome_events[-1]["executed_by"] == "qwen"
    with patch("urban_response.outcomes.chat_json",side_effect=LLMError("test timeout")):
        await world.advance(orch)
    assert not world.outcome_events[-1]["accepted"]
    assert world._work[world.reports[0]["incident_id"]] == 0
    assert world.inflight == 0
    orch.settings = replace(orch.settings,llm_mode="mock")
    await world.advance(orch)
    assert world._work[world.reports[0]["incident_id"]] == 1


@pytest.mark.asyncio
async def test_partial_attendance_slows_spread_without_work(integrated):
    _,world,orch = integrated
    world.outcome_mode = "model"
    await world.dispatch_due(orch)
    # Remove the fire capability from attendance, leaving a police partial response.
    for unit in world.state["resource_db"].values():
        if unit["assigned_cam"] == "CAM_01" and unit["resource_type"] == "fire_engine":
            unit["resource_type"] = "aerial_ladder_platform"
    for _ in range(6):
        await world.advance(orch)
    assert world.evaluate_response("CAM_01")["effective_work_ticks"] == 0
    assert any(e["accepted"] and e["proposal"]["outcome"] == "partial_response" for e in world.outcome_events)
    assert not world.spread_events
    for _ in range(12):
        await world.advance(orch)
    assert world.spread_events[0]["outcome"] == "SPREAD"
    received = datetime.fromisoformat(world.reports[-1]["received_at"].replace("Z","+00:00"))
    assert received == world.base_time + timedelta(minutes=world.spread_events[0]["tick"])
    assert world.reports[-1]["incident_id"] == world.spread_events[0]["incident_id"]


@pytest.mark.asyncio
async def test_model_window_blocks_clock_and_followup_triggers_new_assessment(integrated):
    _,world,orch = integrated
    world.outcome_mode = "model"
    await world.dispatch_due(orch)
    followup = deepcopy(world.reports[0])
    followup["description"] += " More smoke reported by another caller."
    world.ingest_follow_up(followup,"caller-2")
    started,release = asyncio.Event(),asyncio.Event()
    async def wait_model(settings,evidence):
        assert evidence["follow_up_reports"][0]["observation_id"] == "caller-2"
        started.set()
        await release.wait()
        return outcomes.mock_proposal(evidence),"mock"
    with patch("urban_response.outcomes.propose",side_effect=wait_model):
        task = asyncio.create_task(world.reassess(orch))
        await started.wait()
        with pytest.raises(WorldConflict):await world.advance(orch)
        with pytest.raises(WorldConflict):world.tick()
        release.set()
        events = await task
    assert len(events) == 1
    assert world.inflight == 0


@pytest.mark.asyncio
async def test_failed_audit_commit_rolls_back_effect(integrated):
    db,world,orch = integrated
    world.outcome_mode = "model"
    result = (await world.dispatch_due(orch))[0]
    while not world.evaluate_response("CAM_01")["capability_coverage"]:
        await world.advance(orch)
    world._eligible_nodes = {"CAM_01"}
    evidence = world.outcome_evidence(result.incident_id)
    before = deepcopy(world.state)
    proposal = outcomes.mock_proposal(evidence)
    with patch.object(db,"tx",side_effect=RuntimeError("disk failure")):
        with pytest.raises(RuntimeError,match="disk failure"):
            world.apply_outcome(proposal,evidence,"qwen")
    assert world.state == before
    assert world._work[world.reports[0]["incident_id"]] == 0
