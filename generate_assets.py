"""Track 4: local CCTV GeoJSON, video preparation and deterministic storm demo.

Supply the council's 212-camera GeoJSON locally; no dataset is bundled or invented.
"""
import argparse
import json
import math
import random
from pathlib import Path
from state_schema import HERO_CAMERAS, get_initial_city_state, make_incident
from knowledge_base import NODE_HISTORICAL_PROBABILITIES


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
        lon, lat = geometry["coordinates"][:2]
        if any(type(v) not in (int, float) or not math.isfinite(v) for v in (lon, lat)) or not (-180 <= lon <= 180 and -90 <= lat <= 90):
            raise ValueError("Invalid longitude/latitude")
    return data


def prepare_video(source, destination, max_frames=300, fps=10.0):
    import cv2
    if Path(source).resolve() == Path(destination).resolve():
        raise ValueError("Video source and destination must differ")
    if max_frames <= 0 or fps <= 0:
        raise ValueError("max_frames and fps must be positive")
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


def extract_frame(video_path, output_path):
    import cv2
    capture = cv2.VideoCapture(str(video_path))
    try:
        ok, frame = capture.read()
        if not ok:
            raise ValueError(f"No readable frame in {video_path}")
        Path(output_path).parent.mkdir(parents=True, exist_ok=True)
        if not cv2.imwrite(str(output_path), frame):
            raise ValueError(f"Could not write {output_path}")
    finally:
        capture.release()


def prepare_hero_assets():
    """Prepare frames for supplied Cosmos MP4s; never claim missing videos exist."""
    results = {}
    for cam, details in HERO_CAMERAS.items():
        path = Path(details["cosmos_video_path"])
        if path.is_file():
            extract_frame(path, details["frame_path"])
            results[cam] = details["frame_path"]
        else:
            results[cam] = "MISSING_LOCAL_COSMOS_VIDEO"
    return results


def apply_super_storm(state: dict, seed=42) -> dict:
    """Mutate the supplied state with repeatable synthetic incidents."""
    rng = random.Random(seed)
    for cam in HERO_CAMERAS:
        weights = NODE_HISTORICAL_PROBABILITIES[cam]["hazard_weights"]
        hazard = rng.choices(list(weights), weights=list(weights.values()))[0]
        state["active_incidents"][cam] = make_incident(
            cam, hazard, rng.randint(4, 10), state["tick"], rng.choice([True, False]))
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-heroes", action="store_true")
    parser.add_argument("--geojson", type=Path)
    parser.add_argument("--source", type=Path)
    parser.add_argument("--destination", type=Path)
    args = parser.parse_args()
    if bool(args.source) != bool(args.destination):
        parser.error("--source and --destination must be provided together")
    if args.prepare_heroes:
        print(json.dumps(prepare_hero_assets(), indent=2))
    if args.geojson:
        print(f"Loaded {len(load_cctv_geojson(args.geojson)['features'])} cameras")
    if args.source:
        print(f"Prepared {prepare_video(args.source, args.destination)} frames")
    print(json.dumps(apply_super_storm(get_initial_city_state()), indent=2))
