"""Opt-in authored-report graph world using the backend's concrete fleet.

The world owns movement, incident outcomes and release. SQLite owns reservations
and crew identities. Model evaluation is an assessment, never a release command.
"""
from collections import Counter
import base64
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import math
from pathlib import Path
from PIL import Image

from report_world import ReportDrivenWorld
from state_schema import EMERGENCY_HUBS, is_unit_available
from . import timing
from .db import Database, time_scale
from .resources import crew_for
from .schemas import Incident, Triage
from .config import ROOT


class WorldConflict(ValueError):
    pass


class GraphReportWorld(ReportDrivenWorld):
    def __init__(self, db, scenario_path, graph=None):
        super().__init__(scenario_path, graph=graph)
        self.db = db
        self.inflight = 0
        self.pending_reports = {}
        self.case_nodes = {}
        self.submitted = set()
        self.responses = {}
        self.last_released = []
        self.hubs = deepcopy(EMERGENCY_HUBS)
        fleet = {}
        for row in db._conn.execute("SELECT * FROM units WHERE status='available'"):
            hub = "DB:" + row["agency"] + ":" + row["station"]
            self.hubs[hub] = {"name": row["station"], "lat": row["lat"], "lon": row["lon"]}
            fleet[row["unit_id"]] = {
                "agency": row["agency"].upper(), "resource_type": row["resource_type"],
                "home_hub": hub, "lat": row["lat"], "lon": row["lon"],
                "status": "AVAILABLE", "assigned_cam": None,
                "travel_to_scene_ticks": 0, "on_scene_ticks": 0,
                "return_to_base_ticks": 0, "cooldown_ticks_remaining": 0,
                "total_depletion_ticks_remaining": 0,
            }
        self.state["resource_db"] = fleet
        self._deployment = {}
        self.cooldown_minutes = dict(self.scenario.get("cooldown_minutes", {}))
        self.handover_minutes = self.scenario.get("hospital_handover_minutes", timing.HOSPITAL_HANDOVER_MIN)
        for minutes in [self.handover_minutes, *self.cooldown_minutes.values()]:
            if type(minutes) is not int or minutes < 0:
                raise ValueError("Cooldown and handover durations must be nonnegative integer minutes")
        basic_types = {"FIRE": "fire_engine", "POLICE": "patrol_car", "AMBULANCE": "emergency_ambulance"}
        for policy in self.scenario["nodes"].values():
            policy.setdefault("required_resource_types", {basic_types[a]: n for a,n in policy["required_units"].items()})
            required = policy["required_resource_types"]
            if not required or any(t not in db.resource_types() or type(n) is not int or n < 1 for t,n in required.items()):
                raise ValueError("Authored resource thresholds must name known types with positive counts")
            if "deployment_scene_minutes" in policy and (type(policy["deployment_scene_minutes"]) is not int or policy["deployment_scene_minutes"] < policy["resolution_work_ticks"]):
                raise ValueError("Deployment scene duration must cover resolution work")

    def bind_case(self, report):
        node = self.pending_reports.get(id(report))
        if node is None:
            raise WorldConflict("World mode accepts authored reports through /world/dispatch-due")
        self.case_nodes[report.incident_id] = node

    def intake_image(self, report):
        """Only an explicitly approved scenario asset may be sent to the VLM."""
        node = self.pending_reports.get(id(report))
        if node is None:
            return None
        source_id = self.state["active_incidents"][node]["incident_id"]
        asset = self.scenario.get("approved_media", {}).get(str(source_id), {})
        if asset.get("approved") is not True:
            return None
        path = (ROOT / asset["image_path"]).resolve()
        if not path.is_relative_to(ROOT.resolve()):
            raise WorldConflict("Approved media must stay inside the checkout")
        with Image.open(path) as image:
            mime = {"JPEG":"image/jpeg", "PNG":"image/png"}.get(image.format)
            if mime is None:
                raise WorldConflict("Approved intake image must be JPEG or PNG")
            image.verify()
        return "data:" + mime + ";base64," + base64.b64encode(path.read_bytes()).decode("ascii")

    def validate_allocation(self, cam_id, unit_ids):
        incident = self.state["active_incidents"].get(cam_id)
        if incident is None or incident["status"] == "RESOLVED":
            return False, "Unknown or resolved world incident"
        if not unit_ids or len(set(unit_ids)) != len(unit_ids):
            return False, "Empty or duplicate units"
        if any(k not in self.state["resource_db"] or not is_unit_available(self.state["resource_db"][k]) for k in unit_ids):
            return False, "Resource is unavailable in the graph world"
        try:
            for k in unit_ids:
                self.estimate_depletion_ticks(k, cam_id, incident["severity"])
        except Exception as error:
            return False, str(error)
        return True, "Valid concrete backend allocation"

    def evaluate_response(self, node_id):
        metrics = super().evaluate_response(node_id)
        counts = Counter(u["resource_type"] for u in self.state["resource_db"].values()
                         if u["assigned_cam"] == node_id and u["status"] == "DEPLETED_ON_SCENE")
        required = self.scenario["nodes"][node_id]["required_resource_types"]
        metrics["on_scene_resource_types"] = dict(counts)
        metrics["capability_coverage"] = all(counts[t] >= n for t,n in required.items())
        metrics["response_state"] = "SUFFICIENT" if metrics["capability_coverage"] else "PARTIAL" if any(counts[t] for t in required) else "NONE"
        return metrics

    def estimate_depletion_ticks(self, unit_id, cam_id, severity):
        unit = self.state["resource_db"][unit_id]
        incident = self.state["active_incidents"][cam_id]
        destination = incident.get("staging_location", incident["location"])
        excluded = incident.get("hazard_edges", ())
        home = self.hubs[unit["home_hub"]]
        outward = self._route(unit, destination, excluded)
        deployment = self._deployment.get(unit_id)
        conveys = deployment is not None and deployment["conveys"]
        hospital = self.hubs["NHS_NORTHERN_GEN"] if conveys else home
        returning = self._route(destination, hospital, excluded)
        final = self._route(hospital, home, excluded) if hospital != home else None
        scene = max(self.scenario["nodes"][cam_id]["resolution_work_ticks"],
                    self.scenario["nodes"][cam_id].get("deployment_scene_minutes",
                        math.ceil(deployment["scene_minutes"]) if deployment else severity + 2))
        cooldown = self.cooldown_minutes.get(unit["resource_type"], timing.COOLDOWN_MIN.get(unit["resource_type"], (10,""))[0])
        if conveys:
            cooldown += self.handover_minutes
        return {"travel_to_scene_ticks": outward["ticks"], "on_scene_ticks": scene,
                "return_to_base_ticks": returning["ticks"] + (final["ticks"] if final else 0),
                "cooldown_ticks_remaining": cooldown,
                "total_depletion_ticks_remaining": outward["ticks"] + scene + returning["ticks"] + cooldown + (final["ticks"] if final else 0),
                "outward_route": outward, "return_route": returning, "final_route": final}

    def commit_response(self, case, response):
        node = self.case_nodes.get(case.incident_id)
        if node is None:
            raise WorldConflict("Backend case has no authored graph-node binding")
        self._deployment = {}
        conveying = 0
        try:
            for allocation in response.allocations:
                if allocation.preempted_from is not None:
                    raise WorldConflict("World lifecycle locks prohibit reassignment of busy vehicles")
                record = self.db.unit(allocation.unit_id)
                if record is None or (record["resource_type"],record["agency"],record["station"]) != (allocation.resource_type,allocation.agency.value,allocation.station):
                    raise WorldConflict("Allocation identity/type/station does not match the resource database")
                if Counter(m.speciality for m in allocation.crew) != Counter(crew_for(allocation.resource_type)):
                    raise WorldConflict("Incomplete crew in concrete allocation")
                for member in allocation.crew:
                    person = self.db._conn.execute("SELECT speciality,station,on_shift FROM personnel WHERE person_id=?",(member.person_id,)).fetchone()
                    if person is None or not person["on_shift"] or (person["speciality"],person["station"]) != (member.speciality,member.station):
                        raise WorldConflict("Crew identity/speciality does not match the resource database")
                scene = next((s["minutes"] for s in allocation.timeline if s["stage"] == "on_scene"), timing.DEFAULT_ON_SCENE_MIN)
                conveys = allocation.resource_type == "emergency_ambulance" and conveying < case.casualties_reported
                conveying += int(conveys)
                self._deployment[allocation.unit_id] = {"scene_minutes": scene, "conveys": conveys}
                plan = self.estimate_depletion_ticks(allocation.unit_id, node, 1)
                allocation.eta_min = plan["travel_to_scene_ticks"]
                allocation.timeline = [
                    {"stage":"en_route", "minutes":plan["travel_to_scene_ticks"]},
                    {"stage":"on_scene", "minutes":plan["on_scene_ticks"]},
                    {"stage":"returning", "minutes":plan["return_to_base_ticks"]},
                    {"stage":"cooldown", "minutes":plan["cooldown_ticks_remaining"]},
                ]
                allocation.back_in_service_min = plan["total_depletion_ticks_remaining"]
            if response.allocations:
                self.dispatch(node, [a.unit_id for a in response.allocations])
        finally:
            self._deployment = {}
        self.responses[case.incident_id] = response

    def tick(self, spawn=False):
        if self.inflight:
            raise WorldConflict("Finish model decision windows before advancing the clock")
        result = super().tick(spawn)
        for case_id,response in self.responses.items():
            node = self.case_nodes[case_id]
            metrics = self.evaluate_response(node)
            if response.evaluation is not None:
                response.evaluation.metrics["world_outcome"] = {"tick":self.state["tick"], **metrics,
                    "status": self.state["active_incidents"][node]["status"]}
                self.db.update_response(response)
        self.last_released = self.db.release_due()
        return result

    async def dispatch_due(self, orchestrator, on_event=None):
        results = []
        for report in list(self.reports):
            source_id = report["incident_id"]
            if source_id in self.submitted:
                continue
            node = self._observations[source_id]["graph_context"]["node_id"]
            incident = Incident.model_validate(deepcopy(report))
            self.pending_reports[id(incident)] = node
            try:
                async def emit(name, payload):
                    if on_event:
                        await on_event(incident.incident_id, name, payload)
                result = await orchestrator.handle(incident, on_event=emit)
                self.submitted.add(source_id)
                results.append(result)
            finally:
                self.pending_reports.pop(id(incident), None)
        return results

    def snapshot(self):
        return {"tick":self.state["tick"], "observed_at":self.timestamp(), "routing":self.state["routing"],
                "incidents":self.state["active_incidents"], "units":self.state["resource_db"],
                "response_events":self.response_events, "spread_events":self.spread_events,
                "case_nodes":self.case_nodes, "inflight":self.inflight}


class WorldDatabase(Database):
    """Reservations are transactional; only actual world completion releases them."""
    graph_world = None

    def record_response(self, incident, response):
        world = self.graph_world
        if world is None:
            return super().record_response(incident, response)
        before = deepcopy(world.state)
        old_responses = dict(world.responses)
        try:
            world.commit_response(incident, response)
            super().record_response(incident, response)
        except Exception:
            world.state = before
            world.responses = old_responses
            raise

    def available_units(self, resource_type, lat, lon):
        units = super().available_units(resource_type, lat, lon)
        world = self.graph_world
        if world is None:
            return units
        result = []
        destination = {"lat":lat,"lon":lon}
        for incident in world.state["active_incidents"].values():
            if (lat,lon) == (incident["location"]["lat"],incident["location"]["lon"]):
                destination = incident.get("staging_location", destination)
                break
        for unit in units:
            if unit["unit_id"] not in world.state["resource_db"] or not is_unit_available(world.state["resource_db"][unit["unit_id"]]):
                continue
            try:
                unit["eta_min"] = world._route(unit, destination)["ticks"]
            except Exception:
                continue
            result.append(unit)
        return sorted(result, key=lambda u:u["eta_min"])

    def committed_units(self, resource_type):
        # World lockouts cannot be bypassed by the backend's preemption policy.
        return [] if self.graph_world is not None else super().committed_units(resource_type)

    def recent_incidents(self, within_minutes, statuses=("pending","dispatched","backlog","merged")):
        world = self.graph_world
        if world is None:
            return super().recent_incidents(within_minutes,statuses)
        since = (datetime.fromisoformat(world.timestamp().replace("Z","+00:00")) - timedelta(minutes=within_minutes)).isoformat()
        marks = ",".join("?" for _ in statuses)
        rows = self._conn.execute(
            f"SELECT incident_id,status,duplicate_of,report_count,incident_json,triage_json FROM incidents WHERE status IN ({marks}) AND received_at>=? ORDER BY received_at",
            (*statuses,since)).fetchall()
        return [{"incident_id":r["incident_id"],"status":r["status"],"duplicate_of":r["duplicate_of"],
                 "report_count":r["report_count"],"incident":Incident.model_validate_json(r["incident_json"]),
                 "triage":Triage.model_validate_json(r["triage_json"])} for r in rows]

    def fleet(self):
        if self.graph_world is None:
            return super().fleet()
        rows = self._conn.execute("SELECT a.unit_id,a.incident_id,a.expected_release_at,u.callsign,u.resource_type,u.agency,u.station FROM allocations a JOIN units u ON u.unit_id=a.unit_id WHERE a.released_at IS NULL").fetchall()
        out = []
        for row in rows:
            unit = self.graph_world.state["resource_db"][row["unit_id"]]
            blocked = unit.get("route_blocked",False)
            out.append({**dict(row), "stage":unit["status"].removeprefix("DEPLETED_").lower(),
                        "detail":"Observed graph-world lifecycle", "stage_left_min":None if blocked else unit["phases"][0]["remaining"],
                        "lat":unit["lat"], "lon":unit["lon"], "route":unit["phases"][0].get("route",{}).get("path",[]),
                        "back_in_service_min":None if blocked else unit["total_depletion_ticks_remaining"]})
        return out

    def release_due(self):
        world = self.graph_world
        if world is None:
            return super().release_due()
        released = []
        with self.tx() as connection:
            active = connection.execute("SELECT unit_id FROM allocations WHERE released_at IS NULL").fetchall()
            for row in active:
                unit = world.state["resource_db"].get(row["unit_id"])
                if unit and is_unit_available(unit):
                    released += self._release(connection, "a.unit_id = ?", (row["unit_id"],))
                elif unit:
                    # Forecast only; elapsed wall time never unlocks a vehicle.
                    forecast = datetime.now(timezone.utc) + timedelta(minutes=unit["total_depletion_ticks_remaining"]/time_scale())
                    connection.execute("UPDATE allocations SET expected_release_at=? WHERE unit_id=? AND released_at IS NULL",
                                       (forecast.isoformat(), row["unit_id"]))
            for case_id in {u["incident_id"] for u in released}:
                self._close_if_clear(connection, case_id)
        return released

    def close_incident(self, incident_id):
        if self.graph_world is not None:
            raise WorldConflict("World incidents close after resolution and vehicle turnaround")
        return super().close_incident(incident_id)

    def release_unit(self, unit_id):
        if self.graph_world is not None:
            raise WorldConflict("World-controlled units must complete their turnaround")
        return super().release_unit(unit_id)
