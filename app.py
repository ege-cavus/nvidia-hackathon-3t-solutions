"""Track 3: streamlit run app.py. Offline, synthetic simulation by default."""
import asyncio
from pathlib import Path
import pydeck as pdk
import streamlit as st
from knowledge_base import SOURCES
from state_schema import EMERGENCY_HUBS, HERO_CAMERAS, get_initial_city_state
from sheffield_sim import SheffieldSim
from agent_orchestrator import process_pending


def main():
    st.set_page_config(page_title="SURU Shared Command", layout="wide")
    st.title("Sheffield Urban Response Unit")
    st.caption("Hackathon simulation | Qwen triage -> selected Nemotron agencies -> consensus or Qwen takeover")
    if "sim" not in st.session_state:
        st.session_state.sim = SheffieldSim(get_initial_city_state(), seed=42, demo=True)
    sim = st.session_state.sim
    state = sim.state
    live = st.sidebar.toggle("Live Ollama models", value=False)
    st.sidebar.caption("Live mode requires local model tags and prepared Cosmos assets. Offline mode uses synthetic triage.")
    if st.sidebar.button("Reset Seed 42 scenario"):
        st.session_state.sim = SheffieldSim(get_initial_city_state(), seed=42, demo=True)
        st.rerun()
    if st.sidebar.button("Advance tick and process incidents"):
        sim.tick()
        with st.spinner("Triage and selected agency negotiation"):
            asyncio.run(process_pending(state, sim=sim, live=live))
    if st.sidebar.button("Retry pending negotiations"):
        asyncio.run(process_pending(state, sim=sim, live=live))
    st.sidebar.write(f"Tick {state['tick']} (one minute per tick)")
    st.sidebar.write(f"Graph: {state['routing']['mode']}")
    st.sidebar.warning("Risk priors and numeric SOP thresholds are uncalibrated demo assumptions.")
    for key, unit in state["resource_db"].items():
        st.sidebar.write(f"{key}: {unit['status']} | {unit['total_depletion_ticks_remaining']} ticks")
        st.sidebar.caption(unit["home_hub"])
    st.subheader("Response Effectiveness Scorecard")
    metrics = state["effectiveness_metrics"]
    cols = st.columns(4)
    for column, title, key in zip(cols, ["Overall score", "Resolved", "SLA met", "SLA missed"],
                                  ["overall_score", "incidents_resolved", "sla_met_count", "sla_missed_count"]):
        column.metric(title, metrics[key])
    st.caption(f"Mean first-response ticks: {metrics['avg_response_ticks']:.2f} | "
               f"Spillovers prevented: {metrics['spillovers_prevented']} | Occurred: {metrics['spillovers_occurred']}")
    roads = []
    for u, v, data in sim.graph.edges(data=True):
        geometry = data.get("geometry")
        path = list(geometry.coords) if geometry is not None else [
            [sim.graph.nodes[u]["x"], sim.graph.nodes[u]["y"]],
            [sim.graph.nodes[v]["x"], sim.graph.nodes[v]["y"]]]
        roads.append({"path": path})
    stations = [{"id": key, **hub} for key, hub in EMERGENCY_HUBS.items()]
    units = [{"id": key, "name": f"{unit['agency']} {unit['status']}", **unit}
             for key, unit in state["resource_db"].items()]
    incidents = [{"id": cam, "name": f"{incident['hazard']} severity {incident['severity']}",
                  **incident["location"]} for cam, incident in state["active_incidents"].items()
                 if incident["status"] != "RESOLVED"]
    layers = [pdk.Layer("PathLayer", roads, get_path="path", get_color=[100, 120, 140], get_width=3)]
    for points, color, radius in [(stations, [40, 140, 255], 130), (incidents, [255, 50, 40], 150), (units, [255, 200, 40], 70)]:
        layers.append(pdk.Layer("ScatterplotLayer", points, get_position="[lon, lat]", get_fill_color=color,
                                get_radius=radius, pickable=True))
    st.pydeck_chart(pdk.Deck(layers=layers, map_style=None,
        initial_view_state=pdk.ViewState(latitude=53.39, longitude=-1.46, zoom=11),
        tooltip={"text": "{id}: {name}"}))
    st.caption("Blue: stations | Red: incidents | Yellow: units. Synthetic graph lines are invented links, not actual streets.")
    st.subheader("Cosmos 3 CCTV and Qwen Initial Triage")
    cameras = st.multiselect("Four-panel monitor", list(HERO_CAMERAS), default=list(HERO_CAMERAS)[:4], max_selections=4)
    for row in range(2):
        for offset, column in enumerate(st.columns(2)):
            index = row*2 + offset
            if index >= len(cameras):
                continue
            cam = cameras[index]
            with column:
                st.write(f"{cam}: {HERO_CAMERAS[cam]['name']}")
                path = Path(HERO_CAMERAS[cam]["cosmos_video_path"])
                if path.is_file():
                    st.video(str(path))
                else:
                    st.info(f"Awaiting local Cosmos MP4: {path}")
                incident = state["active_incidents"].get(cam)
                if incident:
                    st.write(incident["status"])
                    st.write(incident["qwen_report"]["summary"])
                    st.caption("Agency wake-up: " + ", ".join(incident["qwen_report"]["required_agencies"]))
    st.subheader("Shared Multi-Agent Message Board")
    if state["message_board"]:
        st.dataframe(state["message_board"], width="stretch", hide_index=True)
    else:
        st.info("Advance a tick to trigger triage and negotiation.")
    st.subheader("Live Resource Database and Depletion Timers")
    fields = ["agency", "home_hub", "lat", "lon", "status", "assigned_cam", "travel_to_scene_ticks",
              "on_scene_ticks", "return_to_base_ticks", "cooldown_ticks_remaining", "total_depletion_ticks_remaining"]
    st.dataframe([{"unit": key, **{field: unit[field] for field in fields}} for key, unit in state["resource_db"].items()],
                 width="stretch", hide_index=True)
    with st.expander("Data provenance and simulation rules"):
        st.json(state["node_probabilities"])
        st.json(SOURCES)
        st.write("Score = clamp(100 - 5*SLA misses - 3*spillovers + resolved, 0, 100). "
                 "SLA uses first arrival <=7 ticks. Spillover is counted once on severity >=9; "
                 "resolved before spillover counts as prevented. These are game metrics.")


if __name__ == "__main__":
    main()
