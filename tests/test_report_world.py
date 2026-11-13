"""Authored-report lifecycle tests, with synthetic streets and no network/models."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import networkx as nx
from jsonschema import ValidationError
from report_world import DEFAULT_SCENARIO, ReportDrivenWorld, scripted_response
from state_schema import EMERGENCY_HUBS, HERO_CAMERAS


def test_graph():
    scenario=json.loads(DEFAULT_SCENARIO.read_text())
    graph=nx.MultiDiGraph(crs="EPSG:4326",source="synthetic_report_tests")
    points=list(EMERGENCY_HUBS.values())+list(HERO_CAMERAS.values())
    for i,point in enumerate(points,1):
        graph.add_node(i,x=point["lon"],y=point["lat"])
        for offset in (1,2,3):
            graph.add_node(1000+i*10+offset,x=point["lon"]+offset*0.0007,y=point["lat"]+0.0007)
    for node in scenario["nodes"].values():
        if "graph_node" in node:graph.add_node(node["graph_node"],x=node["lon"],y=node["lat"])
    for a in graph:
        for b in graph:
            if a!=b:graph.add_edge(a,b,length=100,travel_time=120,osmid=a*10000000000+b)
    return graph


class ReportWorldTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):cls.graph=test_graph()

    def world(self):return ReportDrivenWorld(graph=self.graph)

    def test_initial_reports_match_user_schema(self):
        w=self.world()
        self.assertEqual(len(w.reports),4)
        for report in w.reports:self.assertTrue(w.validate_report(report))
        bad=deepcopy(w.reports[0]);bad["casualties_reported"]=-1
        with self.assertRaises(ValidationError):w.validate_report(bad)
        bad=deepcopy(w.reports[0]);bad["received_at"]="not-a-date"
        with self.assertRaises((ValidationError,ValueError)):w.validate_report(bad)

    def test_spread_emits_one_new_timed_causal_report(self):
        w=self.world()
        for _ in range(5):w.tick()
        self.assertEqual(len(w.reports),4)
        w.tick()
        self.assertEqual(len(w.reports),5)
        child=w.reports[-1]
        self.assertEqual(child["received_at"],"2026-11-13T09:06:00Z")
        self.assertEqual(child["incident_id"],20261113000005)
        self.assertTrue(w.validate_report(child))
        self.assertEqual(w.spread_events[0]["parent_incident_id"],20261113000001)
        target=w.spread_events[0]["node_id"]
        self.assertEqual(w.state["active_incidents"][target]["incident_id"],child["incident_id"])
        for _ in range(5):w.tick()
        self.assertEqual(len(w.reports),5)

    def test_response_updates_world_and_prevents_spread(self):
        w=self.world()
        for _ in range(40):scripted_response(w);w.tick()
        self.assertEqual(len(w.reports),4)
        self.assertEqual(w.spread_events[0]["outcome"],"PREVENTED")
        self.assertEqual(w.state["effectiveness_metrics"]["incidents_resolved"],4)
        self.assertTrue(all(u["status"]=="AVAILABLE" for u in w.state["resource_db"].values()))
        self.assertTrue(w.response_events)
        self.assertTrue(any(e["success_metrics"]["containment_success"] for e in w.response_events))
        self.assertTrue(any(e["severity_after"]<e["severity_before"] for e in w.response_events))
        self.assertTrue(w.validate_world_invariants())

    def test_wrong_capability_cannot_claim_success(self):
        w=self.world()
        self.assertFalse(w.validate_allocation("CAM_01",["AMB_1"])[0])
        for _ in range(4):w.tick()
        metrics=w.evaluate_response("CAM_01")
        self.assertFalse(metrics["capability_coverage"])
        self.assertFalse(metrics["resolution_success"])
        self.assertEqual(metrics["effective_work_ticks"],0)

    def test_duplicate_reports_are_inert(self):
        w=self.world();before=deepcopy(w.state)
        with self.assertRaises(ValueError):w.ingest_report(w.reports[0],"CAM_01")
        self.assertEqual(w.state,before)

    def test_observation_is_detached_and_hides_evaluation(self):
        w=self.world();o=w.qwen_observation(20261113000001)
        self.assertEqual(set(o),{"report","observed_at","graph_context","media"})
        self.assertNotIn("required_units",str(o))
        self.assertNotIn("containment_work_ticks",str(o))
        self.assertNotIn("spread_rules",str(o))
        o["report"]["description"]="changed"
        self.assertNotEqual(w.reports[0]["description"],"changed")

    def test_no_legacy_random_spawn_or_escalation(self):
        w=self.world();severity=w.state["active_incidents"]["CAM_01"]["severity"]
        for _ in range(4):w.tick()
        self.assertEqual(w.state["active_incidents"]["CAM_01"]["severity"],severity)
        with self.assertRaises(ValueError):w.tick(spawn=True)

    def test_exports_are_schema_valid(self):
        w=self.world()
        for _ in range(6):w.tick()
        with tempfile.TemporaryDirectory() as folder:
            result=w.export(folder)
            self.assertEqual(result["reports"],5)
            for report in w.reports:
                exported=json.loads((Path(folder)/f"{report['incident_id']}.json").read_text())
                self.assertTrue(w.validate_report(exported))

    def test_follow_up_bundle_arrives_without_duplicating_incident(self):
        bundle=DEFAULT_SCENARIO.parent/"same_incident_reports.json"
        w=ReportDrivenWorld(graph=self.graph,follow_up_path=bundle)
        self.assertEqual(len(w.follow_up_reports),0)
        w.tick()
        self.assertEqual(len(w.follow_up_reports),4)
        w.tick();w.tick()
        self.assertEqual(len(w.follow_up_reports),12)
        self.assertEqual(len(w.reports),4)
        self.assertEqual(len(w.state["active_incidents"]),4)
        self.assertEqual(w.state["effectiveness_metrics"]["incidents_resolved"],0)
        for item in w.follow_up_reports:
            self.assertTrue(w.validate_report(item["report"]))
            self.assertEqual(item["report"]["incident_id"],20261113000001)

    def test_follow_up_does_not_rewrite_world_or_root_report(self):
        w=self.world();w.tick()
        report=deepcopy(w.reports[0]);report["received_at"]="2026-11-13T09:00:30Z"
        report["description"]="SYNTHETIC alternative caller observation"
        before=deepcopy(w.state);root=deepcopy(w.reports[0])
        w.ingest_follow_up(report,"CALL_1")
        self.assertEqual(w.state,before)
        self.assertEqual(w.reports[0],root)
        observation=w.qwen_follow_up_observation("CALL_1")
        self.assertEqual(observation["report"]["description"],report["description"])
        self.assertEqual(observation["media"]["reference_time"],root["received_at"])
        self.assertNotIn("required_units",str(observation))
        with self.assertRaises(ValueError):w.ingest_follow_up(report,"CALL_1")

    def test_follow_up_rejects_future_and_unknown_incident(self):
        w=self.world();report=deepcopy(w.reports[0]);report["received_at"]="2026-11-13T09:01:00Z"
        with self.assertRaises(ValueError):w.ingest_follow_up(report,"FUTURE")
        report["received_at"]=w.reports[0]["received_at"];report["incident_id"]=20261113999999
        with self.assertRaises(ValueError):w.ingest_follow_up(report,"UNKNOWN")
        self.assertEqual(w.follow_up_reports,[])

    def test_follow_up_bundle_preserves_spread_timing(self):
        a=self.world()
        b=ReportDrivenWorld(graph=self.graph,follow_up_path=DEFAULT_SCENARIO.parent/"same_incident_reports.json")
        for _ in range(6):a.tick();b.tick()
        self.assertEqual(a.spread_events,b.spread_events)
        self.assertEqual(a.state,b.state)
        self.assertEqual(a.reports,b.reports)
        self.assertEqual(len(b.follow_up_reports),12)
