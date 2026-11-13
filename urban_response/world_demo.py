"""Run canonical reports through agents, graph work and full resource return."""
import argparse
import asyncio
from dataclasses import replace
import json
from pathlib import Path
import tempfile

from report_world import DEFAULT_SCENARIO
from .agents.orchestrator import Orchestrator
from .config import get_settings
from .seed import seed
from .world import GraphReportWorld, WorldDatabase


async def run(args):
    config=json.loads(args.scenario.read_text(encoding="utf-8"))
    config["schema"]=str((args.scenario.parent/config["schema"]).resolve())
    if args.node:
        config["initial_reports"]=[r for r in config["initial_reports"] if r["node_id"]==args.node]
        if not config["initial_reports"]:
            raise ValueError("No authored initial report at that node")
    with tempfile.TemporaryDirectory() as temporary:
        scenario=Path(temporary)/"scenario.json"
        scenario.write_text(json.dumps(config),encoding="utf-8")
        db=WorldDatabase(":memory:")
        try:
            seed(db)
            world=GraphReportWorld(db,scenario)
            db.graph_world=world
            orch=Orchestrator(db,replace(get_settings(),llm_mode="live" if args.live else "mock"))
            async def event(incident_id,name,payload):
                if name in ("agent_start","agent_done","committed","evaluated"):
                    print(json.dumps({"incident_id":incident_id,"event":name,
                                      "agency":payload.get("agency"),"executed_by":payload.get("executed_by")}),flush=True)
            results=await world.dispatch_due(orch,event)
            for _ in range(args.ticks):
                world.tick()
                results.extend(await world.dispatch_due(orch,event))
            payload={"validation_kind":"LIVE_MODEL_RUN" if args.live else "MOCK_INTEGRATION_RUN",
                     "routing":world.state["routing"], "tick":world.state["tick"],
                     "results":[r.model_dump(mode="json") for r in results],
                     "world":world.snapshot(), "inventory":db.inventory(), "ledger":db.ledger(1000)}
            args.output.parent.mkdir(parents=True,exist_ok=True)
            args.output.write_text(json.dumps(payload,indent=2)+"\n",encoding="utf-8")
            print(json.dumps({"output":str(args.output),"reports":len(world.reports),
                              "resolved":world.state["effectiveness_metrics"]["incidents_resolved"],
                              "active_allocations":db._conn.execute("SELECT COUNT(*) FROM allocations WHERE released_at IS NULL").fetchone()[0],
                              "spread":world.spread_events}),flush=True)
        finally:
            db.close()


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario",type=Path,default=DEFAULT_SCENARIO)
    parser.add_argument("--node",help="Limit initial reports to one authored node, e.g. CAM_01")
    parser.add_argument("--live",action="store_true")
    parser.add_argument("--ticks",type=int,default=360)
    parser.add_argument("--output",type=Path,default=Path("demo_output/integrated_world.json"))
    args=parser.parse_args()
    if args.ticks<0:parser.error("ticks must be nonnegative")
    asyncio.run(run(args))
