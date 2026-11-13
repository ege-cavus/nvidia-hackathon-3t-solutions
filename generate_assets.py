"""Track 4: local CCTV GeoJSON, video preparation and deterministic storm demo.

Supply the council's 212-camera GeoJSON locally; no dataset is bundled or invented.
"""
import argparse
import json
import random
from pathlib import Path
from state_schema import HERO_CAMERAS, get_initial_city_state


def load_cctv_geojson(path, expected_count=212):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    if data.get("type") != "FeatureCollection":
        raise ValueError("Expected a GeoJSON FeatureCollection")
    features = data.get("features", [])
    if expected_count is not None and len(features) != expected_count:
        raise ValueError(f"Expected {expected_count} cameras, received {len(features)}")
    for feature in features:
        geometry = feature.get("geometry") or {}
        if geometry.get("type") != "Point" or len(geometry.get("coordinates", [])) < 2:
            raise ValueError("Each camera must have Point coordinates [longitude, latitude]")
    return data


def prepare_video(source, destination, max_frames=300, fps=10.0):
    import cv2
    capture = cv2.VideoCapture(str(source))
    writer = None
    count = 0
    try:
        if not capture.isOpened():
            raise ValueError(f"Cannot open video: {source}")
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        while count < max_frames:
            ok, frame = capture.read()
            if not ok:
                break
            if writer is None:
                writer = cv2.VideoWriter(str(destination), cv2.VideoWriter_fourcc(*"mp4v"), fps,
                                         (frame.shape[1], frame.shape[0]))
                if not writer.isOpened():
                    raise ValueError(f"Cannot write video: {destination}")
            writer.write(frame)
            count += 1
        if not count:
            raise ValueError("Source contains no readable frames")
    finally:
        capture.release()
        if writer is not None:
            writer.release()
    return count


def apply_super_storm(state: dict, seed=42) -> dict:
    """Mutate the supplied state with repeatable synthetic incidents."""
    rng = random.Random(seed)
    for index, (cam, details) in enumerate(HERO_CAMERAS.items(), 1):
        state["active_incidents"][f"STORM_{index:02d}"] = {
            "camera_id": cam, "hazard": rng.choice(["flood", "fire", "trauma"]),
            "severity": rng.randint(4, 10), "road_blocked": rng.choice([True, False]),
            "vlm_summary": "Synthetic Seed #42 Super-Storm scenario" if seed == 42 else f"Synthetic storm seed {seed}",
            "video_path": details["disaster_video_path"],
        }
    # TODO: map blocked camera roads to OSM edge tuples once a graph is available.
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--geojson", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    if bool(args.source) != bool(args.destination):
        parser.error("--source and --destination must be provided together")
    if args.geojson:
        print(f"Loaded {len(load_cctv_geojson(args.geojson)['features'])} cameras")
    if args.source:
        print(f"Prepared {prepare_video(args.source, args.destination)} frames")
    print(json.dumps(apply_super_storm(get_initial_city_state()), indent=2))
