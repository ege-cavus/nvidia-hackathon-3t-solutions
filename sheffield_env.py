"""Track 1: minimal Gymnasium dispatch world; OSM road integration starts here."""
import gymnasium as gym
import numpy as np
from state_schema import HERO_CAMERAS, get_initial_city_state


def load_sheffield_graph():
    """Explicit network operation; never download during import or reset."""
    import osmnx as ox
    return ox.graph_from_place("Sheffield, South Yorkshire, England, UK", network_type="drive")


def vehicle_turnaround_contract():
    """Track 1 integration contract (routing/timer implementation pending).

    Dispatch only is_unit_available(u). IDLE_AT_BASE -> EN_ROUTE: route
    current (lat, lon) to camera over an OSMnx graph excluding blocked_edges;
    convert shortest-path street distance/speed to integer eta_ticks.
    No reachable route means reject dispatch, never use straight-line distance.
    EN_ROUTE -> ON_SCENE when eta_ticks reaches zero; lock for
    on_scene_ticks_remaining. Then route back to EMERGENCY_HUBS[home_hub],
    set eta_ticks and status RETURNING. Recompute routes if roads become blocked.
    RETURNING -> COOLDOWN only after arrival at the home station/hospital;
    decrement cooldown_ticks_remaining for handover/restock before IDLE_AT_BASE.
    Update positions along routes, never redispatch during any of these stages.
    After every change, total_busy_ticks = eta_ticks +
    on_scene_ticks_remaining + cooldown_ticks_remaining.
    eta_ticks is the current travel leg; the return ETA is computed on departure
    from the scene, so the convenience sum is not a full round-trip forecast.
    """


def propagate_hazards(state: dict, growth: float = 0.1) -> None:
    for incident in state["active_incidents"].values():
        incident["severity"] = min(10, max(1, incident["severity"] + growth))
    # TODO: use road graph adjacency, rainfall and blocked_edges for spatial spread.


class SheffieldEnv(gym.Env):
    """Action 0 waits; actions 1..5 resolve a camera incident (demo abstraction).

    Observation: five camera severities. Replace instant resolution with routed
    unit travel and capability checks before using this as an operational model.
    """
    metadata = {"render_modes": []}

    def __init__(self, max_ticks: int = 100):
        super().__init__()
        self.max_ticks = max_ticks
        self.cameras = tuple(HERO_CAMERAS)
        self.action_space = gym.spaces.Discrete(6)
        self.observation_space = gym.spaces.Box(0, 10, shape=(5,), dtype=np.float32)
        self.state = get_initial_city_state()

    def _observe(self):
        return np.array([max((i["severity"] for i in self.state["active_incidents"].values()
                             if i["camera_id"] == cam), default=0)
                         for cam in self.cameras], dtype=np.float32)

    def reset(self, *, seed=None, options=None):
        super().reset(seed=seed)
        self.state = get_initial_city_state()
        cam = self.cameras[int(self.np_random.integers(5))]
        self.state["active_incidents"]["DEMO_1"] = {
            "camera_id": cam, "hazard": "flood", "severity": 5,
            "road_blocked": False, "vlm_summary": "Synthetic training incident",
            "video_path": HERO_CAMERAS[cam]["disaster_video_path"],
        }
        return self._observe(), {}

    def step(self, action):
        if not self.action_space.contains(action):
            raise ValueError("Action must be an integer from 0 to 5")
        reward = 0.0
        if action:
            cam = self.cameras[int(action) - 1]
            for key, incident in list(self.state["active_incidents"].items()):
                if incident["camera_id"] == cam:
                    reward += incident["severity"] * self.state["rl_priority_weights"][incident["hazard"]]
                    del self.state["active_incidents"][key]
        propagate_hazards(self.state)
        reward -= sum(i["severity"] for i in self.state["active_incidents"].values()) * 0.01
        self.state["tick"] += 1
        self.state["score_ai"] += reward
        terminated = not self.state["active_incidents"]
        truncated = self.state["tick"] >= self.max_ticks and not terminated
        return self._observe(), float(reward), terminated, truncated, {}


if __name__ == "__main__":
    env = SheffieldEnv()
    print(env.reset(seed=42))
    print(env.step(0))
