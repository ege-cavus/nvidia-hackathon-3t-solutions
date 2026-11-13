"""HTTP API and live console. Run with: urban-response serve  (then open http://localhost:8000)."""

from __future__ import annotations

import asyncio
import hmac
import ipaddress
import json
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
from .db import Database
from .schemas import Incident, IncidentStatus, PipelineResult
from .seed import seed

STATIC = Path(__file__).parent / "static"
state: dict = {}


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    db = Database(settings.db_path)
    seed(db)
    state.update(
        settings=settings,
        db=db,
        orch=Orchestrator(db, settings),
        # A second orchestrator in mock mode so the console can run instantly without an LLM.
        orch_mock=Orchestrator(db, replace(settings, llm_mode="mock")),
        jobs={},
    )

    async def sweeper():
        """Autonomous unit return: release units and crews whose expected release time has passed."""
        while True:
            await asyncio.sleep(5)
            try:
                db.release_due()
            except Exception:  # keep sweeping even if one pass fails
                pass

    sweep = asyncio.create_task(sweeper())
    yield
    sweep.cancel()
    db.close()


app = FastAPI(title="Sheffield Urban Response", version="0.3.0", lifespan=lifespan)


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


@app.get("/healthz", include_in_schema=False)
def healthz():
    """Unauthenticated liveness probe for the tunnel / Vercel status check. Reveals nothing."""
    return {"ok": True}


# ---- Console ------------------------------------------------------------------- #


@app.get("/", include_in_schema=False)
def console():
    return FileResponse(STATIC / "index.html")


@app.get("/suru-config.js", include_in_schema=False)
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
    return {"incidents": db.incidents(), "inventory": db.inventory(), "personnel": db.personnel_inventory(),
            "stations": db.stations(), "jobs": jobs[-30:],
            "jobs_running": sum(1 for j in state["jobs"].values() if not j["done"]),
            "time_scale": float(os.getenv("UR_TIME_SCALE", "1"))}


@app.get("/api/personnel")
def personnel():
    return state["db"].personnel_inventory()


@app.get("/api/ledger")
def ledger(limit: int = 100, incident_id: str | None = None):
    return state["db"].ledger(limit, incident_id)


@app.get("/api/brief/{incident_id}")
def brief(incident_id: str):
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
    label = f"{incident.category.replace('_', ' ')} · {incident.location.address or incident.incident_id}"
    job = {"events": [], "done": False, "cond": asyncio.Condition(), "incident_id": incident.incident_id, "label": label}
    state["jobs"][job_id] = job
    loop = asyncio.get_running_loop()
    t0 = loop.time()

    async def on_event(name: str, payload: dict) -> None:
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
    seed(state["db"], force=True)
    state["jobs"].clear()
    return {"ok": True}


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
def get_incident(incident_id: str):
    inc = state["db"].get_incident(incident_id)
    if not inc:
        raise HTTPException(404)
    return inc


@app.get("/incidents/{incident_id}/responses")
def incident_responses(incident_id: str):
    if not state["db"].get_incident(incident_id):
        raise HTTPException(404)
    return state["db"].responses(incident_id)


@app.post("/incidents/{incident_id}/close")
def close_incident(incident_id: str):
    if not state["db"].get_incident(incident_id):
        raise HTTPException(404)
    return {"units_released": state["db"].close_incident(incident_id)}


# ---- Response DB ---------------------------------------------------------------- #


@app.get("/responses")
def list_responses():
    return state["db"].responses()


@app.get("/responses/{response_id}")
def get_response(response_id: str):
    r = state["db"].get_response(response_id)
    if not r:
        raise HTTPException(404)
    return r


@app.get("/conflicts")
def conflicts(incident_id: str | None = None, response_id: str | None = None):
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
