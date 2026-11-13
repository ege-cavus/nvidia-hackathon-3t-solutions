"""Shared integration layer for a future solo human-versus-agents game UI.

No Streamlit or model imports: controllers depend on this layer, not vice versa.
A fixed incident tape avoids RNG divergence caused by different player decisions.
"""
from copy import deepcopy
from dataclasses import dataclass
import random
from typing import Awaitable, Callable, Literal
from knowledge_base import NODE_HISTORICAL_PROBABILITIES
from state_schema import DispatchCommand, HERO_CAMERAS, get_initial_city_state, make_incident
from sheffield_sim import SheffieldSim, synthetic_graph

Side = Literal["HUMAN", "AGENTS"]
AgentController = Callable[[SheffieldSim], Awaitable[None]]


@dataclass(frozen=True)
class ScenarioEvent:
    tick: int
    cam_id: str
    hazard: str
    severity: int
    road_blocked: bool = False

    def __post_init__(self):
        if type(self.tick) is not int or self.tick < 1:
            raise ValueError("Event tick must be a positive integer")
        if type(self.road_blocked) is not bool:
            raise ValueError("road_blocked must be boolean")
        make_incident(self.cam_id, self.hazard, self.severity, self.tick, self.road_blocked)


@dataclass(frozen=True)
class Scenario:
    events: tuple[ScenarioEvent, ...]
    horizon_ticks: int = 60
    seed: int = 42

    def __post_init__(self):
        if type(self.horizon_ticks) is not int or self.horizon_ticks < 2:
            raise ValueError("Match horizon must be at least two ticks")
        if type(self.events) is not tuple or any(not isinstance(e, ScenarioEvent) for e in self.events):
            raise ValueError("events must be an immutable tuple of ScenarioEvent")
        if any(e.tick >= self.horizon_ticks for e in self.events):
            raise ValueError("Incidents must arrive before the final scoring tick")
        if len({e.cam_id for e in self.events}) != len(self.events):
            raise ValueError("Version 1 supports one incident per camera; repeated incidents need unique IDs")


def make_demo_scenario(seed=42, horizon_ticks=60) -> Scenario:
    """Precompute identical external events, independent of policy state or calls."""
    if horizon_ticks < 22:
        raise ValueError("Five-camera demo requires at least 22 ticks")
    rng = random.Random(seed)
    events = []
    for index, cam in enumerate(HERO_CAMERAS):
        weights = NODE_HISTORICAL_PROBABILITIES[cam]["hazard_weights"]
        hazard = rng.choices(list(weights), weights=list(weights.values()))[0]
        events.append(ScenarioEvent(1+index*4, cam, hazard, rng.randint(4, 8), rng.choice([True, False])))
    return Scenario(tuple(events), horizon_ticks, seed)


class SoloMatch:
    """Two isolated worlds with a shared clock, scenario, road graph and rules.

    advance() opens the next decision tick. Human submits DispatchCommands;
    run_agents(controller) calls the existing agent pipeline for its own world.
    LLM wall time never advances the clock. The UI owns any real-time deadline.
    Final horizon tick closes decisions; scorecards are compared then.
    """
    def __init__(self, scenario=None, graph=None):
        self.scenario = scenario if scenario is not None else make_demo_scenario()
        graph = graph if graph is not None else synthetic_graph()
        self._worlds = {side: SheffieldSim(get_initial_city_state(), graph=deepcopy(graph), seed=self.scenario.seed)
                        for side in ("HUMAN", "AGENTS")}
        self._tick = 0
        self._agents_running = False
        self._agents_ran_tick = None

    @property
    def tick(self):
        return self._tick

    @property
    def finished(self):
        return self._tick >= self.scenario.horizon_ticks

    def observation(self, side: Side) -> dict:
        """Detached current-world snapshot; no opponent decisions or future tape."""
        if side not in self._worlds:
            raise ValueError("Unknown side")
        return deepcopy(self._worlds[side].state)

    def advance(self):
        if self._agents_running:
            raise RuntimeError("Finish agent decisions before advancing the shared clock")
        if self.finished:
            raise ValueError("Match finished")
        self._tick += 1
        for world in self._worlds.values():
            # Crucial: never use per-world random spawning in a comparison match.
            world.tick(spawn=False)
            for event in self.scenario.events:
                if event.tick == self._tick:
                    world.state["active_incidents"][event.cam_id] = make_incident(
                        event.cam_id, event.hazard, event.severity, event.tick, event.road_blocked)
        return self.scoreboard()

    def submit_human(self, command: DispatchCommand) -> dict:
        if self._tick == 0 or self.finished:
            raise ValueError("No open decision tick")
        if command.controller != "HUMAN":
            raise ValueError("Human controller required")
        return self._worlds["HUMAN"].apply_command(command)

    async def run_agents(self, controller: AgentController):
        """Trusted controller adapter; invoke process_pending(state, sim=world).

        An adapter must not call tick(), mutate graph/rules or read the other side.
        One run per decision tick prevents retries from granting extra model time.
        """
        if self._tick == 0 or self.finished:
            raise ValueError("No open decision tick")
        if self._agents_running or self._agents_ran_tick == self._tick:
            raise ValueError("Agents already ran this tick")
        self._agents_running = True
        self._agents_ran_tick = self._tick
        try:
            await controller(self._worlds["AGENTS"])
        finally:
            self._agents_running = False

    def scoreboard(self):
        return {side: deepcopy(world.state["effectiveness_metrics"]) for side, world in self._worlds.items()}

    def replay_commands(self, side: Side):
        return deepcopy(self.observation(side)["command_log"])
