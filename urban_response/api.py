"""HTTP API and both UIs. Run with: urban-response serve  (then open http://localhost:8000).

  /          SURU Command demo dashboard (web/, by Severian0), fed by /ui/* (ui_adapter.py)
  /console   the engine console (static/index.html): raw pipeline stages, jobs, ledger, bursts
"""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
import logging
import os
from contextlib import asynccontextmanager
from dataclasses import replace
from pathlib import Path
from typing import Literal
from uuid import uuid4

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from pydantic import BaseModel

from .agents.orchestrator import Orchestrator
from .briefing import build_brief
from .config import ROOT, get_settings
from .db import Database, time_scale
from .schemas import Incident, IncidentStatus, PipelineResult
from .seed import seed
from .ui_adapter import EventFeed, make_router
from .world import GraphReportWorld, WorldConflict, WorldDatabase

STATIC = Path(__file__).parent / "static"
WEB = ROOT / "web"  # demo dashboard
WEB_ASSETS = {"app.js", "api.js", "mock.js", "style.css"}
state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    db = WorldDatabase(settings.db_path)
    seed(db)
    scenario = os.getenv("UR_WORLD_SCENARIO")
    if scenario:
        if db._conn.execute("SELECT COUNT(*) FROM allocations WHERE released_at IS NULL").fetchone()[0]:
            raise WorldConflict("Use a fresh demo database for world mode; active reservations cannot be reconstructed")
        db.graph_world = GraphReportWorld(db, scenario)
    state.update(
        settings=settings,
        db=db,
        orch=Orchestrator(db, settings),
        # A second orchestrator in mock mode so the console can run instantly without an LLM.
        orch_mock=Orchestrator(db, replace(settings, llm_mode="mock")),
        jobs={},
        feed=EventFeed(),
        world=db.graph_world,
        world_event=_world_event,
    )

    async def sweeper():
        """Autonomous unit return: release units and crews whose expected release time has passed."""
        while True:
            automatic = state.get("world") is not None and os.getenv("UR_WORLD_AUTORUN", "0") == "1"
            await asyncio.sleep(60 / time_scale() if automatic else 5)
            try:
                world = state.get("world")
                if world is not None and os.getenv("UR_WORLD_AUTORUN", "0") == "1" and not world.inflight:
                    await world.dispatch_due(state["orch"], _world_event)
                    world.tick()
                    released = world.last_released
                else:
                    released = db.release_due()
                if released:
                    state["feed"].units_returned(released)
            except Exception:  # keep sweeping, but surface failures instead of hiding them
                logging.getLogger(__name__).exception("Resource/world clock update failed")

    sweep = asyncio.create_task(sweeper())
    # Warm MiniLM in the background so the first report is not slowed by model loading.
    from . import dedup
    asyncio.create_task(asyncio.to_thread(dedup.load_model))
    yield
    sweep.cancel()
    db.close()


app = FastAPI(title="Sheffield Urban Response", version="0.3.0", lifespan=lifespan)


@app.exception_handler(WorldConflict)
async def world_conflict(request, error):
    return JSONResponse({"detail": str(error)}, status_code=409)


async def _world_event(incident_id, name, payload):
    state["feed"].on_job_event(incident_id, name, payload)
    job_id = f"world-{incident_id}"
    now = asyncio.get_running_loop().time()
    job = state["jobs"].setdefault(job_id, {"events":[], "done":False, "cond":asyncio.Condition(),
        "incident_id":incident_id, "label":f"Authored report {incident_id}", "started_at":now})
    async with job["cond"]:
        job["events"].append({"event":name, "t":round(now-job["started_at"],2), "data":payload})
        if name in ("completed","merged","error"):
            job["done"] = True
            job["outcome"] = "error" if name=="error" else "merged" if name=="merged" else "dispatched"
        job["cond"].notify_all()


@app.get("/world/state")
def world_state():
    world = state.get("world")
    if world is None:
        raise HTTPException(409, "Set UR_WORLD_SCENARIO and use a fresh demo database")
    return world.snapshot()


@app.post("/world/dispatch-due")
async def world_dispatch_due():
    world = state.get("world")
    if world is None:
        raise HTTPException(409, "World mode is not enabled")
    if world.inflight:
        raise WorldConflict("A model decision window is already open")
    return await world.dispatch_due(state["orch"], _world_event)


@app.post("/world/advance")
async def world_advance():
    world = state.get("world")
    if world is None:
        raise HTTPException(409, "World mode is not enabled")
    world.tick()
    state["feed"].units_returned(world.last_released)
    return world.snapshot()


# ---- Access control -------------------------------------------------------------- #
# The console is reachable two ways:
#   1. directly on the LAN (http://<spark>:8080), trusted, no token needed;
#   2. from the Vercel site through a tunnel (Cloudflare / Tailscale Funnel). Tunnels add
#      forwarding headers and connect from loopback, so anything proxied must carry
#      `Authorization: Bearer $UR_API_TOKEN`. The Vercel proxy adds it server-side.
# With UR_API_TOKEN unset everything is open (local development).

API_TOKEN = os.getenv("UR_API_TOKEN", "")
_FORWARD_HEADERS = ("cf-connecting-ip", "x-forwarded-for", "x-forwarded-host", "forwarded", "tailscale-funnel-request")
_OPEN_PATHS = {"/healthz"}


def _direct_lan_client(request: Request) -> bool:
    if any(h in request.headers for h in _FORWARD_HEADERS):
        return False
    try:
        ip = ipaddress.ip_address(request.client.host if request.client else "")
    except ValueError:
        return False
    return ip.is_loopback or ip.is_private


@app.middleware("http")
async def require_token(request: Request, call_next):
    if API_TOKEN and request.url.path not in _OPEN_PATHS and not _direct_lan_client(request):
        sent = request.headers.get("authorization", "")
        if not hmac.compare_digest(sent, f"Bearer {API_TOKEN}"):
            return JSONResponse({"detail": "Missing or invalid API token"}, status_code=401)
    return await call_next(request)


# Lets a UI served from another origin (e.g. web/ on a laptop, reaching the Spark over an SSH
# tunnel) call this API from the browser. Comma-separated origins; unset = no CORS headers.
# Added after require_token so it wraps it: preflight OPTIONS is answered before the token check.
if _cors := [o.strip() for o in os.getenv("UR_CORS_ORIGINS", "").split(",") if o.strip()]:
    from fastapi.middleware.cors import CORSMiddleware

    app.add_middleware(CORSMiddleware, allow_origins=_cors, allow_methods=["*"], allow_headers=["*"])


@app.get("/healthz", include_in_schema=False)
def healthz():
    """Unauthenticated liveness probe for the tunnel / Vercel status check. Reveals nothing."""
    return {"ok": True}


# ---- Console ------------------------------------------------------------------- #


@app.get("/", include_in_schema=False)
def dashboard():
    return FileResponse(WEB / "index.html")


@app.get("/config.js", include_in_schema=False)
def dashboard_config_js():
    """Point the dashboard at this server (web/config.js alone would start the in-browser mock)."""
    return PlainTextResponse("window.SURU_API_BASE = location.origin;", media_type="application/javascript")


@app.get("/scenarios/sheffield_demo.json", include_in_schema=False)
def dashboard_scenario():
    return FileResponse(WEB / "scenarios" / "sheffield_demo.json")


@app.get("/console", include_in_schema=False)
@app.get("/console/", include_in_schema=False)
def console():
    return FileResponse(STATIC / "index.html")


@app.get("/suru-config.js", include_in_schema=False)
@app.get("/console/suru-config.js", include_in_schema=False)
def console_config_js():
    """Served by FastAPI the console calls its own origin; the Vercel build overrides this file."""
    return PlainTextResponse('window.SURU_API_BASE = "";', media_type="application/javascript")


@app.get("/api/config")
def config():
    s = state["settings"]
    if not s.live:
        live_label = None
    elif s.specialist.provider == "claude-code":
        live_label = f"Claude Code stand-in / {s.specialist.model}"
    else:
        live_label = f"Qwen {s.orchestrator.model} · Nemotron {s.specialist.model}"
    return {"live_available": s.live, "live_label": live_label,
            "provider": s.specialist.provider, "auth_required": bool(API_TOKEN)}


@app.get("/api/scenarios")
def scenarios():
    out = json.loads((ROOT / "scenarios" / "sheffield_demo.json").read_text())
    out.insert(0, json.loads((ROOT / "scenarios" / "simple_rtc.json").read_text()))
    return out


@app.get("/api/state")
def console_state():
    db = state["db"]
    jobs = [{"job_id": k, "incident_id": j["incident_id"], "label": j["label"], "done": j["done"],
             "outcome": j.get("outcome")} for k, j in state["jobs"].items()]
    fleet = db.fleet()
    by_type: dict = {}
    for f in fleet:
        by_type.setdefault(f["resource_type"], {}).setdefault(f["stage"], 0)
        by_type[f["resource_type"]][f["stage"]] += 1
    inventory = [r | {"out_by_stage": by_type.get(r["resource_type"], {})} for r in db.inventory()]
    world = state.get("world")
    incidents = []
    for row in db.incidents():
        record = db.get_incident(row["incident_id"])
        caller = record["incident"]
        incidents.append({**row, "incident":caller, "triage":record["triage"],
                          "address":caller["location"].get("address") or caller["location"].get("landmark") or row["place"],
                          "district":"", "description":caller["description"],
                          "world_outcome":({"status":world.state["active_incidents"][world.case_nodes[row["incident_id"]]]["status"],
                              **world.evaluate_response(world.case_nodes[row["incident_id"]])}
                              if world is not None and row["incident_id"] in world.case_nodes else None)})
    return {"incidents": incidents, "inventory": inventory, "fleet": fleet, "personnel": db.personnel_inventory(),
            "stations": db.stations(), "jobs": jobs[-30:],
            "jobs_running": sum(1 for j in state["jobs"].values() if not j["done"]),
            "world":({"enabled":True, "tick":world.state["tick"], "inflight":world.inflight} if world is not None else {"enabled":False}),
            "time_scale": float(os.getenv("UR_TIME_SCALE", "1"))}


@app.get("/api/personnel")
def personnel():
    return state["db"].personnel_inventory()


@app.get("/api/ledger")
def ledger(limit: int = 100, incident_id: int | None = None):
    return state["db"].ledger(limit, incident_id)


@app.get("/api/brief/{incident_id}")
def brief(incident_id: int):
    """Normalised, phone-sized incident brief: headline, facts, orders, units with crews, log."""
    b = build_brief(state["db"], incident_id)
    if not b:
        raise HTTPException(404)
    return b


class RunRequest(BaseModel):
    incident: Incident
    mode: Literal["live", "mock"] = "live"


class BurstRequest(BaseModel):
    incidents: list[Incident]
    mode: Literal["live", "mock"] = "live"
    stagger_s: float = 1.5  # callers rarely ring in the same instant


def _bursts() -> dict[str, list]:
    out = {}
    for path in sorted((ROOT / "scenarios").glob("burst_*.json")):
        out[path.stem.removeprefix("burst_").replace("_", " ").title()] = json.loads(path.read_text())
    return out


@app.get("/api/burst")
def list_bursts():
    """Prepared sets of near-simultaneous reports around one location (duplicates + a separate incident)."""
    return _bursts()


@app.post("/api/burst")
async def run_burst(req: BurstRequest):
    """Fire several reports concurrently; each gets its own job and event stream."""
    if req.mode == "live" and not state["settings"].live:
        raise HTTPException(400, "Server was started without LLM_MODE=live; use mock")
    started = []
    for n, inc in enumerate(req.incidents):
        if n and req.stagger_s:
            await asyncio.sleep(req.stagger_s)
        started.append(await _start_job(inc, req.mode))
    return {"jobs": started}


@app.post("/api/run")
async def run(req: RunRequest):
    """Start the pipeline in the background; stream progress from /api/jobs/{id}/events."""
    if req.mode == "live" and not state["settings"].live:
        raise HTTPException(400, "Server was started without LLM_MODE=live; use mock or restart with a model configured")
    if state["db"].get_incident(req.incident.incident_id):
        raise HTTPException(409, f"{req.incident.incident_id} already exists")
    return await _start_job(req.incident, req.mode)


async def _start_job(incident: Incident, mode: str) -> dict:
    orch = state["orch"] if mode == "live" else state["orch_mock"]
    job_id = uuid4().hex[:10]
    where = incident.location.address or incident.location.landmark or "unknown location"
    label = f"{incident.channel} · {where}"
    job = {"events": [], "done": False, "cond": asyncio.Condition(), "incident_id": incident.incident_id, "label": label}
    state["jobs"][job_id] = job
    loop = asyncio.get_running_loop()
    t0 = loop.time()

    async def on_event(name: str, payload: dict) -> None:
        if name == "ingested":  # the database assigns the numeric id at intake
            job["incident_id"] = payload["incident"]["incident_id"]
        try:
            state["feed"].on_job_event(job["incident_id"], name, payload)
        except Exception:  # the dashboard feed must never break a pipeline run
            pass
        async with job["cond"]:
            job["events"].append({"event": name, "t": round(loop.time() - t0, 2), "data": payload})
            job["cond"].notify_all()

    async def runner():
        try:
            result = await orch.handle(incident, on_event=on_event)
            job["outcome"] = result.outcome
        except Exception as e:  # surface to the console instead of dying silently
            job["outcome"] = "error"
            await on_event("error", {"message": f"{type(e).__name__}: {e}"})
        finally:
            async with job["cond"]:
                job["done"] = True
                job["cond"].notify_all()

    job["task"] = asyncio.create_task(runner())
    return {"job_id": job_id, "incident_id": incident.incident_id, "label": label}


@app.get("/api/jobs/{job_id}/events")
async def job_events(job_id: str):
    job = state["jobs"].get(job_id)
    if not job:
        raise HTTPException(404)

    async def stream():
        sent = 0
        while True:
            async with job["cond"]:
                while sent >= len(job["events"]) and not job["done"]:
                    await job["cond"].wait()
                pending = job["events"][sent:]
                done = job["done"]
            for ev in pending:
                yield f"event: {ev['event']}\ndata: {json.dumps(ev, default=str)}\n\n"
            sent += len(pending)
            if done and sent >= len(job["events"]):
                yield "event: end\ndata: {}\n\n"
                return

    return StreamingResponse(stream(), media_type="text/event-stream", headers={"Cache-Control": "no-cache"})


@app.post("/api/reset")
def reset():
    if any(not j["done"] for j in state["jobs"].values()):
        raise HTTPException(409, "Wait for running incidents to finish")
    if state.get("world") is not None and state["world"].inflight:
        raise WorldConflict("Finish model decisions before resetting")
    seed(state["db"], force=True)
    if state.get("world") is not None:
        state["db"].graph_world = GraphReportWorld(state["db"], os.environ["UR_WORLD_SCENARIO"])
        state["world"] = state["db"].graph_world
    state["jobs"].clear()
    state["feed"].clear()
    return {"ok": True}


app.include_router(make_router(state, _start_job, reset))


# ---- Incident DB ---------------------------------------------------------------- #


@app.get("/health")
def health():
    s = state["settings"]
    return {
        "mode": s.llm_mode,
        "orchestrator_qwen": {"base_url": s.orchestrator.base_url, "model": s.orchestrator.model},
        "specialists_a1_a3": {"base_url": s.specialist.base_url, "model": s.specialist.model},
        "conflict_resolver_cr": {"base_url": s.resolver.base_url, "model": s.resolver.model},
    }


@app.post("/incidents", response_model=PipelineResult)
async def create_incident(incident: Incident):
    """Run the full loop (ingest, dedup, route, agents, CR dispatch, evaluation) and return the result."""
    if state["db"].get_incident(incident.incident_id):
        raise HTTPException(409, f"{incident.incident_id} already exists")
    return await state["orch"].handle(incident)


@app.get("/incidents")
def list_incidents(status: IncidentStatus | None = None):
    return state["db"].incidents(status)


@app.get("/backlog")
def backlog():
    """Incidents whose response has been dispatched."""
    return state["db"].incidents(IncidentStatus.backlog)


@app.get("/incidents/{incident_id}")
def get_incident(incident_id: int):
    inc = state["db"].get_incident(incident_id)
    if not inc:
        raise HTTPException(404)
    return inc


@app.get("/incidents/{incident_id}/responses")
def incident_responses(incident_id: int):
    if not state["db"].get_incident(incident_id):
        raise HTTPException(404)
    return state["db"].responses(incident_id)


@app.post("/incidents/{incident_id}/close")
def close_incident(incident_id: int):
    if not state["db"].get_incident(incident_id):
        raise HTTPException(404)
    return {"units_released": state["db"].close_incident(incident_id)}


# ---- Response DB ---------------------------------------------------------------- #


@app.get("/responses")
def list_responses():
    return state["db"].responses()


@app.get("/responses/{response_id}")
def get_response(response_id: int):
    r = state["db"].get_response(response_id)
    if not r:
        raise HTTPException(404)
    return r


@app.get("/conflicts")
def conflicts(incident_id: int | None = None, response_id: int | None = None):
    return state["db"].conflict_log(incident_id, response_id)


# ---- Resource DB ---------------------------------------------------------------- #


@app.post("/units/{unit_id}/release")
def release_unit(unit_id: str):
    return {"released": state["db"].release_unit(unit_id)}


@app.get("/resources")
def resources():
    return state["db"].inventory()


@app.get("/resources/{resource_type}/forecast")
def forecast(resource_type: str, horizon_min: int = 120):
    if resource_type not in state["db"].resource_types():
        raise HTTPException(404)
    return state["db"].availability_forecast(resource_type, horizon_min)


# Registered last so it never shadows an API route.
@app.get("/{asset}", include_in_schema=False)
def dashboard_asset(asset: str):
    if asset not in WEB_ASSETS:
        raise HTTPException(404)
    return FileResponse(WEB / asset)
