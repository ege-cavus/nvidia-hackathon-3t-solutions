"""Track 3: JSON API for the Vercel dashboard (web/). Runs on the demo host beside Ollama.

python web_api.py --port 8000   then expose it over HTTPS, e.g. cloudflared tunnel --url http://localhost:8000
Standard library only. One shared Seed-42 simulation per process. Simulated output, never operational advice.
"""
import argparse
import asyncio
import json
import os
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from agent_orchestrator import process_pending
from knowledge_base import SOURCES
from sheffield_sim import SheffieldSim
from state_schema import EMERGENCY_HUBS, HERO_CAMERAS, get_initial_city_state

ALLOWED_ORIGIN = os.environ.get("SURU_ALLOWED_ORIGIN", "*")
MEDIA_TYPES = {".mp4": "video/mp4", ".jpg": "image/jpeg"}
_lock = threading.Lock()
_sim = None


def new_sim():
    return SheffieldSim(get_initial_city_state(), seed=42, demo=True)


def roads_geojson(graph):
    lines = []
    for u, v, data in graph.edges(data=True):
        geometry = data.get("geometry")
        lines.append([list(c) for c in geometry.coords] if geometry is not None else
                     [[graph.nodes[u]["x"], graph.nodes[u]["y"]], [graph.nodes[v]["x"], graph.nodes[v]["y"]]])
    return {"type": "Feature", "properties": {}, "geometry": {"type": "MultiLineString", "coordinates": lines}}


def snapshot(sim):
    return {"state": sim.state, "hubs": EMERGENCY_HUBS,
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
        with _lock:
            if path == "/api/state":
                return self._json(snapshot(_sim))
            if path == "/api/roads":
                return self._json({"mode": _sim.state["routing"]["mode"], "roads": roads_geojson(_sim.graph)})
        self._json({"error": "Not found"}, 404)

    def do_POST(self):
        global _sim
        url = urlparse(self.path)
        live = parse_qs(url.query).get("live") == ["1"]
        with _lock:
            try:
                if url.path == "/api/reset":
                    _sim = new_sim()
                elif url.path == "/api/tick":
                    _sim.tick()
                    asyncio.run(process_pending(_sim.state, sim=_sim, live=live))
                elif url.path == "/api/retry":
                    asyncio.run(process_pending(_sim.state, sim=_sim, live=live))
                else:
                    return self._json({"error": "Not found"}, 404)
            except Exception as error:  # Keep the server up; the board already records deferrals.
                return self._json({"error": f"{type(error).__name__}: {error}"}, 500)
            return self._json(snapshot(_sim))


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
