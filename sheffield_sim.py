"""Track 1: directed graph world, minute-tick resource depletion and scorecard."""
import argparse
import json
import math
import random
from pathlib import Path
import networkx as nx
from knowledge_base import NODE_HISTORICAL_PROBABILITIES
from state_schema import DispatchCommand, EMERGENCY_HUBS, HERO_CAMERAS, get_initial_city_state, is_unit_available, make_incident, required_agencies_for_incident


def _distance(a, b):
    lat1, lat2 = math.radians(a["lat"]), math.radians(b["lat"])
    dlat = lat2 - lat1
    dlon = math.radians(b["lon"] - a["lon"])
    return 6371000 * 2 * math.asin(min(1, math.sqrt(math.sin(dlat/2)**2 + math.cos(lat1)*math.cos(lat2)*math.sin(dlon/2)**2)))


def synthetic_graph():
    """Connected DEMO graph; edges are invented, never labelled OSM streets."""
    graph = nx.MultiDiGraph(crs="EPSG:4326", source="synthetic")
    locations = {**EMERGENCY_HUBS, **HERO_CAMERAS}
    for key, point in locations.items():
        graph.add_node(key, x=point["lon"], y=point["lat"])
    for a, start in locations.items():
        for b, end in locations.items():
            if a != b:
                distance = _distance(start, end) * 1.3
                graph.add_edge(a, b, length=distance, travel_time=distance / (30/3.6))
    return graph


def load_sheffield_graph(download=False, cache="assets/sheffield.graphml"):
    """Offline by default. Explicit OSM download has a bounded HTTP timeout."""
    if not download and not Path(cache).is_file():
        return synthetic_graph(), "synthetic_offline"
    try:
        import osmnx as ox
        if Path(cache).is_file():
            graph = ox.load_graphml(cache)
        else:
            ox.settings.requests_timeout = 8
            graph = ox.graph_from_place("Sheffield, South Yorkshire, England, UK", network_type="drive")
            Path(cache).parent.mkdir(parents=True, exist_ok=True)
            ox.save_graphml(graph, cache)
        graph = ox.routing.add_edge_speeds(graph)
        graph = ox.routing.add_edge_travel_times(graph)
        return graph, "osmnx"
    except (ImportError, OSError, ValueError, nx.NetworkXException) as error:
        graph = synthetic_graph()
        graph.graph["fallback_reason"] = str(error)
        return graph, "synthetic_fallback"
    except Exception as error:
        # OSMnx HTTP exceptions differ between requests versions; remain usable offline.
        graph = synthetic_graph()
        graph.graph["fallback_reason"] = type(error).__name__
        return graph, "synthetic_fallback"


class SheffieldSim:
    def __init__(self, state=None, graph=None, seed=42, demo=False, download=False):
        self.state = state if state is not None else get_initial_city_state()
        self.rng = random.Random(seed)
        self.demo = demo
        if graph is None:
            self.graph, mode = load_sheffield_graph(download=download)
        else:
            self.graph, mode = graph, graph.graph.get("source", "supplied_graph")
        self.state["routing"] = {"mode": mode, "tick_seconds": 60,
                                 "fallback_reason": self.graph.graph.get("fallback_reason")}

    def _nearest(self, point):
        return min(self.graph.nodes, key=lambda n: (self.graph.nodes[n]["x"]-point["lon"])**2
                   + (self.graph.nodes[n]["y"]-point["lat"])**2)

    def _route(self, start, end):
        graph = self.graph.copy()
        for edge in self.state["blocked_edges"]:
            if len(edge) == 3 and graph.has_edge(*edge):
                graph.remove_edge(*edge)
        a, b = self._nearest(start), self._nearest(end)
        path = nx.shortest_path(graph, a, b, weight="travel_time")
        seconds = 0.0
        coordinates = [[graph.nodes[n]["x"], graph.nodes[n]["y"]] for n in path]
        cumulative_seconds = [0.0]
        for u, v in zip(path, path[1:]):
            seconds += min(float(data["travel_time"]) for data in graph[u][v].values())
            cumulative_seconds.append(seconds)
        return {"ticks": max(1, math.ceil(seconds / 60)), "seconds": seconds,
                "path": coordinates, "cumulative_seconds": cumulative_seconds, "destination": {"lat": end["lat"], "lon": end["lon"]}}

    def estimate_depletion_ticks(self, unit_id, cam_id, severity):
        unit = self.state["resource_db"][unit_id]
        camera, home = HERO_CAMERAS[cam_id], EMERGENCY_HUBS[unit["home_hub"]]
        outward = self._route(unit, camera)
        # Trauma patients go to Northern General; Hallamshire is only a demo base.
        hospital = EMERGENCY_HUBS["NHS_NORTHERN_GEN"] if unit["agency"] == "AMBULANCE" else home
        returning = self._route(camera, hospital)
        final = self._route(hospital, home) if hospital is not home and hospital != home else None
        cooldown = 2 if unit["agency"] in ("FIRE", "AMBULANCE") else 0
        scene = int(severity) + 2
        return {"travel_to_scene_ticks": outward["ticks"], "on_scene_ticks": scene,
                "return_to_base_ticks": returning["ticks"] + (final["ticks"] if final else 0),
                "cooldown_ticks_remaining": cooldown,
                "total_depletion_ticks_remaining": outward["ticks"] + scene + returning["ticks"] + cooldown + (final["ticks"] if final else 0),
                "outward_route": outward, "return_route": returning, "final_route": final}

    def resource_estimates(self, cam_id):
        severity = self.state["active_incidents"][cam_id]["severity"]
        estimates = {}
        for key, unit in self.state["resource_db"].items():
            if is_unit_available(unit):
                try:
                    estimates[key] = self.estimate_depletion_ticks(key, cam_id, severity)
                except (nx.NetworkXNoPath, nx.NodeNotFound):
                    estimates[key] = {"unreachable": True}
        return estimates

    def validate_allocation(self, cam_id, unit_ids):
        if cam_id not in self.state["active_incidents"]:
            return False, "Unknown incident"
        incident = self.state["active_incidents"][cam_id]
        if incident["status"] in ("UNITS_DISPATCHED", "RESOLVED"):
            return False, "Incident already dispatched or resolved"
        if not isinstance(unit_ids, list) or any(not isinstance(u, str) for u in unit_ids):
            return False, "Unit allocation must be a list of IDs"
        if len(unit_ids) != len(set(unit_ids)) or not unit_ids:
            return False, "Empty or duplicate allocation"
        if any(u not in self.state["resource_db"] or not is_unit_available(self.state["resource_db"][u]) for u in unit_ids):
            return False, "Unknown or unavailable unit"
        units = [self.state["resource_db"][u] for u in unit_ids]
        agencies = {u["agency"] for u in units}
        if not set(required_agencies_for_incident(incident)).issubset(agencies):
            return False, "Missing required agency"
        if (incident["road_blocked"] or (incident["severity"] >= 6 and incident["hazard"] in ("flood", "trauma"))) and "POLICE" not in agencies:
            return False, "Police cordon required"
        if incident["severity"] >= 7 and incident["hazard"] in ("fire", "flood"):
            pumps = sum(u["agency"] == "FIRE" for u in units)
            if pumps < 2 and not (pumps >= 1 and "POLICE" in agencies):
                return False, "High severity rescue requires pumps or pump plus cordon"
        flood_risk = max(p["spawn_probability"] * p["hazard_weights"]["flood"] for p in self.state["node_probabilities"].values())
        if flood_risk >= 0.05:
            reserve = sum(u["agency"] == "FIRE" and is_unit_available(u) and key not in unit_ids
                          for key, u in self.state["resource_db"].items())
            if reserve < 1:
                return False, "Fire reserve minimum violated"
        try:
            for key in unit_ids:
                self.estimate_depletion_ticks(key, cam_id, incident["severity"])
        except (nx.NetworkXNoPath, nx.NodeNotFound):
            return False, "No unblocked round-trip route"
        return True, "Valid"

    @staticmethod
    def _sync_total(unit):
        unit["total_depletion_ticks_remaining"] = sum(unit[k] for k in
            ("travel_to_scene_ticks", "on_scene_ticks", "return_to_base_ticks", "cooldown_ticks_remaining"))

    def apply_command(self, command: DispatchCommand) -> dict:
        """Single dispatch boundary for UI, agents and replay; rejected work is inert."""
        if not isinstance(command, DispatchCommand):
            raise TypeError("Expected DispatchCommand")
        accepted, reason = False, "Stale decision tick"
        if command.expected_tick == self.state["tick"]:
            accepted, reason = self.validate_allocation(command.cam_id, list(command.unit_ids))
            if accepted:
                self._dispatch(command.cam_id, list(command.unit_ids))
        receipt = {"tick": self.state["tick"], "expected_tick": command.expected_tick,
                   "controller": command.controller, "cam_id": command.cam_id,
                   "unit_ids": list(command.unit_ids), "accepted": accepted, "reason": reason}
        self.state.setdefault("command_log", []).append(receipt)
        return dict(receipt, unit_ids=list(receipt["unit_ids"]))

    def dispatch(self, cam_id, unit_ids, *, controller="AGENTS", expected_tick=None):
        """Compatibility adapter; all callers use the same command validation."""
        receipt = self.apply_command(DispatchCommand(
            cam_id, tuple(unit_ids), self.state["tick"] if expected_tick is None else expected_tick, controller))
        if not receipt["accepted"]:
            raise ValueError(receipt["reason"])
        return receipt

    def _dispatch(self, cam_id, unit_ids):
        valid, reason = self.validate_allocation(cam_id, unit_ids)
        if not valid:
            raise ValueError(reason)
        incident = self.state["active_incidents"][cam_id]
        # Compute every plan before mutating any unit (atomic allocation).
        plans = {key: self.estimate_depletion_ticks(key, cam_id, incident["severity"]) for key in unit_ids}
        for key, plan in plans.items():
            unit = self.state["resource_db"][key]
            for field in ("travel_to_scene_ticks", "on_scene_ticks", "return_to_base_ticks", "cooldown_ticks_remaining", "total_depletion_ticks_remaining"):
                unit[field] = plan[field]
            phases = [self._phase("DEPLETED_EN_ROUTE", "travel_to_scene_ticks", plan["outward_route"]),
                      {"status": "DEPLETED_ON_SCENE", "timer": "on_scene_ticks", "remaining": plan["on_scene_ticks"]},
                      self._phase("DEPLETED_RETURNING", "return_to_base_ticks", plan["return_route"])]
            if plan["cooldown_ticks_remaining"]:
                phases.append({"status": "DEPLETED_COOLDOWN", "timer": "cooldown_ticks_remaining", "remaining": plan["cooldown_ticks_remaining"]})
            if plan["final_route"]:
                phases.append(self._phase("DEPLETED_RETURNING", "return_to_base_ticks", plan["final_route"]))
            unit.update(status="DEPLETED_EN_ROUTE", assigned_cam=cam_id, phases=phases)
        incident.update(status="UNITS_DISPATCHED", dispatched_units=list(unit_ids))

    @staticmethod
    def _phase(status, timer, route):
        return {"status": status, "timer": timer, "remaining": route["ticks"],
                "duration": route["ticks"], "route": route}

    def _advance_unit(self, unit):
        if is_unit_available(unit):
            return
        phase = unit["phases"][0]
        incident = self.state["active_incidents"].get(unit["assigned_cam"])
        if phase["status"] == "DEPLETED_ON_SCENE" and phase["remaining"] == 1 and incident and (incident["severity"] >= 4 if unit["agency"] == "POLICE" else incident["severity"] > 1):
            # Hazard escalation/cordon clearance can extend the nominal round-trip estimate.
            phase["remaining"] += 1
            unit["on_scene_ticks"] += 1
        phase["remaining"] -= 1
        unit[phase["timer"]] -= 1
        if "route" in phase:
            route = phase["route"]
            elapsed = min(route["seconds"], (phase["duration"]-phase["remaining"])*60)
            times = route["cumulative_seconds"]
            index = next((i for i in range(len(times)-1) if times[i+1] >= elapsed), len(times)-1)
            if index < len(times)-1:
                span = times[index+1]-times[index]
                fraction = (elapsed-times[index])/span if span else 1.0
                start, end = route["path"][index:index+2]
                unit["lon"] = start[0] + fraction*(end[0]-start[0])
                unit["lat"] = start[1] + fraction*(end[1]-start[1])
            else:
                unit["lon"], unit["lat"] = route["path"][-1]
            if phase["remaining"] == 0:
                unit.update(route["destination"])
        if phase["remaining"] == 0:
            completed = unit["phases"].pop(0)
            if completed["status"] == "DEPLETED_EN_ROUTE" and incident and incident["response_ticks"] is None:
                response = self.state["tick"] - incident["spawn_tick"]
                incident["response_ticks"] = response
                metrics = self.state["effectiveness_metrics"]
                count = metrics.get("response_sample_count", 0)
                metrics["avg_response_ticks"] = (metrics["avg_response_ticks"] * count + response)/(count+1)
                metrics["response_sample_count"] = count + 1
                if not incident["sla_recorded"]:
                    metrics["sla_met_count" if response <= 7 else "sla_missed_count"] += 1
                    incident["sla_recorded"] = True
            unit["status"] = unit["phases"][0]["status"] if unit["phases"] else "AVAILABLE"
            if not unit["phases"]:
                home = EMERGENCY_HUBS[unit["home_hub"]]
                unit.update(lat=home["lat"], lon=home["lon"], assigned_cam=None)
        self._sync_total(unit)

    def tick(self, spawn=True):
        self.state["tick"] += 1
        for unit in self.state["resource_db"].values():
            self._advance_unit(unit)
        for cam_id, incident in self.state["active_incidents"].items():
            if incident["status"] == "RESOLVED":
                continue
            if self.state["tick"] - incident["spawn_tick"] > 7 and not incident["sla_recorded"]:
                self.state["effectiveness_metrics"]["sla_missed_count"] += 1
                incident["sla_recorded"] = True
            on_scene = any(u["assigned_cam"] == cam_id and u["status"] == "DEPLETED_ON_SCENE" for u in self.state["resource_db"].values())
            incident["severity"] = max(1, incident["severity"] - 1) if on_scene else min(10, incident["severity"] + (self.state["tick"] % 3 == 0))
            if incident["severity"] == 1 and on_scene:
                incident["status"] = "RESOLVED"
                metrics = self.state["effectiveness_metrics"]
                metrics["incidents_resolved"] += 1
                if not incident["spillover_recorded"]:
                    metrics["spillovers_prevented"] += 1
            elif incident["severity"] >= 9 and not incident["spillover_recorded"]:
                incident["spillover_recorded"] = True
                self.state["effectiveness_metrics"]["spillovers_occurred"] += 1
        # Deterministic synthetic weather multiplier; priors remain explicitly uncalibrated.
        for cam, baseline in NODE_HISTORICAL_PROBABILITIES.items():
            self.state["node_probabilities"][cam]["spawn_probability"] = min(1.0, baseline["spawn_probability"] * (1 + (self.state["tick"] % 10)/20))
        if spawn:
            self._spawn()
        metrics = self.state["effectiveness_metrics"]
        metrics["overall_score"] = max(0.0, min(100.0, 100 - 5*metrics["sla_missed_count"] - 3*metrics["spillovers_occurred"] + metrics["incidents_resolved"]))
        return self.state

    def _spawn(self):
        for index, cam in enumerate(HERO_CAMERAS):
            existing = self.state["active_incidents"].get(cam)
            if existing and existing["status"] != "RESOLVED":
                continue
            if any(u["assigned_cam"] == cam for u in self.state["resource_db"].values()):
                continue
            probability = self.state["node_probabilities"][cam]
            trigger = self.state["tick"] == 1 + index*4 if self.demo else self.rng.random() < probability["spawn_probability"]
            if trigger:
                weights = probability["hazard_weights"]
                hazard = self.rng.choices(list(weights), weights=list(weights.values()))[0]
                self.state["active_incidents"][cam] = make_incident(cam, hazard, self.rng.randint(4, 8), self.state["tick"], self.rng.choice([True, False]))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticks", type=int, default=5)
    parser.add_argument("--download-osm", action="store_true")
    args = parser.parse_args()
    sim = SheffieldSim(seed=42, demo=True, download=args.download_osm)
    for _ in range(args.ticks):
        sim.tick()
    print(json.dumps(sim.state, indent=2))
