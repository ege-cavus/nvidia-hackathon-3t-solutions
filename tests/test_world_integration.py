"""Canonical backend -> concrete allocations -> observed graph outcomes."""
from copy import deepcopy
import json
from pathlib import Path
from unittest.mock import patch

import pytest
from jsonschema import Draft202012Validator

from report_world import DEFAULT_SCENARIO
from tests.test_report_world import test_graph as make_graph
from urban_response.agents.orchestrator import Orchestrator
from urban_response.schemas import Incident
from urban_response.seed import seed
from urban_response.world import GraphReportWorld, WorldConflict, WorldDatabase


@pytest.fixture
def integrated(tmp_path, settings):
    scenario=json.loads(DEFAULT_SCENARIO.read_text())
    scenario["schema"]=str((DEFAULT_SCENARIO.parent/scenario["schema"]).resolve())
    scenario["initial_reports"]=scenario["initial_reports"][:1]
    path=tmp_path/"scenario.json"
    path.write_text(json.dumps(scenario))
    db=WorldDatabase(":memory:")
    seed(db)
    world=GraphReportWorld(db,path,graph=make_graph())
    db.graph_world=world
    yield db,world,Orchestrator(db,settings)
    db.close()


def test_allocator_schema_is_json_and_accepts_required_review_flag():
    root=Path(__file__).resolve().parents[1]
    schema=json.loads((root/"nemotron_resource_skills/resource_allocator_skill/allocator_output_schema.json").read_text())
    Draft202012Validator.check_schema(schema)
    Draft202012Validator(schema).validate({"snapshot_id":"test", "status":"proposed", "allocations":[],
        "resource_totals":[], "review_flags":[], "requires_human_review":True})


@pytest.mark.asyncio
async def test_dispatch_does_not_resolve_or_release_before_actual_work(integrated):
    db,world,orch=integrated
    results=await world.dispatch_due(orch)
    response=results[0].response
    assert {a.agency.value for a in response.agent_responses}=={"police","ambulance","fire"}
    assert response.allocations
    assert world.state["active_incidents"]["CAM_01"]["status"]!="RESOLVED"
    ids={a.unit_id for a in response.allocations}
    assert ids <= set(world.state["resource_db"])
    assert all(world.state["resource_db"][k]["status"]=="DEPLETED_EN_ROUTE" for k in ids)
    # A forecast in the past cannot override the world's lifecycle lockout.
    db._conn.execute("UPDATE allocations SET expected_release_at='2000-01-01T00:00:00+00:00'")
    db._conn.commit()
    assert db.release_due()==[]
    assert db._conn.execute("SELECT COUNT(*) FROM crew_assignments WHERE released_at IS NULL").fetchone()[0]>0
    for _ in range(360):
        world.tick()
        if all(world.state["resource_db"][k]["status"]=="AVAILABLE" for k in ids):
            break
    assert world.state["active_incidents"]["CAM_01"]["status"]=="RESOLVED"
    assert world.spread_events[0]["outcome"]=="PREVENTED"
    assert db._conn.execute("SELECT COUNT(*) FROM allocations WHERE released_at IS NULL").fetchone()[0]==0
    assert db._conn.execute("SELECT COUNT(*) FROM crew_assignments WHERE released_at IS NULL").fetchone()[0]==0
    stored=db.get_response(response.response_id)
    assert stored["evaluation"]["metrics"]["world_outcome"]["resolution_success"]


@pytest.mark.asyncio
async def test_database_failure_rolls_back_world_dispatch(integrated):
    db,world,orch=integrated
    before=deepcopy(world.state["resource_db"])
    with patch("urban_response.db.Database.record_response",side_effect=RuntimeError("commit failed")):
        with pytest.raises(RuntimeError,match="commit failed"):
            await world.dispatch_due(orch)
    assert world.state["resource_db"]==before
    assert world.responses=={}
    assert db._conn.execute("SELECT COUNT(*) FROM allocations").fetchone()[0]==0


def test_world_pauses_during_decisions_and_rejects_manual_release(integrated):
    db,world,_=integrated
    world.inflight=1
    with pytest.raises(WorldConflict):world.tick()
    assert world.state["tick"]==0
    with pytest.raises(WorldConflict):db.release_unit(next(iter(world.state["resource_db"])))
    with pytest.raises(WorldConflict):db.close_incident(20261113000001)


def test_specialist_vehicle_does_not_replace_required_fire_engine(integrated):
    _,world,_=integrated
    unit=next(u for u in world.state["resource_db"].values() if u["resource_type"]=="aerial_ladder_platform")
    unit.update(assigned_cam="CAM_01",status="DEPLETED_ON_SCENE")
    metrics=world.evaluate_response("CAM_01")
    assert not metrics["capability_coverage"]
    assert not metrics["resolution_success"]


def test_dashboard_api_uses_world_reservations_and_blocks_manual_release(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient
    from urban_response import api, dedup
    scenario=json.loads(DEFAULT_SCENARIO.read_text())
    scenario["schema"]=str((DEFAULT_SCENARIO.parent/scenario["schema"]).resolve())
    scenario["initial_reports"]=scenario["initial_reports"][:1]
    path=tmp_path/"scenario.json"
    path.write_text(json.dumps(scenario))
    monkeypatch.setenv("UR_WORLD_SCENARIO",str(path))
    monkeypatch.setenv("UR_WORLD_AUTORUN","0")
    monkeypatch.setenv("LLM_MODE","mock")
    monkeypatch.setenv("UR_DB_PATH",str(tmp_path/"demo.db"))
    monkeypatch.setattr(api,"GraphReportWorld",lambda db,p:GraphReportWorld(db,p,graph=make_graph()))
    monkeypatch.setattr(dedup,"load_model",lambda:None)
    with TestClient(api.app) as client:
        assert client.get("/").status_code==200
        assert "location.origin" in client.get("/config.js").text
        assert client.get("/world/state").json()["tick"]==0
        result=client.post("/world/dispatch-due")
        assert result.status_code==200,result.text
        response=result.json()[0]["response"]
        unit_id=response["allocations"][0]["unit_id"]
        assert client.post(f"/ui/units/{unit_id}/release").status_code==409
        units=client.get("/ui/units").json()
        unit=next(u for u in units if u["id"]==unit_id)
        assert unit["status"]=="committed"
        assert unit["world_stage"]=="DEPLETED_EN_ROUTE"
        assert client.post("/world/advance").json()["tick"]==1
        assert client.get("/ui/events").json()
        summary=client.get("/ui/world").json()
        assert summary["enabled"] and summary["tick"]==1
        assert str(response["incident_id"]) in summary["outcomes"]


def test_only_explicitly_approved_media_is_loaded(integrated,tmp_path,monkeypatch):
    from PIL import Image
    from urban_response import world as module
    _,world,_=integrated
    report=Incident.model_validate(world.reports[0])
    world.pending_reports[id(report)]="CAM_01"
    assert world.intake_image(report) is None
    monkeypatch.setattr(module,"ROOT",tmp_path)
    Image.new("RGB",(32,24)).save(tmp_path/"approved.jpg")
    world.scenario["approved_media"]={str(report.incident_id):{"approved":True,"image_path":"approved.jpg"}}
    assert world.intake_image(report).startswith("data:image/jpeg;base64,")
    world.scenario["approved_media"][str(report.incident_id)]["image_path"]="../outside.jpg"
    with pytest.raises(WorldConflict):world.intake_image(report)


@pytest.mark.asyncio
async def test_shared_fleet_processes_all_four_authored_reports(settings):
    db=WorldDatabase(":memory:")
    try:
        seed(db)
        world=GraphReportWorld(db,DEFAULT_SCENARIO,graph=make_graph())
        db.graph_world=world
        orch=Orchestrator(db,settings)
        results=await world.dispatch_due(orch)
        assert len(results)==4
        assert len(db.recent_incidents(60))==4  # scenario time, not the host wall clock
        for _ in range(360):
            world.tick()
        assert world.state["effectiveness_metrics"]["incidents_resolved"]==4
        assert world.spread_events[0]["outcome"]=="PREVENTED"
        assert db._conn.execute("SELECT COUNT(*) FROM allocations WHERE released_at IS NULL").fetchone()[0]==0
    finally:
        db.close()
