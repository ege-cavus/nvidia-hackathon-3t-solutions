"""Concurrent dashboard polls must not share SQLite across worker threads."""
from concurrent.futures import ThreadPoolExecutor
import json
import threading

from fastapi.testclient import TestClient

from report_world import DEFAULT_SCENARIO
from tests.test_report_world import test_graph as make_graph
from urban_response.world import GraphReportWorld, WorldDatabase


def test_concurrent_polls_and_effects_keep_database_on_its_owner(tmp_path,monkeypatch):
    from urban_response import api,dedup

    def owner_database(path):
        db = WorldDatabase(path)
        connection,owner = db._conn,threading.get_ident()
        class OwnerConnection:
            def __getattr__(self,name):
                value = getattr(connection,name)
                if not callable(value):
                    return value
                def call(*args,**kwargs):
                    assert threading.get_ident() == owner, "SQLite escaped its owner thread"
                    return value(*args,**kwargs)
                return call
        db._conn = OwnerConnection()
        return db

    scenario=json.loads(DEFAULT_SCENARIO.read_text())
    scenario["schema"]=str((DEFAULT_SCENARIO.parent/scenario["schema"]).resolve())
    scenario["initial_reports"]=scenario["initial_reports"][:1]
    path=tmp_path/"scenario.json"
    path.write_text(json.dumps(scenario))
    monkeypatch.setenv("UR_WORLD_SCENARIO",str(path))
    monkeypatch.setenv("UR_WORLD_AUTORUN","0")
    monkeypatch.setenv("UR_DB_PATH",":memory:")
    monkeypatch.setenv("LLM_MODE","mock")
    monkeypatch.setattr(api,"WorldDatabase",owner_database)
    monkeypatch.setattr(api,"GraphReportWorld",lambda db,p:GraphReportWorld(db,p,graph=make_graph()))
    monkeypatch.setattr(dedup,"load_model",lambda:None)
    with TestClient(api.app) as client:
        dispatch=client.post("/world/dispatch-due")
        assert dispatch.status_code==200
        response_id=dispatch.json()[0]["response"]["response_id"]
        paths=["/api/state",f"/responses/{response_id}","/ui/units","/ui/responses","/ui/world"]
        with ThreadPoolExecutor(max_workers=8) as pool:
            polls=[pool.submit(client.get,path) for path in paths*4]
            assert client.post("/ui/world/advance").status_code==200
            assert all(f.result().status_code==200 for f in polls)
        assert client.post("/ui/reset").status_code==200
