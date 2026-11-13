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


def load_sheffield_graph(download=False, cache="assets/sheffield.graphml", strict=False):
    """Offline by default. Explicit OSM download has a bounded HTTP timeout."""
    if not download and not Path(cache).is_file():
        if strict:
            raise FileNotFoundError("No cached OSM graph; run with --download-osm")
        return synthetic_graph(), "synthetic_offline"
    try:
        import osmnx as ox
        cached = Path(cache).is_file()
        if cached:
            graph = ox.load_graphml(cache)
        else:
            ox.settings.requests_timeout = 60
            # Bounded study area covers all six bases and five hero cameras.
            # Avoid downloading the much larger Sheffield administrative district.
            graph = ox.graph_from_bbox((-1.54, 53.36, -1.39, 53.43), network_type="drive")
        needs_times = any("travel_time" not in data for _, _, data in graph.edges(data=True))
        if needs_times:
            graph = ox.routing.add_edge_speeds(graph)
            graph = ox.routing.add_edge_travel_times(graph)
        graph.graph["source"] = "osmnx"
        graph.graph["attribution"] = "OpenStreetMap contributors, ODbL"
        Path(cache).parent.mkdir(parents=True, exist_ok=True)
        if not cached or needs_times:
            temporary = Path(cache).with_suffix(".tmp.graphml")
            ox.save_graphml(graph, temporary)
            temporary.replace(cache)
        return graph, "osmnx"
    except (ImportError, OSError, ValueError, nx.NetworkXException) as error:
        if strict:
            raise RuntimeError("Could not load a real OSM graph") from error
        graph = synthetic_graph()
        graph.graph["fallback_reason"] = str(error)
        return graph, "synthetic_fallback"
    except Exception as error:
        if strict:
            raise RuntimeError("OSM download/cache processing failed") from error
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
        # Great-circle distance avoids comparing longitude/latitude degrees as metres.
        for mapping in getattr(self, "location_mappings", {}).values():
            if (point["lat"], point["lon"]) == (mapping["lat"], mapping["lon"]):
                return mapping["access_node"]
        return min(self.graph.nodes, key=lambda n: _distance(point,
            {"lat": self.graph.nodes[n]["y"], "lon": self.graph.nodes[n]["x"]}))

    def build_location_mappings(self):
        """Map team-supplied coordinates to streets in a metre-based projection.

        Nearest street is an inferred association, not verified camera direction
        or a station driveway. Distances above 150m require manual review.
        Access nodes remain fixed when a street closes: do not snap past a closure.
        """
        import osmnx as ox
        from pyproj import Transformer
        projected = ox.project_graph(self.graph)
        transform = Transformer.from_crs("EPSG:4326", projected.graph["crs"], always_xy=True)
        mappings = {}
        for key, point in {**EMERGENCY_HUBS, **HERO_CAMERAS}.items():
            x, y = transform.transform(point["lon"], point["lat"])
            edge, edge_distance = ox.distance.nearest_edges(projected, x, y, return_dist=True)
            node, node_distance = ox.distance.nearest_nodes(projected, x, y, return_dist=True)
            edge = tuple(int(value) if hasattr(value, "item") else value for value in edge)
            node = int(node) if hasattr(node, "item") else node
            data = self.graph.edges[edge]
            mappings[key] = {"name": point["name"], "lat": point["lat"], "lon": point["lon"],
                "access_node": node, "edge": edge, "road_name": data.get("name", "unnamed"),
                "osmid": data.get("osmid"), "edge_distance_m": round(float(edge_distance), 2),
                "node_distance_m": round(float(node_distance), 2),
                "needs_review": max(float(edge_distance), float(node_distance)) > 150,
                "association": "nearest_street_unverified"}
        self.location_mappings = mappings
        return mappings

    def camera_road_edges(self, cam_id):
        """Selected directed street plus its actual reverse, excluding other roads."""
        mappings = getattr(self, "location_mappings", None) or self.build_location_mappings()
        u, v, key = mappings[cam_id]["edge"]
        selected = self.graph[u][v][key]
        edges = [(u, v, key)]
        for reverse_key, data in self.graph.get_edge_data(v, u, default={}).items():
            if data.get("osmid") == selected.get("osmid") and data.get("geometry") is not None and selected.get("geometry") is not None:
                if data["geometry"].equals(selected["geometry"]):
                    edges.append((v, u, reverse_key))
        return edges

    def set_camera_road_closed(self, cam_id, closed=True):
        """Manual road closure for experiments; inference alone must not call this.

        Track ownership so clearing this camera does not clear another closure.
        These affect newly planned routes; mid-trip replanning remains pending.
        """
        if cam_id not in HERO_CAMERAS:
            raise ValueError("Unknown camera")
        registry = self.state.setdefault("camera_road_closures", {})
        owned = {tuple(e) for edges in registry.values() for e in edges}
        current = {tuple(e) for e in self.state["blocked_edges"]}
        preserved = self.state.setdefault("camera_closure_external_edges", list(current - owned))
        external = (current - owned) | {tuple(e) for e in preserved}
        if closed:
            registry[cam_id] = self.camera_road_edges(cam_id)
        else:
            registry.pop(cam_id, None)
        self.state["blocked_edges"] = sorted(external | {tuple(e) for edges in registry.values() for e in edges}, key=str)
        return self.state["blocked_edges"]

    def road_verification_report(self):
        mappings = self.build_location_mappings()
        routes = {}
        for unit in self.state["resource_db"]:
            routes[unit] = {}
            for cam in HERO_CAMERAS:
                try:
                    plan = self.estimate_depletion_ticks(unit, cam, 5)
                    routes[unit][cam] = {k: plan[k] for k in ("travel_to_scene_ticks", "return_to_base_ticks", "total_depletion_ticks_remaining")}
                except nx.NetworkXNoPath:
                    routes[unit][cam] = {"unreachable": True}
        return {"routing": self.state["routing"], "attribution": self.graph.graph.get("attribution"),
                "nodes": self.graph.number_of_nodes(), "edges": self.graph.number_of_edges(),
                "locations": mappings, "round_trips": routes,
                "limitations": ["Team coordinates and nearest-street associations require visual verification",
                                "Inferred speeds are estimates; no live traffic",
                                "Study-area boundary can truncate routes; expand if routes are unreachable"]}

    def _route(self, start, end):
        graph = self.graph.copy()
        for edge in self.state["blocked_edges"]:
            if len(edge) == 3 and graph.has_edge(*edge):
                graph.remove_edge(*edge)
        a, b = self._nearest(start), self._nearest(end)
        path = nx.shortest_path(graph, a, b, weight="travel_time")
        seconds = 0.0
        coordinates = [[graph.nodes[n]["x"], graph.nodes[n]["y"]] for n in path]
        route_edges = []
        distance_m = 0.0
        cumulative_seconds = [0.0]
        for u, v in zip(path, path[1:]):
            key, data = min(graph[u][v].items(), key=lambda item: float(item[1]["travel_time"]))
            route_edges.append((u, v, key))
            distance_m += float(data.get("length", 0))
            seconds += float(data["travel_time"])
            cumulative_seconds.append(seconds)
        return {"ticks": max(1, math.ceil(seconds / 60)), "seconds": seconds,
                "edges": route_edges, "distance_m": distance_m, "path": coordinates, "cumulative_seconds": cumulative_seconds, "destination": {"lat": end["lat"], "lon": end["lon"]}}

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
    parser.add_argument("--road-report", type=Path, help="Write real-OSM camera/station and round-trip verification JSON")
    args = parser.parse_args()
    if args.road_report:
        graph, _ = load_sheffield_graph(download=args.download_osm, strict=True)
        sim = SheffieldSim(graph=graph, seed=42, demo=True)
        report = sim.road_verification_report()
        args.road_report.parent.mkdir(parents=True, exist_ok=True)
        args.road_report.write_text(json.dumps(report, indent=2), encoding="utf-8")
        print(f"Road report: {args.road_report} ({report['nodes']} nodes, {report['edges']} edges)")
    else:
        sim = SheffieldSim(seed=42, demo=True, download=args.download_osm)
    for _ in range(args.ticks):
        sim.tick()
    if not args.road_report:
        print(json.dumps(sim.state, indent=2))
