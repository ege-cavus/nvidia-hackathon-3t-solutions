"""Command line entry point.

  urban-response demo [--reset]     run the Sheffield demo scenarios in sequence
  urban-response incident FILE      run one incident (JSON) through the pipeline
  urban-response burst FILE         send a list of reports concurrently (duplicates get merged)
  urban-response resources          show Resource DB inventory
  urban-response incidents          show the incident DB (pending / backlog / closed)
  urban-response seed [--force]     (re)seed the Resource DB
  urban-response serve              start the HTTP API
  urban-response trace FILE         print a readable transcript of a --trace file
  urban-response prefetch           cache the MiniLM dedup model (then HF_HUB_OFFLINE=1 works)
  urban-response smoke [--quick]    live check of the DGX Spark model servers (fails on any fallback)
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
from pathlib import Path

from .agents.orchestrator import Orchestrator
from .config import ROOT, get_settings
from .db import Database
from .briefing import build_brief
from .schemas import Incident, IncidentStatus, PipelineResult
from .seed import seed


def print_result(db: Database, report: Incident, result: PipelineResult) -> None:
    incident = db.get_case(result.incident_id)
    t = incident.triage
    print(f"\n{'=' * 78}\n{incident.incident_id}  [{report.channel}] {report.description[:70]}")
    print(f"Triage [{t.executed_by.value}]: {t.category} sev {t.severity} at {t.place} ({t.location_resolved_by})")
    d = result.dedup
    print(f"Dedup [{d.executed_by.value}]: {d.verdict.value}" + (f" -> {d.match_incident_id}" if d.match_incident_id else "") + f"  {d.reason}")
    if result.outcome == "merged":
        print("  merged; nothing dispatched")
        return
    plan = result.response
    r = plan.routing
    print(f"Route [{r.executed_by.value}]: {', '.join(a.value for a in r.agencies)}  (lead: {r.lead_agency.value})")
    for resp in plan.agent_responses:
        reqs = ", ".join(f"{q.quantity}x {q.resource_type}(p{q.priority.value})" for q in resp.resource_requests)
        print(f"  {resp.agent_id} {resp.agency.value:<9} [{resp.executed_by.value}] {reqs}")
    print(f"CR manifest [{plan.cr_executed_by.value}]: requested {sum(plan.requested_total.values())} -> declared {sum(m.quantity for m in plan.manifest)}")
    for m in plan.manifest:
        print(f"  {m.quantity}x {m.resource_type:<24} for {m.for_agency.value:<9} p{m.priority.value}  {m.reason}")
    for n in plan.manifest_adjustments:
        print(f"  ! {n}")
    for c in plan.hard_conflicts:
        print(f"  conflict {c.kind.value:<15} {c.detail}")
    for res in plan.hard_resolutions:
        print(f"  resolve {res.resource_type}: {res.action.value} [{res.executed_by.value}]")
    print(f"Dispatched {len(plan.allocations)} vehicles, {sum(len(a.crew) for a in plan.allocations)} personnel  status={plan.status}")
    for a in plan.allocations:
        pre = f"  (from {a.preempted_from})" if a.preempted_from else ""
        crew = ", ".join(f"{m.speciality}" for m in a.crew)
        print(f"  {a.callsign:<16} {a.resource_type:<24} ETA {a.eta_min:>5.1f}  crew: {crew}{pre}")
    for u in plan.unmet:
        print(f"  GAP {u['quantity']}x {u['resource_type']} for {u['requested_by']} ({u['reason']})")
    e = plan.evaluation
    if e:
        print(f"Evaluation [{e.executed_by.value}]: {e.score}/100 ({e.grade}) {e.verdict}")
        for f in e.findings:
            print(f"  - {f}")
    brief = build_brief(db, incident.incident_id)
    print(f"\n  {brief['headline'].upper()}\n  {brief['dek']}")
    for agency, orders in brief["orders"].items():
        for o in orders:
            print(f"  [{agency}] {o['action']}")
    print("  LOG")
    for line in brief["log"]:
        print(f"  {line['ts'][11:19]}  {line['text']}")


def print_trace(path: Path, full: bool = False) -> None:
    def clip(text: str, n: int) -> str:
        lines = text.strip().splitlines()
        return "\n".join(lines if full else lines[:n]) + ("" if full or len(lines) <= n else f"\n      ... ({len(lines) - n} more lines)")

    for line in path.read_text().splitlines():
        r = json.loads(line)
        status = f"{r.get('latency_ms', '?')} ms" if r["ok"] else f"INVALID: {r.get('error', '')[:120]}"
        if r.get("models"):
            status += f"  served by {', '.join(r['models'])}"
        print(f"\n{'-' * 78}\n{r['agent'] or '?'}  [{r['provider']} / {r['model']}]  attempt {r['attempt']}  {status}\n{'-' * 78}")
        system = r["system"].split("\n\nRespond with a single JSON object")[0]
        print("  SYSTEM:\n      " + clip(system, 6).replace("\n", "\n      "))
        print("  USER:\n      " + clip(r["user"], 12).replace("\n", "\n      "))
        raw = r.get("raw") or ""
        try:
            raw = json.dumps(json.loads(raw), indent=2)
        except (json.JSONDecodeError, TypeError):
            pass
        print("  REPLY:\n      " + clip(raw, 40).replace("\n", "\n      "))


async def _run(db: Database, incidents: list[Incident], as_json: bool, concurrent: bool = False) -> None:
    settings = get_settings()
    if not as_json:
        served = "rules (no LLM)" if not settings.live else f"{settings.specialist.provider} / {settings.specialist.model}"
        print(f"Mode: {settings.llm_mode}  LLM: {served}  (tags below name the role: qwen = router, nemotron = A1-A3/CR)")
    orch = Orchestrator(db, settings)
    if concurrent:
        results = await asyncio.gather(*(orch.handle(inc) for inc in incidents))
    else:
        results = [await orch.handle(inc) for inc in incidents]
    for inc, res in zip(incidents, results):
        if as_json:
            print(res.model_dump_json(indent=2))
        else:
            print_result(db, inc, res)


def main() -> None:
    p = argparse.ArgumentParser(prog="urban-response")
    p.add_argument("-v", "--verbose", action="store_true")
    sub = p.add_subparsers(dest="cmd", required=True)
    d = sub.add_parser("demo")
    d.add_argument("--reset", action="store_true", help="Reseed DB and clear the incident log first")
    d.add_argument("--json", action="store_true")
    d.add_argument("--trace", type=Path, help="Append every LLM call (prompt + reply) to this JSONL file")
    i = sub.add_parser("incident")
    i.add_argument("file", type=Path)
    i.add_argument("--json", action="store_true")
    i.add_argument("--trace", type=Path, help="Append every LLM call (prompt + reply) to this JSONL file")
    sub.add_parser("prefetch", help="Download/cache the MiniLM model (run once before going offline)")
    b = sub.add_parser("burst")
    b.add_argument("file", type=Path, nargs="?", default=ROOT / "scenarios" / "burst_park_hill.json")
    b.add_argument("--json", action="store_true")
    b.add_argument("--reset", action="store_true")
    b.add_argument("--trace", type=Path, help="Append every LLM call (prompt + reply) to this JSONL file")
    sm = sub.add_parser("smoke", help="Check the live model servers end to end; exit 1 on any failure or fallback")
    sm.add_argument("--quick", action="store_true", help="Reachability + structured probe only, skip the full pipeline run")
    sm.add_argument("--trace", type=Path, help="Append every LLM call (prompt + reply) to this JSONL file")
    t = sub.add_parser("trace")
    t.add_argument("file", type=Path)
    t.add_argument("--full", action="store_true", help="Show full prompts instead of the first lines")
    sub.add_parser("resources")
    sub.add_parser("incidents")
    s = sub.add_parser("seed")
    s.add_argument("--force", action="store_true")
    sv = sub.add_parser("serve")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8000)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO if args.verbose else logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    if getattr(args, "trace", None):
        os.environ["UR_TRACE_FILE"] = str(args.trace)
    settings = get_settings()

    if args.cmd == "prefetch":
        from . import dedup

        dedup.load_model()
        print(f"MiniLM ready: {dedup.MODEL}. Set HF_HUB_OFFLINE=1 to run without internet.")
        return
    if args.cmd == "smoke":
        from . import smoke

        raise SystemExit(smoke.main(settings, skip_pipeline=args.quick))
    if args.cmd == "trace":
        print_trace(args.file, args.full)
        return
    if args.cmd == "serve":
        import uvicorn

        uvicorn.run("urban_response.api:app", host=args.host, port=args.port)
        return

    db = Database(settings.db_path)
    try:
        if args.cmd == "seed":
            print(f"Seeded {seed(db, force=args.force)} units")
        elif args.cmd == "resources":
            seed(db)
            print(f"{'agency':<10} {'resource_type':<24} {'total':>5} {'committed':>9} {'available':>9}")
            for r in db.inventory():
                print(f"{r['agency']:<10} {r['resource_type']:<24} {r['total']:>5} {r['committed']:>9} {r['available']:>9}")
        elif args.cmd == "incidents":
            for st in IncidentStatus:
                rows = db.incidents(st)
                print(f"\n{st.value.upper()} ({len(rows)})")
                for i in rows:
                    print(f"  {i['incident_id']}  {i['category']:<16} sev {i['severity']}  units {i['units_committed']:>2}  "
                          f"response {i['latest_response_id'] or '-'}")
        elif args.cmd == "demo":
            seed(db, force=args.reset)
            raw = json.loads((ROOT / "scenarios" / "sheffield_demo.json").read_text())
            asyncio.run(_run(db, [Incident(**x) for x in raw], args.json))
        elif args.cmd == "burst":
            seed(db, force=args.reset)
            asyncio.run(_run(db, [Incident(**x) for x in json.loads(args.file.read_text())], args.json, concurrent=True))
        elif args.cmd == "incident":
            seed(db)
            asyncio.run(_run(db, [Incident(**json.loads(args.file.read_text()))], args.json))
    finally:
        db.close()


if __name__ == "__main__":
    main()
