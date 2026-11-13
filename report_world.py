"""Authored reports -> graph state -> causal spread reports -> evaluated outcomes.

This runner never invokes legacy random spawning or intensity escalation. Spread
links and work thresholds are explicit scenario rules, not physical predictions.
"""
import argparse
from collections import Counter
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import networkx as nx
from jsonschema import Draft202012Validator, FormatChecker
from sheffield_sim import SheffieldSim, effectiveness_score_breakdown, load_sheffield_graph
from state_schema import DispatchCommand, HERO_CAMERAS, is_unit_available, make_incident

ROOT = Path(__file__).resolve().parent
DEFAULT_SCENARIO = ROOT / "assets/scenarios/sheffield_reports_demo/scenario.json"


class ReportDrivenWorld(SheffieldSim):
    def __init__(self, scenario_path=DEFAULT_SCENARIO, graph=None, follow_up_path=None):
        self.scenario_path = Path(scenario_path)
        self.scenario = json.loads(self.scenario_path.read_text(encoding="utf-8"))
        schema_path = (self.scenario_path.parent / self.scenario["schema"]).resolve()
        schema = json.loads(schema_path.read_text(encoding="utf-8-sig"))
        Draft202012Validator.check_schema(schema)
        self.validator = Draft202012Validator(schema, format_checker=FormatChecker())
        self.base_time = datetime.fromisoformat(self.scenario["base_time"].replace("Z", "+00:00"))
        if self.base_time.tzinfo is None or self.scenario["tick_seconds"] != 60:
            raise ValueError("Scenario needs a timezone-aware base and 60-second ticks")
        for policy in self.scenario["nodes"].values():
            counts = policy["required_units"]
            if not counts or any(k not in ("FIRE", "POLICE", "AMBULANCE") or type(v) is not int or v < 1 for k,v in counts.items()):
                raise ValueError("Invalid required unit counts")
            if not 1 <= policy["containment_work_ticks"] <= policy["resolution_work_ticks"]:
                raise ValueError("Invalid response work thresholds")
        rules = self.scenario["spread_rules"]
        if len({r["rule_id"] for r in rules}) != len(rules):
            raise ValueError("Duplicate spread rule IDs")
        for rule in rules:
            if type(rule["delay_ticks"]) is not int or rule["delay_ticks"] < 1 or any(rule[k] not in self.scenario["nodes"] for k in ("source_node", "target_node")):
                raise ValueError("Invalid authored spread link")
        if graph is None:
            graph, _ = load_sheffield_graph(strict=True)
        super().__init__(graph=graph)
        self.reports, self.response_events, self.spread_events = [], [], []
        self.follow_up_reports, self._follow_up_observations = [], {}
        self._follow_up_bundle = json.loads(Path(follow_up_path).read_text()) if follow_up_path else None
        self._received_follow_ups = set()
        self._reports_by_id, self._observations, self._work, self._contained = {}, {}, {}, set()
        self._finished_rules, self._ingested_initial = set(), set()
        self.state["world_mode"] = "AUTHORED_REPORTS"
        self._receive_due_initial()
        self._receive_due_follow_ups()

    def timestamp(self):
        return (self.base_time + timedelta(seconds=60*self.state["tick"])).isoformat().replace("+00:00", "Z")

    def validate_report(self, report):
        self.validator.validate(report)
        if type(report["incident_id"]) is not int:
            raise ValueError("incident_id must be an integer fixture/database ID")
        if datetime.fromisoformat(report["received_at"].replace("Z", "+00:00")).tzinfo is None:
            raise ValueError("Report time requires a timezone")
        if not report["description"].strip():
            raise ValueError("Blank report description")
        return True

    def ingest_report(self, report, node_id, parent_incident_id=None):
        self.validate_report(report)
        report = deepcopy(report)
        if report["incident_id"] in self._reports_by_id:
            raise ValueError("Duplicate incident_id")
        if node_id not in self.scenario["nodes"]:
            raise ValueError("Report needs an authored graph-node binding")
        existing = self.state["active_incidents"].get(node_id)
        if (existing and existing["status"] != "RESOLVED") or any(u["assigned_cam"] == node_id for u in self.state["resource_db"].values()):
            raise ValueError("Node already has an incident or locked resources")
        if parent_incident_id is not None and parent_incident_id not in self._reports_by_id:
            raise ValueError("Unknown parent report")
        received = datetime.fromisoformat(report["received_at"].replace("Z", "+00:00"))
        if received > self.base_time + timedelta(seconds=60*self.state["tick"]):
            raise ValueError("Cannot ingest a future report")
        policy = self.scenario["nodes"][node_id]
        incident = make_incident("CAM_01", policy["hazard"], policy["severity"], self.state["tick"], policy.get("road_blocked", False))
        incident.update(cam_id=node_id, incident_id=report["incident_id"], parent_incident_id=parent_incident_id,
                        location={"lat": policy["lat"], "lon": policy["lon"], **({"graph_node":policy["graph_node"]} if "graph_node" in policy else {})},
                        progression_model="report_driven_v1", source_report=report,
                        video_path=f"assets/incident_media/{report['incident_id']}.mp4")
        if incident["road_blocked"]:
            if node_id not in HERO_CAMERAS:
                raise ValueError("Blocked non-camera nodes need explicit access/closure support")
            incident["staging_location"] = self.select_staging_location(node_id)
            incident["hazard_edges"] = incident["staging_location"]["excluded_edges"]
            self.set_camera_road_closed(node_id)
        self.state["active_incidents"][node_id] = incident
        self.reports.append(report)
        self._reports_by_id[report["incident_id"]] = report
        self._work[report["incident_id"]] = 0
        image = f"assets/incident_media/{report['incident_id']}.jpg"
        self._observations[report["incident_id"]] = {
            "report":deepcopy(report), "observed_at":report["received_at"],
            "graph_context":{"node_id":node_id,"location":deepcopy(incident["location"]),
                             "routing_mode":self.state["routing"]["mode"],"blocked_edges":deepcopy(self.state["blocked_edges"])},
            "media":{"image_path":image,"video_path":incident["video_path"],"status":"MISSING_PREGENERATED_IMAGE"}}
        return deepcopy(report)

    def _receive_due_initial(self):
        for index, entry in enumerate(self.scenario["initial_reports"]):
            if index not in self._ingested_initial and entry["at_tick"] <= self.state["tick"]:
                self.ingest_report(entry["report"], entry["node_id"])
                self._ingested_initial.add(index)

    def ingest_follow_up(self, report, observation_id):
        """Attach another caller observation, preserving canonical world truth."""
        self.validate_report(report)
        if not isinstance(observation_id,str) or not observation_id.strip():
            raise ValueError("A distinct observation_id is required")
        if observation_id in self._follow_up_observations:
            raise ValueError("Duplicate observation_id")
        canonical = self._reports_by_id.get(report["incident_id"])
        if canonical is None:
            raise ValueError("Ingest the main incident before its follow-ups")
        received = datetime.fromisoformat(report["received_at"].replace("Z","+00:00"))
        first = datetime.fromisoformat(canonical["received_at"].replace("Z","+00:00"))
        if received < first or received > self.base_time+timedelta(seconds=60*self.state["tick"]):
            raise ValueError("Follow-up must arrive after the main report and not in the future")
        observation = deepcopy(self._observations[report["incident_id"]])
        observation.update(observation_id=observation_id, report=deepcopy(report), observed_at=report["received_at"],
                           relationship="FOLLOW_UP_SAME_INCIDENT")
        observation["graph_context"].update(as_of=self.timestamp(),blocked_edges=deepcopy(self.state["blocked_edges"]))
        observation["media"]["reference_time"] = canonical["received_at"]
        # The original incident image is a reference, not a newly captured frame.
        observation["media"]["reference_incident_id"] = report["incident_id"]
        self.follow_up_reports.append({"observation_id":observation_id,"report":deepcopy(report)})
        self._follow_up_observations[observation_id] = observation
        return deepcopy(observation)

    def _receive_due_follow_ups(self):
        bundle = self._follow_up_bundle
        if bundle is None:return
        if bundle.get("bundle_type") != "FOLLOW_UP_SAME_INCIDENT":
            raise ValueError("Expected same-incident follow-up bundle")
        reports,links = bundle["reports"],bundle["report_links"]
        main=self._observations.get(bundle["canonical_incident_id"])
        if main and main["graph_context"]["node_id"] != bundle["node_id"]:
            raise ValueError("Follow-up bundle node does not match the main incident")
        if sorted(link["report_index"] for link in links) != list(range(len(reports))) or len({link["observation_id"] for link in links}) != len(links):
            raise ValueError("Each report needs one unique observation link")
        now = self.base_time+timedelta(seconds=60*self.state["tick"])
        for link in links:
            index=link["report_index"]
            report=reports[index]
            self.validate_report(report)
            if report["incident_id"] != bundle["canonical_incident_id"]:
                raise ValueError("Follow-up points to a different incident")
            if index not in self._received_follow_ups and datetime.fromisoformat(report["received_at"].replace("Z","+00:00")) <= now:
                self.ingest_follow_up(report,link["observation_id"])
                self._received_follow_ups.add(index)

    def qwen_follow_up_observation(self, observation_id):
        observation = deepcopy(self._follow_up_observations[observation_id])
        image=ROOT/observation["media"]["image_path"]
        observation["media"]["status"]="LOCAL_IMAGE" if image.is_file() else "MISSING_PREGENERATED_IMAGE"
        return observation

    def qwen_observation(self, incident_id):
        """Whitelist observations; never include spread/evaluation answer keys."""
        observation = deepcopy(self._observations[incident_id])
        image = ROOT/observation["media"]["image_path"]
        observation["media"]["status"] = "LOCAL_IMAGE" if image.is_file() else "MISSING_PREGENERATED_IMAGE"
        return observation

    def validate_allocation(self, cam_id, unit_ids):
        incident = self.state["active_incidents"].get(cam_id)
        if incident is None or incident["status"] == "RESOLVED":
            return False, "Unknown or resolved report node"
        if not isinstance(unit_ids,list) or not unit_ids or any(not isinstance(k,str) for k in unit_ids) or len(set(unit_ids)) != len(unit_ids):
            return False, "Invalid or duplicate units"
        if any(k not in self.state["resource_db"] or not is_unit_available(self.state["resource_db"][k]) for k in unit_ids):
            return False, "Unknown or unavailable resource"
        committed = Counter(u["agency"] for u in self.state["resource_db"].values() if u["assigned_cam"] == cam_id and u["status"] in ("DEPLETED_EN_ROUTE","DEPLETED_ON_SCENE"))
        required = self.scenario["nodes"][cam_id]["required_units"]
        proposed = Counter(self.state["resource_db"][k]["agency"] for k in unit_ids)
        if incident["status"] == "UNITS_DISPATCHED" and not any(committed[a] < count and proposed[a] for a,count in required.items()):
            return False, "No missing capability to reinforce"
        if any(committed[a]+proposed[a] < count for a,count in required.items()):
            return False, "Missing required response capability"
        try:
            for k in unit_ids:self.estimate_depletion_ticks(k,cam_id,incident["severity"])
        except (nx.NetworkXNoPath,nx.NodeNotFound):
            return False, "No unblocked round-trip route"
        return True, "Valid"

    def evaluate_response(self, node_id):
        incident = self.state["active_incidents"][node_id]
        policy = self.scenario["nodes"][node_id]
        counts = Counter(u["agency"] for u in self.state["resource_db"].values() if u["assigned_cam"] == node_id and u["status"] == "DEPLETED_ON_SCENE")
        effective = all(counts[a]>=n for a,n in policy["required_units"].items())
        work = self._work[incident["incident_id"]]
        return {"capability_coverage":effective, "effective_work_ticks":work,
                "containment_success":work>=policy["containment_work_ticks"],
                "resolution_success":work>=policy["resolution_work_ticks"]}

    def _apply_observed_response(self, node_id, incident):
        metrics = self.evaluate_response(node_id)
        if not metrics["capability_coverage"]:
            return
        self._record_response(incident)
        self._work[incident["incident_id"]] += 1
        metrics = self.evaluate_response(node_id)
        before = incident["severity"]
        incident["severity"] = max(1, self.scenario["nodes"][node_id]["severity"]-metrics["effective_work_ticks"])
        if metrics["containment_success"]:self._contained.add(incident["incident_id"])
        if metrics["resolution_success"]:
            incident["status"] = "RESOLVED"
            self.state["effectiveness_metrics"]["incidents_resolved"] += 1
            if incident["road_blocked"]:
                self.set_camera_road_closed(node_id,False)
                incident["road_blocked"] = False
        self.response_events.append({"tick":self.state["tick"], "incident_id":incident["incident_id"],
                                     "success_metrics":metrics, "severity_before":before,
                                     "severity_after":incident["severity"], "world_status":incident["status"]})

    def _spread(self):
        for rule in self.scenario["spread_rules"]:
            if rule["rule_id"] in self._finished_rules:continue
            source = self.state["active_incidents"].get(rule["source_node"])
            if source is None or self.state["tick"] < source["spawn_tick"]+rule["delay_ticks"]:continue
            target = self.state["active_incidents"].get(rule["target_node"])
            if source["incident_id"] in self._contained or source["status"] == "RESOLVED" or source["severity"] < rule["minimum_severity"] :
                self.spread_events.append({"tick":self.state["tick"],"rule_id":rule["rule_id"],"outcome":"PREVENTED"})
                self.state["effectiveness_metrics"]["spillovers_prevented"] += 1
                self._finished_rules.add(rule["rule_id"])
                continue
            if target and target["status"] != "RESOLVED":
                self.spread_events.append({"tick":self.state["tick"],"rule_id":rule["rule_id"],"outcome":"SKIPPED_ALREADY_AFFECTED"})
                self._finished_rules.add(rule["rule_id"])
                continue
            if any(u["assigned_cam"] == rule["target_node"] for u in self.state["resource_db"].values()):
                continue
            report = deepcopy(rule["report_template"])
            report["received_at"] = self.timestamp()
            prefix = int(self.timestamp()[:10].replace("-",""))*1000000
            report["incident_id"] = max([prefix]+[k for k in self._reports_by_id if prefix<=k<prefix+1000000])+1
            self.ingest_report(report,rule["target_node"],source["incident_id"])
            self._finished_rules.add(rule["rule_id"])
            self.state["effectiveness_metrics"]["spillovers_occurred"] += 1
            self.spread_events.append({"tick":self.state["tick"],"rule_id":rule["rule_id"],"outcome":"SPREAD",
                                       "parent_incident_id":source["incident_id"],"incident_id":report["incident_id"],"node_id":rule["target_node"]})

    def tick(self, spawn=False):
        if spawn:raise ValueError("Report-driven world never randomly spawns incidents")
        self.state["tick"] += 1
        m = self.state["effectiveness_metrics"]
        m["resource_busy_ticks"] += sum(not is_unit_available(u) for u in self.state["resource_db"].values())
        for unit in self.state["resource_db"].values():self._advance_unit(unit)
        self._receive_due_initial()
        self._receive_due_follow_ups()
        for node,incident in list(self.state["active_incidents"].items()):
            if incident["status"] == "RESOLVED":continue
            self._apply_observed_response(node,incident)
            if self.state["tick"]-incident["spawn_tick"]>7 and not incident["sla_recorded"]:
                m["sla_missed_count"] += 1;incident["sla_recorded"] = True
        # Responses are applied before spread due at the same tick.
        self._spread()
        active = [i for i in self.state["active_incidents"].values() if i["status"] != "RESOLVED"]
        m["severity_tick_exposure"] += sum(i["severity"] for i in active)
        m["road_closed_ticks"] += sum(i["road_blocked"] for i in active)
        m["unresolved_incidents"] = len(active)
        m["overall_score"] = effectiveness_score_breakdown(m)["overall_score"]
        self.validate_world_invariants()
        return self.state

    def export(self, directory):
        directory = Path(directory);directory.mkdir(parents=True,exist_ok=True)
        for report in self.reports:
            (directory/f"{report['incident_id']}.json").write_text(json.dumps(report,indent=2)+"\n",encoding="utf-8")
        observations = [self.qwen_observation(r["incident_id"]) for r in self.reports]
        (directory/"observations.json").write_text(json.dumps(observations,indent=2)+"\n",encoding="utf-8")
        follow_up_observations=[self.qwen_follow_up_observation(item["observation_id"]) for item in self.follow_up_reports]
        (directory/"follow_up_reports.json").write_text(json.dumps(self.follow_up_reports,indent=2)+"\n",encoding="utf-8")
        (directory/"follow_up_observations.json").write_text(json.dumps(follow_up_observations,indent=2)+"\n",encoding="utf-8")
        result = {"follow_up_reports":len(self.follow_up_reports),"tick":self.state["tick"],"reports":len(self.reports),"spread_events":self.spread_events,
                  "response_events":self.response_events,"score_breakdown":effectiveness_score_breakdown(self.state["effectiveness_metrics"])}
        (directory/"world_events.json").write_text(json.dumps(result,indent=2)+"\n",encoding="utf-8")
        return result


def scripted_response(world):
    """Oracle smoke-test script uses evaluator requirements; never AI benchmark evidence."""
    for node,incident in list(world.state["active_incidents"].items()):
        if incident["status"] in ("RESOLVED","UNITS_DISPATCHED"):continue
        selected=[]
        for agency,count in world.scenario["nodes"][node]["required_units"].items():
            available=[]
            for k,u in world.state["resource_db"].items():
                if u["agency"]==agency and is_unit_available(u):
                    try:available.append((world.estimate_depletion_ticks(k,node,incident["severity"])["total_depletion_ticks_remaining"],k))
                    except (nx.NetworkXNoPath,nx.NodeNotFound):pass
            selected.extend(k for _,k in sorted(available)[:count])
        if selected:
            world.apply_command(DispatchCommand(node,tuple(selected),world.state["tick"],"AGENTS"))


if __name__ == "__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario",type=Path,default=DEFAULT_SCENARIO)
    parser.add_argument("--follow-up-bundle",type=Path,help="Scheduled additional caller reports; they do not create new incidents")
    parser.add_argument("--ticks",type=int,default=12)
    parser.add_argument("--scripted-response",action="store_true")
    parser.add_argument("--output",type=Path,default=ROOT/"demo_output/reports")
    args=parser.parse_args()
    if args.ticks<0:parser.error("--ticks must be nonnegative")
    world=ReportDrivenWorld(args.scenario,follow_up_path=args.follow_up_bundle)
    for _ in range(args.ticks):
        if args.scripted_response:scripted_response(world)
        world.tick()
    print(json.dumps(world.export(args.output),indent=2))
