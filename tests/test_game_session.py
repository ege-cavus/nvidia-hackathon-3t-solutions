"""Fairness and controller-independence checks for the future game boundary."""
import asyncio
import json
import unittest
from unittest.mock import patch
from game_session import SoloMatch, Scenario, ScenarioEvent, make_demo_scenario
from state_schema import DispatchCommand, is_unit_available
from agent_orchestrator import qwen_initial_triage


def scenario():
    return Scenario((ScenarioEvent(1, "CAM_01", "fire", 4), ScenarioEvent(5, "CAM_08", "trauma", 5)), 30)


class MatchTests(unittest.IsolatedAsyncioTestCase):
    async def test_identical_choices_produce_identical_scorecards(self):
        match = SoloMatch(scenario())
        async def controller(world):
            cam = "CAM_01" if world.state["tick"] == 1 else "CAM_08"
            unit = "FIRE_1" if cam == "CAM_01" else "AMB_1"
            world.dispatch(cam,[unit])
        for _ in range(30):
            match.advance()
            if match.tick in (1,5):
                cam = "CAM_01" if match.tick == 1 else "CAM_08"
                unit = "FIRE_1" if cam == "CAM_01" else "AMB_1"
                receipt = match.submit_human(DispatchCommand(cam,(unit,),match.tick,"HUMAN"))
                self.assertTrue(receipt["accepted"])
                await match.run_agents(controller)
        self.assertTrue(match.finished)
        scores=match.scoreboard()
        self.assertEqual(scores["HUMAN"],scores["AGENTS"])
        self.assertEqual(scores["HUMAN"]["incidents_resolved"],2)
        self.assertEqual(match.replay_commands("HUMAN")[0]["controller"],"HUMAN")
        self.assertEqual(match.replay_commands("AGENTS")[0]["controller"],"AGENTS")

    async def test_different_actions_do_not_change_external_events(self):
        match=SoloMatch(scenario())
        match.advance()
        match.submit_human(DispatchCommand("CAM_01",("FIRE_1",),1,"HUMAN"))
        for _ in range(4): match.advance()
        human, agents = match.observation("HUMAN"),match.observation("AGENTS")
        self.assertEqual(human["active_incidents"]["CAM_08"],agents["active_incidents"]["CAM_08"])
        self.assertEqual(agents["resource_db"]["FIRE_1"]["status"],"AVAILABLE")
        self.assertNotEqual(human["active_incidents"]["CAM_01"]["severity"],agents["active_incidents"]["CAM_01"]["severity"])
        human["resource_db"]["FIRE_1"]["lat"]=0
        self.assertNotEqual(match.observation("HUMAN")["resource_db"]["FIRE_1"]["lat"],0)

    async def test_stale_and_duplicate_commands_are_inert(self):
        match=SoloMatch(scenario());match.advance()
        stale=match.submit_human(DispatchCommand("CAM_01",("FIRE_1",),0,"HUMAN"))
        self.assertFalse(stale["accepted"])
        self.assertTrue(is_unit_available(match.observation("HUMAN")["resource_db"]["FIRE_1"]))
        command=DispatchCommand("CAM_01",("FIRE_1",),1,"HUMAN")
        self.assertTrue(match.submit_human(command)["accepted"])
        self.assertFalse(match.submit_human(DispatchCommand("CAM_01",("FIRE_2",),1,"HUMAN"))["accepted"])
        self.assertTrue(is_unit_available(match.observation("HUMAN")["resource_db"]["FIRE_2"]))
        self.assertFalse(match.submit_human(DispatchCommand("UNKNOWN",("FIRE_2",),1,"HUMAN"))["accepted"])

    async def test_model_latency_does_not_advance_clock(self):
        match=SoloMatch(scenario());match.advance()
        started,release=asyncio.Event(),asyncio.Event()
        async def controller(world):
            started.set();await release.wait()
        task=asyncio.create_task(match.run_agents(controller))
        await started.wait()
        with self.assertRaises(RuntimeError): match.advance()
        self.assertEqual(match.tick,1)
        release.set();await task
        with self.assertRaises(ValueError): await match.run_agents(controller)

    async def test_qwen_cannot_rewrite_world_facts(self):
        match=SoloMatch(scenario());match.advance()
        async def controller(world):
            class Client:
                async def chat(self,**kwargs):
                    return {"message":{"content":json.dumps({"location":"Station", "incident_type":"trauma", "severity":10,
                        "road_blocked":True,"summary":"Deliberately incorrect report","required_agencies":["AMBULANCE"]})}}
            with patch('agent_orchestrator._frame',return_value='mock-image'):
                await qwen_initial_triage(world.state,"CAM_01",sim=world,live=True,client=Client())
            self.assertTrue(world.validate_allocation("CAM_01",["FIRE_1"])[0])
            self.assertFalse(world.validate_allocation("CAM_01",["AMB_1"])[0])
        await match.run_agents(controller)
        incident=match.observation("AGENTS")["active_incidents"]["CAM_01"]
        self.assertEqual((incident["hazard"],incident["severity"],incident["road_blocked"]),("fire",4,False))
        self.assertEqual(incident["qwen_report"]["incident_type"],"trauma")

    async def test_command_replay_reproduces_human_score(self):
        original=SoloMatch(scenario())
        original.advance()
        original.submit_human(DispatchCommand("CAM_01",("FIRE_1",),1,"HUMAN"))
        for _ in range(29): original.advance()
        replay=SoloMatch(scenario())
        commands=original.replay_commands("HUMAN")
        for _ in range(30):
            replay.advance()
            for entry in commands:
                if entry["tick"]==replay.tick:
                    replay.submit_human(DispatchCommand(entry["cam_id"],tuple(entry["unit_ids"]),entry["expected_tick"],"HUMAN"))
        self.assertEqual(original.scoreboard(),replay.scoreboard())

    async def test_scenario_validation_and_fixed_horizon(self):
        self.assertEqual(make_demo_scenario(42),make_demo_scenario(42))
        with self.assertRaises(ValueError):
            Scenario((ScenarioEvent(1,"CAM_01","fire",4),ScenarioEvent(2,"CAM_01","fire",5)))
        match=SoloMatch(Scenario((),2))
        with self.assertRaises(ValueError): match.submit_human(DispatchCommand("CAM_01",("FIRE_1",),0,"HUMAN"))
        match.advance();match.advance()
        with self.assertRaises(ValueError): match.advance()
        with self.assertRaises(ValueError): match.submit_human(DispatchCommand("CAM_01",("FIRE_1",),2,"HUMAN"))
