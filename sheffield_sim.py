"""Track 1: directed graph world, resource depletion and effective response scoring.

Opt-in Park Square flood probe (real cached/downloaded roads required by CLI):
    python sheffield_sim.py --download-osm --flood-demo --ticks 60
    python sheffield_sim.py --flood-demo --ticks 60 --response-delay 15
    python sheffield_sim.py --cosmos-manifest

Flood intensity is normalized 0..10, NOT water depth. Standoff, escalation and
work rates are explicit demonstration assumptions, not calibrated hydrodynamics.
Staged observation/reinforcement fields are additive; the agent/UI adapters still
need to consume them. Existing generic scenarios keep their original behaviour.
"""
import argparse
import json
import math
import random
from pathlib import Path
import networkx as nx
from knowledge_base import NODE_HISTORICAL_PROBABILITIES
from state_schema import DispatchCommand, EMERGENCY_HUBS, HERO_CAMERAS, get_initial_city_state, is_unit_available, make_incident, required_agencies_for_incident


def effectiveness_score_breakdown(metrics):
    """Explain the current game score without changing its existing weights."""
    components = {"baseline": 100.0, "sla_miss_penalty": -5.0 * metrics["sla_missed_count"],
                  "spillover_penalty": -3.0 * metrics["spillovers_occurred"],
                  "resolution_reward": float(metrics["incidents_resolved"])}
    raw = sum(components.values())
    return {"components": components, "unclamped_score": raw,
            "overall_score": max(0.0, min(100.0, raw)),
            "limitations": "Placeholder game weights; exposure, closure duration and resource use are diagnostic, not scored yet"}


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

    def select_staging_location(self, cam_id, min_standoff_m=50, max_distance_m=350):
        """Find a provisional staging node; not an assertion of real flood safety.

        Remove the threatened street BEFORE selecting access. Require a directed
        round trip from all six station access nodes. The 50m standoff is a game
        assumption; real flood footprints/entrances need manual verification.
        """
        if not 0 <= min_standoff_m <= max_distance_m:
            raise ValueError("Invalid staging distance bounds")
        mappings = getattr(self, "location_mappings", None) or self.build_location_mappings()
        threatened = self.camera_road_edges(cam_id)
        graph = self.graph.copy()
        graph.remove_edges_from([tuple(e) for e in self.state["blocked_edges"]] + threatened)
        bases = {mappings[hub]["access_node"] for hub in EMERGENCY_HUBS}
        component = next((c for c in nx.strongly_connected_components(graph) if bases <= c), set())
        forbidden_nodes = {n for u, v, _ in threatened for n in (u, v)}
        candidates = []
        for node in component - forbidden_nodes:
            point = {"lat": graph.nodes[node]["y"], "lon": graph.nodes[node]["x"]}
            distance = _distance(HERO_CAMERAS[cam_id], point)
            if min_standoff_m <= distance <= max_distance_m:
                candidates.append((distance, str(node), node, point))
        if not candidates:
            raise nx.NetworkXNoPath("No staging node with unblocked round trips to all bases")
        distance, _, node, point = min(candidates)
        return {**point, "graph_node": node, "distance_from_camera_m": round(distance, 1),
                "safety_status": "SIMULATION_STANDOFF_UNVERIFIED", "excluded_edges": threatened}

    def start_park_square_flood(self):
        """Opt-in three-stage demo. Generic incident behaviour remains unchanged."""
        cam_id = "CAM_08"
        previous = self.state["active_incidents"].get(cam_id)
        if previous and previous["status"] != "RESOLVED":
            raise ValueError("Park Square already has an active incident")
        if any(u["assigned_cam"] == cam_id for u in self.state["resource_db"].values()):
            raise ValueError("Previous incident still holds resources")
        staging = self.select_staging_location(cam_id)
        incident = make_incident(cam_id, "flood", 3, self.state["tick"])
        incident.update(incident_type="flood", progression_model="park_square_flood_v1",
                        water_level=3.0, water_level_unit="NORMALISED_0_10_NOT_METRES", stage="POOLING", stage_elapsed_ticks=0,
                        stage_history=[{"tick": self.state["tick"], "stage": "POOLING"}],
                        staging_location=staging, hazard_edges=staging["excluded_edges"],
                        response_requirements=["FIRE"], reinforcement_required=["FIRE"],
                        control_state="NO_EFFECTIVE_RESPONSE")
        self._set_flood_visual(incident)
        self.state["active_incidents"][cam_id] = incident
        return incident

    @staticmethod
    def _set_flood_visual(incident):
        # An observation contract for Cosmos preparation, not generated footage.
        stem = f"assets/hero_cams_disaster/{incident['cam_id']}_flood_{incident['stage']}"
        incident["visual_observation"] = {"camera_id": incident["cam_id"], "incident_type": "flood",
            "stage": incident["stage"], "clip_path": stem + ".mp4", "frame_path": stem + ".jpg",
            "asset_status": "LOCAL_ASSET" if Path(stem + ".mp4").is_file() else "PLACEHOLDER"}

    def _update_flood(self, cam_id, incident):
        on_scene = {u["agency"] for u in self.state["resource_db"].values()
                    if u["assigned_cam"] == cam_id and u["status"] == "DEPLETED_ON_SCENE"}
        required = set(incident["response_requirements"])
        if required <= on_scene:
            self._record_response(incident)
            incident["water_level"] = max(0.0, incident["water_level"] - 1.0)
            incident["control_state"] = "CONTROLLED"
        elif "FIRE" in on_scene:
            incident["water_level"] = min(10.0, incident["water_level"] + 0.25)
            incident["control_state"] = "PARTIAL_RESPONSE"
        else:
            incident["water_level"] = min(10.0, incident["water_level"] + 0.5)
            incident["control_state"] = "NO_EFFECTIVE_RESPONSE"
        level = incident["water_level"]
        stage = "CLEARED" if level == 0 else "POOLING" if level <= 3 else "ROAD_FLOODED" if level <= 6 else "VEHICLES_TRAPPED"
        if stage != incident["stage"]:
            incident["stage"] = stage
            incident["stage_elapsed_ticks"] = 0
            incident["stage_history"].append({"tick": self.state["tick"], "stage": stage})
            self._set_flood_visual(incident)
        else:
            incident["stage_elapsed_ticks"] += 1
        blocked = level > 3
        if blocked != incident["road_blocked"]:
            self.set_camera_road_closed(cam_id, blocked)
        incident["road_blocked"] = blocked
        incident["severity"] = max(1, min(10, math.ceil(level)))
        incident["response_requirements"] = ["FIRE", "POLICE"] if blocked else ["FIRE"]
        committed = {u["agency"] for u in self.state["resource_db"].values()
                     if u["assigned_cam"] == cam_id and u["status"] in ("DEPLETED_EN_ROUTE", "DEPLETED_ON_SCENE")}
        incident["reinforcement_required"] = sorted(set(incident["response_requirements"]) - committed) if level else []

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

    def _route(self, start, end, excluded_edges=()):
        graph = self.graph.copy()
        for edge in list(self.state["blocked_edges"]) + list(excluded_edges):
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
        incident = self.state["active_incidents"].get(cam_id, {})
        camera = incident.get("staging_location", HERO_CAMERAS[cam_id])
        home = EMERGENCY_HUBS[unit["home_hub"]]
        excluded = incident.get("hazard_edges", ())
        outward = self._route(unit, camera, excluded)
        # Trauma patients go to Northern General; Hallamshire is only a demo base.
        hospital = EMERGENCY_HUBS["NHS_NORTHERN_GEN"] if unit["agency"] == "AMBULANCE" else home
        returning = self._route(camera, hospital, excluded)
        final = self._route(hospital, home, excluded) if hospital is not home and hospital != home else None
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
        staged = incident.get("progression_model") == "park_square_flood_v1"
        reinforcing = incident["status"] == "UNITS_DISPATCHED" and staged
        if incident["status"] == "RESOLVED" or (incident["status"] == "UNITS_DISPATCHED" and not staged):
            return False, "Incident already dispatched or resolved"
        if not isinstance(unit_ids, list) or any(not isinstance(u, str) for u in unit_ids):
            return False, "Unit allocation must be a list of IDs"
        if len(unit_ids) != len(set(unit_ids)) or not unit_ids:
            return False, "Empty or duplicate allocation"
        if any(u not in self.state["resource_db"] or not is_unit_available(self.state["resource_db"][u]) for u in unit_ids):
            return False, "Unknown or unavailable unit"
        units = [self.state["resource_db"][u] for u in unit_ids]
        existing = [u for u in self.state["resource_db"].values() if staged and u["assigned_cam"] == cam_id
                    and u["status"] in ("DEPLETED_EN_ROUTE", "DEPLETED_ON_SCENE")]
        if reinforcing and not (set(incident["response_requirements"]) - {u["agency"] for u in existing}) & {u["agency"] for u in units}:
            return False, "Reinforcement must supply a missing capability"
        agencies = {u["agency"] for u in units + existing}
        required = set(required_agencies_for_incident(incident)) | set(incident.get("response_requirements", []))
        if not required.issubset(agencies):
            return False, "Missing required agency"
        if (incident["road_blocked"] or (incident["severity"] >= 6 and incident["hazard"] in ("flood", "trauma"))) and "POLICE" not in agencies:
            return False, "Police cordon required"
        if incident["severity"] >= 7 and incident["hazard"] in ("fire", "flood"):
            pumps = sum(u["agency"] == "FIRE" for u in units + existing)
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
        incident.update(status="UNITS_DISPATCHED", dispatched_units=list(dict.fromkeys(incident.get("dispatched_units", []) + list(unit_ids))))

    @staticmethod
    def _phase(status, timer, route):
        return {"status": status, "timer": timer, "remaining": route["ticks"],
                "duration": route["ticks"], "route": route}

    def _record_response(self, incident):
        if incident["response_ticks"] is not None:
            return
        response = self.state["tick"] - incident["spawn_tick"]
        incident["response_ticks"] = response
        metrics = self.state["effectiveness_metrics"]
        count = metrics.get("response_sample_count", 0)
        metrics["avg_response_ticks"] = (metrics["avg_response_ticks"] * count + response)/(count+1)
        metrics["response_sample_count"] = count + 1
        if not incident["sla_recorded"]:
            metrics["sla_met_count" if response <= 7 else "sla_missed_count"] += 1
            incident["sla_recorded"] = True

    def _advance_unit(self, unit):
        if is_unit_available(unit):
            return
        phase = unit["phases"][0]
        incident = self.state["active_incidents"].get(unit["assigned_cam"])
        holding_flood = (incident and incident.get("progression_model") == "park_square_flood_v1"
                         and incident["status"] != "RESOLVED")
        if phase["status"] == "DEPLETED_ON_SCENE" and phase["remaining"] == 1 and incident and (holding_flood or (incident["severity"] >= 4 if unit["agency"] == "POLICE" else incident["severity"] > 1)):
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
                if incident.get("progression_model") != "park_square_flood_v1":
                    self._record_response(incident)
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
            staged = incident.get("progression_model") == "park_square_flood_v1"
            if staged:
                self._update_flood(cam_id, incident)
            else:
                incident["severity"] = max(1, incident["severity"] - 1) if on_scene else min(10, incident["severity"] + (self.state["tick"] % 3 == 0))
            cleared = incident["water_level"] == 0 if staged else incident["severity"] == 1
            if cleared and on_scene:
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
        metrics["overall_score"] = effectiveness_score_breakdown(metrics)["overall_score"]
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


def park_square_cosmos_manifest(reference_frame=None):
    """Reviewable local-asset specification; performs no model/API calls.

    Prompts should be adapted to the actual available Cosmos runtime. A verified
    Park Square reference and consistent viewpoint are prerequisites to generation.
    """
    descriptions = {
        "POOLING": "Shallow rainwater pooling near road drains; road still passable; no trapped occupants.",
        "ROAD_FLOODED": "Standing floodwater covers the approach road; traffic stopped before the water; no vehicle driving through it.",
        "VEHICLES_TRAPPED": "Floodwater surrounds a stationary car with visible occupants awaiting rescue; no graphic injuries; road impassable.",
        "CLEARED": "Floodwater has receded; wet road surface and residual puddles; the formerly trapped car is stationary on dry ground.",
    }
    common = ("Synthetic emergency-training CCTV of Park Square, Sheffield. Use the supplied reference "
              "for road geometry, buildings and a fixed elevated viewpoint. Keep camera position, weather, "
              "lighting, road layout and vehicle identities consistent across stages. Locked-off camera, "
              "overcast daylight, natural motion, no camera movement, no captions, no logos or UI. ")
    return {"camera_id": "CAM_08", "reference_frame": str(reference_frame) if reference_frame else None,
            "reference_status": "LOCAL_UNVERIFIED" if reference_frame and Path(reference_frame).is_file() else "REQUIRED",
            "generation_status": "NOT_GENERATED", "duration_seconds_requested": 8,
            "aspect_ratio_requested": "16:9", "seed_requested": 42,
            "notes": ["Runtime-specific options/model tags must be confirmed before generation",
                      "These are synthetic stage observations, not verified real CCTV or flood predictions",
                      "Generate all stages from the same verified reference; visually inspect consistency",
                      "Do not reuse a clip from another stage when an asset is missing"],
            "stages": [{"stage": stage, "prompt": common + description,
                        "clip_path": f"assets/hero_cams_disaster/CAM_08_flood_{stage}.mp4",
                        "frame_path": f"assets/hero_cams_disaster/CAM_08_flood_{stage}.jpg"}
                       for stage, description in descriptions.items()]}


def run_park_square_flood_demo(graph=None, response_delay=0, ticks=60):
    """Reproducible world-model probe with an explicit scripted response.

    This tests physics; it is not an AI benchmark. Tick rates, work and staging
    assumptions are game rules. No Cosmos inference/generation is performed.
    """
    if (response_delay is not None and (type(response_delay) is not int or response_delay < 0)) or type(ticks) is not int or ticks < 1:
        raise ValueError("Response delay must be nonnegative or None (no dispatch), and ticks positive")
    sim = SheffieldSim(graph=graph)
    incident = sim.start_park_square_flood()
    trace = []
    busy_ticks = {key: 0 for key in sim.state["resource_db"]}
    for _ in range(ticks):
        if sim.state["tick"] == response_delay:
            sim.dispatch("CAM_08", ["FIRE_1", "POL_2"], controller="HUMAN")
        for key, unit in sim.state["resource_db"].items():
            busy_ticks[key] += int(not is_unit_available(unit))
        sim.tick(spawn=False)
        trace.append({"tick": sim.state["tick"], "stage": incident["stage"],
                      "severity": incident["severity"], "water_level": incident["water_level"],
                      "road_blocked": incident["road_blocked"], "status": incident["status"],
                      "control_state": incident["control_state"],
                      "unit_status": {key: sim.state["resource_db"][key]["status"] for key in ("FIRE_1", "POL_2")}})
    return {"routing": sim.state["routing"], "response_delay_ticks": response_delay,
            "staging": incident["staging_location"], "stage_history": incident["stage_history"],
            "effectiveness_metrics": sim.state["effectiveness_metrics"],
            "score_breakdown": effectiveness_score_breakdown(sim.state["effectiveness_metrics"]),
            "outcomes": {"resolved_tick": next((e["tick"] for e in incident["stage_history"] if e["stage"] == "CLEARED"), None),
                         "effective_response_tick": incident["response_ticks"],
                         "road_closed_ticks": sum(int(t["road_blocked"]) for t in trace),
                         "trapped_vehicle_ticks": sum(t["stage"] == "VEHICLES_TRAPPED" for t in trace),
                         "intensity_tick_exposure": sum(t["water_level"] for t in trace),
                         "unit_busy_ticks": busy_ticks, "final_status": incident["status"]},
            "visual_observation": incident["visual_observation"], "trace": trace}


def compare_park_square_responses(graph=None, ticks=60):
    """Same environment/horizon, four scripted policies; no models or footage."""
    if graph is None:
        graph, _ = load_sheffield_graph(strict=True)
    cases = []
    for label, delay in (("prompt", 0), ("delay_5", 5), ("delay_15", 15), ("no_response", None)):
        result = run_park_square_flood_demo(graph, delay, ticks)
        cases.append({"case": label, "dispatch_delay_ticks": delay,
                      "score_breakdown": result["score_breakdown"], "outcomes": result["outcomes"],
                      "effectiveness_metrics": result["effectiveness_metrics"]})
    return {"comparison_type": "SCRIPTED_RESPONSE_NOT_AI_BENCHMARK", "horizon_ticks": ticks,
            "tick_seconds": 60, "same_graph_and_incident": True,
            "cosmos_required": False, "cases": cases,
            "sampling": "Incident exposure and closure durations use end-of-tick snapshots; unit busy ticks count each occupied tick interval"}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--ticks", type=int, default=5)
    parser.add_argument("--download-osm", action="store_true")
    parser.add_argument("--cosmos-manifest", action="store_true", help="Print the four stage clip specifications; does not generate video")
    parser.add_argument("--cosmos-reference", type=Path, help="Local Park Square reference frame for the manifest")
    parser.add_argument("--compare-flood", action="store_true", help="Compare prompt, delayed and absent scripted responses without Cosmos")
    parser.add_argument("--flood-demo", action="store_true", help="Run staged Park Square flood with scripted Fire/Police response")
    parser.add_argument("--response-delay", type=int, default=0, help="Dispatch delay in ticks for --flood-demo")
    parser.add_argument("--road-report", type=Path, help="Write real-OSM camera/station and round-trip verification JSON")
    args = parser.parse_args()
    if args.cosmos_manifest:
        print(json.dumps(park_square_cosmos_manifest(args.cosmos_reference), indent=2))
        raise SystemExit(0)
    if args.ticks < 0 or args.response_delay < 0:
        parser.error("Ticks and response delay must be nonnegative")
    if args.compare_flood:
        if args.ticks < 1 or args.road_report or args.flood_demo:
            parser.error("--compare-flood needs positive --ticks and cannot combine with --flood-demo/--road-report")
        graph, _ = load_sheffield_graph(download=args.download_osm, strict=True)
        print(json.dumps(compare_park_square_responses(graph, args.ticks), indent=2))
        raise SystemExit(0)
    if args.flood_demo:
        if args.ticks < 1 or args.road_report:
            parser.error("--flood-demo requires positive --ticks and cannot combine with --road-report")
        graph, _ = load_sheffield_graph(download=args.download_osm, strict=True)
        print(json.dumps(run_park_square_flood_demo(graph, args.response_delay, args.ticks), indent=2))
        raise SystemExit(0)
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
