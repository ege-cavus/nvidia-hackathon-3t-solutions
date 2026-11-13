"""Track 3: JSON API for the Vercel dashboard (web/). Runs on the demo host beside Ollama.

python web_api.py --port 8000   then expose it over HTTPS, e.g. cloudflared tunnel --url http://localhost:8000
Standard library only. One shared Seed-42 simulation per process. Simulated output, never operational advice.
"""
import argparse
import asyncio
import json
import os
import platform
import subprocess
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
import psutil
from agent_orchestrator import NEMOTRON_MODEL, QWEN_MODEL, process_pending
from knowledge_base import SOURCES
from sheffield_sim import SheffieldSim
from state_schema import EMERGENCY_HUBS, HERO_CAMERAS, get_initial_city_state

ALLOWED_ORIGIN = os.environ.get("SURU_ALLOWED_ORIGIN", "*")
OLLAMA_HOST = os.environ.get("OLLAMA_HOST", "http://127.0.0.1:11434").rstrip("/")
MEDIA_TYPES = {".mp4": "video/mp4", ".jpg": "image/jpeg"}
GPU_FIELDS = ("name", "utilization.gpu", "memory.used", "memory.total", "power.draw", "temperature.gpu")
_lock = threading.Lock()  # held by the pipeline worker while it mutates the simulation
_start_lock = threading.Lock()
_sim = None
_pipeline = {"running": False, "started": None, "last_seconds": None, "live": False, "error": None}


def new_sim():
    return SheffieldSim(get_initial_city_state(), seed=42, demo=True)


def roads_geojson(graph):
    lines = []
    for u, v, data in graph.edges(data=True):
        geometry = data.get("geometry")
        lines.append([list(c) for c in geometry.coords] if geometry is not None else
                     [[graph.nodes[u]["x"], graph.nodes[u]["y"]], [graph.nodes[v]["x"], graph.nodes[v]["y"]]])
    return {"type": "Feature", "properties": {}, "geometry": {"type": "MultiLineString", "coordinates": lines}}


def pipeline_status():
    status = dict(_pipeline)
    started = status.pop("started")
    status["elapsed_seconds"] = round(time.monotonic() - started, 1) if status["running"] else None
    return status


def _number(value):
    try:
        return float(value)
    except ValueError:  # nvidia-smi prints [N/A] for fields a device does not report (e.g. GB10 unified memory)
        return None


def gpu_telemetry():
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=" + ",".join(GPU_FIELDS), "--format=csv,noheader,nounits"],
                             capture_output=True, text=True, timeout=2, check=True).stdout
    except (OSError, subprocess.SubprocessError) as error:
        return {"available": False, "reason": type(error).__name__}
    gpus = []
    for line in out.strip().splitlines():
        name, util, used, total, power, temp = [part.strip() for part in line.split(",")]
        gpus.append({"name": name, "util_pct": _number(util), "mem_used_mb": _number(used), "mem_total_mb": _number(total),
                     "power_w": _number(power), "temp_c": _number(temp)})
    return {"available": bool(gpus), "gpus": gpus}


def ollama_models():
    try:
        with urllib.request.urlopen(OLLAMA_HOST + "/api/ps", timeout=1) as response:
            models = json.load(response).get("models", [])
    except (OSError, ValueError) as error:
        return {"reachable": False, "host": OLLAMA_HOST, "reason": type(error).__name__}
    return {"reachable": True, "host": OLLAMA_HOST, "models": [
        {"name": m.get("name"), "size_gb": round(m.get("size", 0) / 1e9, 1), "vram_gb": round(m.get("size_vram", 0) / 1e9, 1),
         "expires_at": m.get("expires_at")} for m in models]}


def telemetry():
    memory = psutil.virtual_memory()
    return {"host": platform.node(), "gpu": gpu_telemetry(), "cpu_pct": psutil.cpu_percent(interval=None),
            "memory": {"used_gb": round(memory.used / 2**30, 1), "total_gb": round(memory.total / 2**30, 1), "pct": memory.percent},
            "ollama": ollama_models(), "pipeline": pipeline_status(),
            "models": {"qwen": QWEN_MODEL, "nemotron": NEMOTRON_MODEL}}


def run_pipeline(advance, live):
    """Worker thread: one tick and/or agent round. Readers poll /api/state meanwhile."""
    started = time.monotonic()
    try:
        with _lock:
            if advance:
                _sim.tick()
            asyncio.run(process_pending(_sim.state, sim=_sim, live=live))
        _pipeline["error"] = None
    except Exception as error:  # Keep the server up; the board already records deferrals.
        _pipeline["error"] = f"{type(error).__name__}: {error}"
    finally:
        _pipeline.update(running=False, last_seconds=round(time.monotonic() - started, 2))


def snapshot(sim):
    return {"state": sim.state, "hubs": EMERGENCY_HUBS, "pipeline": pipeline_status(),
            "cameras": {cam: {k: c[k] for k in ("name", "lat", "lon")} | {
                "video": Path(c["cosmos_video_path"]).is_file(), "frame": Path(c["frame_path"]).is_file()}
                for cam, c in HERO_CAMERAS.items()},
            "sources": SOURCES}


class Handler(BaseHTTPRequestHandler):
    def _send(self, status, body=b"", content_type="application/json"):
        self.send_response(status)
        self.send_header("Access-Control-Allow-Origin", ALLOWED_ORIGIN)
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _json(self, payload, status=200):
        self._send(status, json.dumps(payload).encode())

    def do_OPTIONS(self):
        self._send(204)

    def do_GET(self):
        global _sim
        path = urlparse(self.path).path
        if path == "/api/health":
            return self._json({"ok": True})
        if path.startswith("/api/media/"):
            # Whitelisted hero assets only; no user-supplied filesystem paths.
            name = path.rsplit("/", 1)[-1]
            cam, ext = os.path.splitext(name)
            if cam in HERO_CAMERAS and ext in MEDIA_TYPES:
                file = Path(HERO_CAMERAS[cam]["cosmos_video_path" if ext == ".mp4" else "frame_path"])
                if file.is_file():
                    # shortcut: whole-file response without Range support; fine for short Cosmos clips, add Range if Safari seeking matters.
                    return self._send(200, file.read_bytes(), MEDIA_TYPES[ext])
            return self._json({"error": "Asset not found"}, 404)
        if path == "/api/state":
            return self._state()
        if path == "/api/telemetry":
            return self._json(telemetry())
        if path == "/api/roads":
            return self._json({"mode": _sim.state["routing"]["mode"], "roads": roads_geojson(_sim.graph)})
        self._json({"error": "Not found"}, 404)

    def _state(self):
        # Readers do not wait for a running pipeline, so a model round can be watched live.
        # shortcut: retry if the worker mutates the state mid-serialisation; switch to versioned snapshots if this ever fails 3 times.
        for _ in range(3):
            try:
                return self._send(200, json.dumps(snapshot(_sim)).encode())
            except RuntimeError:
                time.sleep(0.05)
        self._json({"error": "State busy, retry"}, 503)

    def do_POST(self):
        global _sim
        url = urlparse(self.path)
        live = parse_qs(url.query).get("live") == ["1"]
        if url.path not in ("/api/reset", "/api/tick", "/api/retry"):
            return self._json({"error": "Not found"}, 404)
        with _start_lock:  # check-and-start must be atomic across request threads
            if _pipeline["running"]:
                return self._json({"error": "Agents are still running this tick"}, 409)
            if url.path == "/api/reset":
                with _lock:
                    _sim = new_sim()
                _pipeline.update(last_seconds=None, error=None)
            else:
                _pipeline.update(running=True, started=time.monotonic(), live=live)
                threading.Thread(target=run_pipeline, args=(url.path == "/api/tick", live), daemon=True).start()
        self._state()


def main():
    global _sim
    parser = argparse.ArgumentParser(description="SURU dashboard API")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args()
    _sim = new_sim()
    print(f"SURU API on http://{args.host}:{args.port} (graph: {_sim.state['routing']['mode']}, CORS origin: {ALLOWED_ORIGIN})")
    ThreadingHTTPServer((args.host, args.port), Handler).serve_forever()


if __name__ == "__main__":
    main()
