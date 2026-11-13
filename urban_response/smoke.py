"""Live smoke test for the DGX Spark model servers (`urban-response smoke`).

Three stages, each fails loudly instead of letting the pipeline fall back to rules:
  1. reachability  every configured endpoint answers /models and serves the configured model name
  2. probe         one structured (guided JSON, thinking off) call per endpoint; checks the reply
                   parses, is correct, carries no <think> trace, and reports latency
  3. pipeline      one full incident through Qwen -> A1-A3 -> CR -> Qwen evaluate on a throwaway
                   DB; any stage tagged `fallback` fails the run

Exit code 0 only when every check passes.
"""

from __future__ import annotations

import asyncio
import json
import os
import tempfile
import time
from pathlib import Path

import httpx
from pydantic import BaseModel

from .config import ROOT, ModelEndpoint, Settings

OK, FAIL = "PASS", "FAIL"


class _Ping(BaseModel):
    answer: int
    unit: str


def _roles(s: Settings) -> dict[str, ModelEndpoint]:
    return {"Qwen (orchestrator)": s.orchestrator, "Nemotron (A1-A3)": s.specialist, "Nemotron (CR)": s.resolver}


def _unique(s: Settings) -> dict[tuple[str, str], list[str]]:
    eps: dict[tuple[str, str], list[str]] = {}
    for role, ep in _roles(s).items():
        eps.setdefault((ep.base_url, ep.model), []).append(role)
    return eps


async def _check_models(base_url: str, model: str, api_key: str) -> tuple[bool, str]:
    try:
        async with httpx.AsyncClient(timeout=5) as c:
            r = await c.get(f"{base_url.rstrip('/')}/models", headers={"Authorization": f"Bearer {api_key}"})
            r.raise_for_status()
            served = [m["id"] for m in r.json().get("data", [])]
    except Exception as e:  # noqa: BLE001
        return False, f"unreachable: {e}"
    if model not in served:
        return False, f"serves {served}, but config asks for {model!r}"
    return True, f"serves {model}"


async def _probe(ep: ModelEndpoint) -> tuple[bool, str]:
    from .llm import _complete_openai, extract_json

    system = ("You are a test probe. Respond with a single JSON object matching this schema:\n"
              + json.dumps(_Ping.model_json_schema()))
    t0 = time.perf_counter()
    try:
        raw = await _complete_openai(ep, system, [{"role": "user", "content": "A unit travels 12 km/h for 17 h. Distance? unit='km'."}], _Ping)
    except Exception as e:  # noqa: BLE001
        return False, f"call failed: {e}"
    ms = int((time.perf_counter() - t0) * 1000)
    notes = []
    if "<think>" in raw:
        notes.append("reply contains a <think> trace (thinking not disabled; check UR_DISABLE_THINKING / reasoning parser)")
    try:
        ping = _Ping.model_validate(extract_json(raw))
    except Exception as e:  # noqa: BLE001
        return False, f"{ms} ms, invalid JSON ({e}); raw={raw[:160]!r}"
    if ping.answer != 204:
        notes.append(f"wrong answer {ping.answer} (expected 204)")
    return (not notes), f"{ms} ms" + (f"; {'; '.join(notes)}" if notes else "")


async def _pipeline() -> tuple[bool, list[str]]:
    # Fresh DB so dedup can't merge the probe into a real incident and nothing pollutes the live log.
    tmp = Path(tempfile.mkdtemp(prefix="ur-smoke-")) / "smoke.db"
    os.environ["UR_DB_PATH"] = str(tmp)
    from .agents.orchestrator import Orchestrator
    from .config import get_settings
    from .db import Database
    from .schemas import ExecutedBy, Incident
    from .seed import seed

    settings = get_settings()
    db = Database(settings.db_path)
    lines: list[str] = []
    try:
        seed(db, force=True)
        report = Incident(**json.loads((ROOT / "scenarios" / "sheffield_demo.json").read_text())[0])
        t0 = time.perf_counter()
        result = await Orchestrator(db, settings).handle(report)
        total = time.perf_counter() - t0
        case = db.get_case(result.incident_id)
        tags = [("Qwen triage", case.triage.executed_by), ("Dedup", result.dedup.executed_by)]
        plan = result.response
        if plan:
            tags.append(("Qwen route", plan.routing.executed_by))
            tags += [(f"{r.agent_id} {r.agency.value}", r.executed_by) for r in plan.agent_responses]
            tags.append(("CR manifest", plan.cr_executed_by))
            tags += [(f"CR resolve {x.resource_type}", x.executed_by) for x in plan.hard_resolutions]
            if plan.evaluation:
                tags.append(("Qwen evaluate", plan.evaluation.executed_by))
        ok = result.outcome == "dispatched" and all(t != ExecutedBy.fallback for _, t in tags)
        lines.append(f"outcome={result.outcome}  total {total:.1f} s  (DB: {tmp})")
        lines += [f"  {'!!' if t == ExecutedBy.fallback else '  '} {name:<28} {t.value}" for name, t in tags]
        if plan:
            lines.append(f"  dispatched {len(plan.allocations)} vehicles, status={plan.status}")
        return ok, lines
    except Exception as e:  # noqa: BLE001
        return False, lines + [f"pipeline crashed: {e!r}"]
    finally:
        db.close()


async def run(settings: Settings, skip_pipeline: bool = False) -> bool:
    if not settings.live:
        print("LLM_MODE is not 'live'; nothing to smoke-test. Source deploy/dgx/.env first.")
        return False
    eps = _unique(settings)
    all_ok = True

    print("1. Reachability")
    for (url, model), roles in eps.items():
        ep = next(e for r, e in _roles(settings).items() if r == roles[0])
        if ep.provider != "openai":
            print(f"  skip  {', '.join(roles)}: provider {ep.provider}")
            continue
        ok, msg = await _check_models(url, model, ep.api_key)
        all_ok &= ok
        print(f"  {OK if ok else FAIL}  {', '.join(roles):<45} {url}  {msg}")
    if not all_ok:
        return False

    print("2. Structured probe (guided JSON, thinking off)")
    for (url, model), roles in eps.items():
        ep = next(e for r, e in _roles(settings).items() if r == roles[0])
        if ep.provider != "openai":
            continue
        ok, msg = await _probe(ep)
        all_ok &= ok
        print(f"  {OK if ok else FAIL}  {model:<45} {msg}")
    if not all_ok or skip_pipeline:
        return all_ok

    print("3. Full pipeline (sheffield_demo #0: RTC, trapped occupants, fuel leak)")
    ok, lines = await _pipeline()
    print(f"  {OK if ok else FAIL}")
    for line in lines:
        print(f"  {line}")
    return all_ok and ok


def main(settings: Settings, skip_pipeline: bool = False) -> int:
    return 0 if asyncio.run(run(settings, skip_pipeline)) else 1
