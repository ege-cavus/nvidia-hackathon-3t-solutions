"""Track 3: run with streamlit run app.py."""
from pathlib import Path
import streamlit as st
import pydeck as pdk
from state_schema import EMERGENCY_HUBS, HERO_CAMERAS, get_initial_city_state, is_unit_available


def main():
    st.set_page_config(page_title="SURU Command Center", layout="wide")
    st.title("Sheffield Urban Response Unit")
    st.caption("Hackathon simulation ? synthetic incidents and placeholder CCTV assets")
    if "city_state" not in st.session_state:
        st.session_state.city_state = get_initial_city_state()
    state = st.session_state.city_state
    state["mode"] = st.sidebar.selectbox("Command mode", ["AI_AUTONOMOUS", "HUMAN_VS_AI"])
    if st.sidebar.button("Reset simulation"):
        st.session_state.city_state = get_initial_city_state()
        st.rerun()
    if st.sidebar.button("Advance tick"):
        state["tick"] += 1
    st.sidebar.subheader("Unit coverage and lockouts")
    for unit_id, vehicle in state["units"].items():
        st.sidebar.write(f"{unit_id}: {vehicle['status']} | {vehicle['total_busy_ticks']} busy ticks")
        st.sidebar.caption(f"{vehicle['home_hub']} | ({vehicle['lat']:.4f}, {vehicle['lon']:.4f})")
    st.write(f"Tick {state['tick']} | AI score {state['score_ai']:.1f} | Human score {state['score_human']:.1f}")
    points = [{"camera_id": key, **camera} for key, camera in HERO_CAMERAS.items()]
    hubs = [{"camera_id": key, **hub} for key, hub in EMERGENCY_HUBS.items()]
    st.pydeck_chart(pdk.Deck(
        layers=[pdk.Layer("ScatterplotLayer", points, get_position="[lon, lat]",
                          get_radius=90, get_fill_color=[80, 180, 240], pickable=True),
                pdk.Layer("ScatterplotLayer", hubs, get_position="[lon, lat]",
                          get_radius=130, get_fill_color=[255, 140, 40], pickable=True)],
        initial_view_state=pdk.ViewState(latitude=53.39, longitude=-1.45, zoom=11),
        map_style=None, tooltip={"text": "{camera_id}: {name}"},
    ))
    st.subheader("2x2 CCTV Monitor")
    selected = st.multiselect("Monitor cameras (maximum four)", list(HERO_CAMERAS),
                             default=list(HERO_CAMERAS)[:4], max_selections=4)
    for row in range(2):
        columns = st.columns(2)
        for offset, column in enumerate(columns):
            index = row * 2 + offset
            with column:
                if index >= len(selected):
                    st.info("No camera selected")
                    continue
                cam = selected[index]
                st.write(HERO_CAMERAS[cam]["name"])
                incident = next((i for i in state["active_incidents"].values() if i["camera_id"] == cam), None)
                path = Path(incident["video_path"] if incident else HERO_CAMERAS[cam]["clean_video_path"])
                if path.is_file():
                    st.video(str(path))
                else:
                    st.info(f"Awaiting local video: {path}")
    if state["mode"] == "HUMAN_VS_AI":
        st.subheader("Beat the AI")
        st.caption("Select an available unit and target to plan a dispatch. Routed dispatch and PPO comparison await Track 1 integration.")
        available = [key for key, vehicle in state["units"].items() if is_unit_available(vehicle)]
        unit = st.selectbox("Available unit", available)
        camera = st.selectbox("Target camera", list(HERO_CAMERAS))
        st.info("Dispatch awaits Track 1 street routing and turnaround timers; no travel estimate is fabricated.")
        st.button("Dispatch unit", disabled=True)
    st.subheader("Active incidents")
    st.json(state["active_incidents"])
    st.subheader("Live Comms Log")
    for entry in state["comms_log"]:
        st.write(f"{entry['timestamp']} | {entry['sender']}: {entry['message']}")
    # TODO: consume Track 2 command results and Track 1 simulation ticks here.


if __name__ == "__main__":
    main()
