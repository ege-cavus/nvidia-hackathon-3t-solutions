"""Render a short reference-conditioned Cosmos clip and extract review frames.

Outputs are review candidates, never automatically promoted to incident media.
"""
import argparse
import base64
from contextlib import contextmanager
import hashlib
from io import BytesIO
import json
import math
from pathlib import Path
import shutil
import urllib.request
from uuid import uuid4

import cv2
from PIL import Image

from cosmos_client import ROOT, image_request

DEFAULT_CONFIG = ROOT / "assets/reference_views/park_square_flood_trial.json"


@contextmanager
def _staging_directory(directory):
    # Inherit workspace permissions. Python's private Windows temp-directory ACL
    # would otherwise follow the renamed outputs and hide them from the user.
    staging = directory / (".render-" + uuid4().hex)
    staging.mkdir()
    try:
        yield staging
    finally:
        shutil.rmtree(staging)


def video_request(item, reference, seed=42, resolution="480_16_9", num_frames=49, fps=12):
    """Build the I2V request; validate locally before waking the GPU service."""
    payload = image_request(item, seed, resolution)
    tier = resolution.split("_")[0]
    if tier not in {"256", "480", "720"}:
        raise ValueError("Unsupported resolution tier")
    if resolution not in {tier, *(tier + suffix for suffix in ("_16_9", "_1_1", "_9_16", "_4_3", "_3_4"))}:
        raise ValueError("Unsupported resolution aspect")
    if type(num_frames) is not int or not 25 <= num_frames <= {"256": 397, "480": 297, "720": 197}[tier]:
        raise ValueError("Frame count exceeds the resolution tier's supported range")
    if type(fps) not in (int, float) or not math.isfinite(fps) or not 1 <= fps <= 60:
        raise ValueError("FPS must be between 1 and 60")
    data = Path(reference).read_bytes()
    with Image.open(BytesIO(data)) as image:
        mime = {"JPEG": "image/jpeg", "PNG": "image/png", "WEBP": "image/webp"}.get(image.format)
        if mime is None:
            raise ValueError("Reference must be JPEG, PNG or WebP")
        image.verify()
    encoded = base64.b64encode(data).decode("ascii")
    if len(encoded) > 20_000_000:
        raise ValueError("Reference exceeds Cosmos's encoded image limit")
    payload.update(model_mode="image2video", input_reference=f"data:{mime};base64,{encoded}",
                   num_frames=num_frames, fps=fps)
    return payload


def _decode_frames(path, expected_count):
    """Decode the entire short clip, keeping first/middle/last frames for review."""
    capture = cv2.VideoCapture(str(path))
    selected = {}
    indices = {0, expected_count // 2, expected_count - 1}
    count = 0
    try:
        while capture.isOpened():
            ok, frame = capture.read()
            if not ok:
                break
            if count in indices:
                selected[count] = frame
            count += 1
    finally:
        capture.release()
    if count != expected_count:
        raise ValueError(f"Expected {expected_count} decodable frames, received {count}")
    return selected


def generate_reference_clip(config, endpoint="http://127.0.0.1:8000", output_dir=None,
                            overwrite=False, timeout=1800):
    reference = ROOT / config["reference_image"]
    payload = video_request(config, reference, config.get("seed", 42),
                            config.get("resolution", "480_16_9"),
                            config.get("num_frames", 49), config.get("fps", 12))
    directory = Path(output_dir) if output_dir is not None else ROOT / "demo_output/cosmos_reference"
    destination = directory / f"{config['incident_id']}-reference.mp4"
    metadata_path = destination.with_suffix(".json")
    if not overwrite and (destination.exists() or metadata_path.exists()):
        return {"status": "SKIPPED_EXISTING", "path": str(destination)}
    with urllib.request.urlopen(endpoint.rstrip("/") + "/v1/health/ready", timeout=10) as response:
        if response.status != 200:
            raise RuntimeError("Cosmos Generator is not ready")
    request = urllib.request.Request(endpoint.rstrip("/") + "/v1/infer", json.dumps(payload).encode(),
                                     {"Content-Type": "application/json"})
    # A quiet synchronous request may still be computing. Never retry automatically.
    with urllib.request.urlopen(request, timeout=timeout) as response:
        result = json.load(response)
    encoded = result.get("b64_video")
    if not isinstance(encoded, str):
        raise ValueError("Generator did not return b64_video")
    data = base64.b64decode(encoded, validate=True)
    directory.mkdir(parents=True, exist_ok=True)
    # Only publish decoded output; temporary files are cleaned up even on failure.
    with _staging_directory(directory) as staging:
        temporary = Path(staging) / "candidate.mp4"
        temporary.write_bytes(data)
        frames = _decode_frames(temporary, payload["num_frames"])
        frame_paths = []
        for index, frame in sorted(frames.items()):
            frame_name = f"{destination.stem}-frame-{index:03d}.jpg"
            if not cv2.imwrite(str(Path(staging) / frame_name), frame):
                raise ValueError("Unable to save review frame")
            frame_paths.append(frame_name)
        record = {
            "incident_id": config["incident_id"], "status": "GENERATED_PENDING_REVIEW",
            "provenance": "SYNTHETIC_REFERENCE_CONDITIONED_NOT_REAL_CCTV",
            "reference_image": config["reference_image"],
            "reference_sha256": hashlib.sha256(reference.read_bytes()).hexdigest(),
            "attribution": config.get("attribution"),
            "request": {k: v for k, v in payload.items() if k != "input_reference"},
            "video": destination.name, "video_sha256": hashlib.sha256(data).hexdigest(),
            "bytes": len(data), "frames": frame_paths,
            "review_criteria": config.get("review_criteria", []),
            "review": {"status": "PENDING", "approved_for_qwen": False},
        }
        for name in frame_paths:
            (Path(staging) / name).replace(directory / name)
        temporary.replace(destination)
        staged_metadata = Path(staging) / "metadata.json"
        staged_metadata.write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        staged_metadata.replace(metadata_path)
    return {"status": record["status"], "path": str(destination), "metadata": str(metadata_path),
            "frames": [str(directory / name) for name in frame_paths]}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("--endpoint", default="http://127.0.0.1:8000")
    parser.add_argument("--output-dir", type=Path)
    parser.add_argument("--overwrite", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()
    config = json.loads(args.config.read_text(encoding="utf-8"))
    if args.dry_run:
        payload = video_request(config, ROOT / config["reference_image"], config.get("seed", 42),
                                config.get("resolution", "480_16_9"),
                                config.get("num_frames", 49), config.get("fps", 12))
        payload["input_reference"] = "<validated local image; base64 omitted>"
        print(json.dumps(payload, indent=2))
    else:
        print("Submitting one reference-conditioned request; no automatic retries.", flush=True)
        print(json.dumps(generate_reference_clip(config, args.endpoint, args.output_dir, args.overwrite)), flush=True)
