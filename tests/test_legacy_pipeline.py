"""Integration/regression coverage for resource locks and model decision boundaries."""
import asyncio
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import networkx as nx
from state_schema import EMERGENCY_HUBS, get_initial_city_state, is_unit_available, make_incident
from sheffield_sim import SheffieldSim, synthetic_graph, load_sheffield_graph
from agent_orchestrator import (qwen_initial_triage, nemotron_negotiate_on_board,
                                resolve_board_or_qwen_takeover, process_pending)
from generate_assets import apply_super_storm, load_cctv_geojson, prepare_video, extract_frame


def setup_incident(hazard="fire", severity=4, blocked=False):
    sim = SheffieldSim(demo=True)
    sim.state["active_incidents"]["CAM_01"] = make_incident("CAM_01", hazard, severity, 0, blocked)
    return sim


class ContractTests(unittest.TestCase):
    def test_station_assignment_and_fresh_state(self):
        a, b = get_initial_city_state(), get_initial_city_state()
        self.assertEqual(len(a["resource_db"]), 6)
        self.assertEqual(len({u["home_hub"] for u in a["resource_db"].values()}), 6)
        for u in a["resource_db"].values():
            self.assertTrue(is_unit_available(u))
            self.assertEqual(u["lat"], EMERGENCY_HUBS[u["home_hub"]]["lat"])
            for status in ["DEPLETED_EN_ROUTE", "DEPLETED_ON_SCENE", "DEPLETED_RETURNING", "DEPLETED_COOLDOWN"]:
                self.assertFalse(is_unit_available(dict(u, status=status)))
        a["node_probabilities"]["CAM_01"]["spawn_probability"] = 1
        self.assertNotEqual(a["node_probabilities"], b["node_probabilities"])

    def test_seed_reproducibility(self):
        a, b = SheffieldSim(seed=42, demo=True), SheffieldSim(seed=42, demo=True)
        for _ in range(20):
            a.tick(); b.tick()
        self.assertEqual(a.state, b.state)
        self.assertEqual(apply_super_storm(get_initial_city_state()), apply_super_storm(get_initial_city_state()))
        self.assertEqual(set(apply_super_storm(get_initial_city_state())["active_incidents"]), set(a.state["node_probabilities"]))

    def test_directed_graph_and_blocked_return(self):
        graph = nx.MultiDiGraph(source="test")
        hub = EMERGENCY_HUBS["FIRE_CENTRAL_EYRE"]
        graph.add_node("home", x=hub["lon"], y=hub["lat"])
        graph.add_node("cam", x=-1.4628, y=53.3782)
        graph.add_edge("home", "cam", travel_time=61, length=200)
        graph.add_edge("cam", "home", travel_time=181, length=500)
        sim = SheffieldSim(graph=graph)
        estimate = sim.estimate_depletion_ticks("FIRE_1", "CAM_01", 4)
        self.assertEqual(estimate["travel_to_scene_ticks"], 2)
        self.assertEqual(estimate["return_to_base_ticks"], 4)
        sim.state["blocked_edges"] = [("cam", "home", 0)]
        with self.assertRaises(nx.NetworkXNoPath):
            sim.estimate_depletion_ticks("FIRE_1", "CAM_01", 4)

    def test_missing_cache_is_fast_offline(self):
        with tempfile.TemporaryDirectory() as folder:
            graph, mode = load_sheffield_graph(cache=str(Path(folder)/"missing.graphml"))
            self.assertEqual(mode, "synthetic_offline")
            self.assertTrue(nx.is_strongly_connected(graph))

    def test_geojson_count_and_coordinates(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/"cameras.json"
            path.write_text(json.dumps({"type":"FeatureCollection", "features":[{"type":"Feature", "geometry":{"type":"Point","coordinates":[-1.4,53.4]}}]*212}))
            self.assertEqual(len(load_cctv_geojson(path)["features"]),212)
            path.write_text(json.dumps({"type":"FeatureCollection", "features":[]}))
            with self.assertRaises(ValueError): load_cctv_geojson(path)


class AssetTests(unittest.TestCase):
    def test_video_and_frame_preparation(self):
        import cv2
        import numpy as np
        with tempfile.TemporaryDirectory() as folder:
            source, dest, image = [Path(folder)/n for n in ("source.mp4","dest.mp4","frame.jpg")]
            writer = cv2.VideoWriter(str(source), cv2.VideoWriter_fourcc(*"mp4v"), 10, (64,48))
            self.assertTrue(writer.isOpened())
            for _ in range(3): writer.write(np.zeros((48,64,3),dtype=np.uint8))
            writer.release()
            self.assertEqual(prepare_video(source,dest,max_frames=2),2)
            extract_frame(dest,image)
            self.assertTrue(image.is_file())
            with self.assertRaises(ValueError): prepare_video(source,source)


class PipelineTests(unittest.IsolatedAsyncioTestCase):
    async def test_selective_agency_only(self):
        sim=setup_incident()
        await qwen_initial_triage(sim.state,"CAM_01",sim=sim)
        calls=[]
        class Client:
            async def chat(self, **kw):
                calls.append(kw)
                return {"message":{"content":json.dumps({"proposal_text":"Fire proposal", "proposed_units":["FIRE_1"]})}}
        await nemotron_negotiate_on_board(sim.state,"CAM_01",sim=sim,live=True,client=Client())
        self.assertEqual(len(calls),1)
        self.assertIn("NEMOTRON_FIRE", calls[0]["messages"][0]["content"])
        self.assertIn("depletion_estimates",calls[0]["messages"][1]["content"])
        allocation=await resolve_board_or_qwen_takeover(sim.state,"CAM_01",sim=sim)
        self.assertEqual(allocation,["FIRE_1"])
        self.assertEqual(sim.state["message_board"][-1]["consensus_status"],"AGREED")

    async def test_roundtrip_locks_and_countdown(self):
        sim=setup_incident()
        await process_pending(sim.state,sim=sim)
        unit_id=sim.state["active_incidents"]["CAM_01"]["dispatched_units"][0]
        unit=sim.state["resource_db"][unit_id]
        total=unit["total_depletion_ticks_remaining"]
        statuses=set()
        for _ in range(total):
            self.assertFalse(is_unit_available(unit))
            statuses.add(unit["status"])
            before=unit["total_depletion_ticks_remaining"]
            sim.tick(spawn=False)
            self.assertEqual(unit["total_depletion_ticks_remaining"],before-1)
            self.assertEqual(unit["total_depletion_ticks_remaining"],sum(unit[k] for k in
                ["travel_to_scene_ticks","on_scene_ticks","return_to_base_ticks","cooldown_ticks_remaining"]))
        self.assertTrue(is_unit_available(unit))
        self.assertEqual(statuses,{"DEPLETED_EN_ROUTE","DEPLETED_ON_SCENE","DEPLETED_RETURNING","DEPLETED_COOLDOWN"})
        self.assertEqual(unit["lat"],EMERGENCY_HUBS[unit["home_hub"]]["lat"])
        self.assertEqual(sim.state["effectiveness_metrics"]["incidents_resolved"],1)
        self.assertEqual(sim.state["effectiveness_metrics"]["sla_met_count"],1)

    async def test_hospital_handover_before_base_return(self):
        sim=setup_incident("trauma",4)
        await qwen_initial_triage(sim.state,"CAM_01",sim=sim)
        sim.dispatch("CAM_01",["AMB_2"])
        unit=sim.state["resource_db"]["AMB_2"]
        saw_handover=False
        for _ in range(80):
            if unit["status"]=="DEPLETED_COOLDOWN":
                saw_handover=True
                self.assertEqual(unit["lat"],EMERGENCY_HUBS["NHS_NORTHERN_GEN"]["lat"])
            sim.tick(spawn=False)
            if is_unit_available(unit): break
        self.assertTrue(saw_handover)
        self.assertTrue(is_unit_available(unit))
        self.assertEqual(unit["lat"],EMERGENCY_HUBS["NHS_ROYAL_HALLAM"]["lat"])

    async def test_timeout_triggers_validated_takeover(self):
        sim=setup_incident()
        await qwen_initial_triage(sim.state,"CAM_01",sim=sim)
        class SlowClient:
            async def chat(self,**kw):
                await asyncio.sleep(1)
        await nemotron_negotiate_on_board(sim.state,"CAM_01",sim=sim,live=True,client=SlowClient(),timeout=0.001)
        allocation=await resolve_board_or_qwen_takeover(sim.state,"CAM_01",sim=sim)
        self.assertTrue(allocation)
        self.assertEqual(sim.state["message_board"][-1]["sender"],"QWEN_GOLD_ARBITER")
        self.assertEqual(sim.state["message_board"][-1]["consensus_status"],"QWEN_OVERRIDDEN")

    async def test_invalid_arbiter_cannot_dispatch(self):
        sim=setup_incident()
        await qwen_initial_triage(sim.state,"CAM_01",sim=sim)
        class Client:
            async def chat(self,**kw):
                return {"message":{"content":json.dumps({"proposal_text":"Invalid", "proposed_units":["NOT_A_UNIT"]})}}
        units=await resolve_board_or_qwen_takeover(sim.state,"CAM_01",sim=sim,live=True,client=Client())
        self.assertEqual(units,[])
        self.assertTrue(all(is_unit_available(u) for u in sim.state["resource_db"].values()))
        self.assertEqual(sim.state["active_incidents"]["CAM_01"]["status"],"QWEN_ARBITRATING")

    async def test_reserve_conflict_and_atomic_rejection(self):
        sim=setup_incident("fire",8)
        await qwen_initial_triage(sim.state,"CAM_01",sim=sim)
        for p in sim.state["node_probabilities"].values():
            p["spawn_probability"]=0.5
        valid,_=sim.validate_allocation("CAM_01",["FIRE_1","FIRE_2","POL_1"])
        self.assertFalse(valid)
        with self.assertRaises(ValueError): sim.dispatch("CAM_01",["FIRE_1","FIRE_2","POL_1"])
        self.assertTrue(all(is_unit_available(u) for u in sim.state["resource_db"].values()))
        self.assertTrue(sim.validate_allocation("CAM_01",["FIRE_1","POL_1"])[0])

    async def test_live_triage_validation_and_safety_floor(self):
        sim=setup_incident("flood",8,True)
        class Client:
            async def chat(self,**kw):
                return {"message":{"content":json.dumps({"location":"Station", "incident_type":"flood", "severity":8,
                    "road_blocked":True, "summary":"Visible flooding", "required_agencies":["FIRE"]})}}
        with patch('agent_orchestrator._frame',return_value='image'):
            report=await qwen_initial_triage(sim.state,"CAM_01",sim=sim,live=True,client=Client())
        self.assertIn("POLICE",report["required_agencies"])

    async def test_overdue_response_counts_once(self):
        sim=setup_incident()
        for _ in range(12): sim.tick(spawn=False)
        self.assertEqual(sim.state["effectiveness_metrics"]["sla_missed_count"],1)
        await process_pending(sim.state,sim=sim)
        for _ in range(30): sim.tick(spawn=False)
        self.assertEqual(sim.state["effectiveness_metrics"]["sla_missed_count"],1)


if __name__ == "__main__":
    unittest.main()
