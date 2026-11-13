# Future solo human-versus-agents mode

The player acts alone as incident commander against the combined Qwen/Nemotron
team. game_session.py provides a headless comparison foundation; the playable
UI and competition deadline policy are still follow-up work in GitHub issue #6.

## Boundaries already implemented

- SheffieldSim owns authoritative incident facts, resource lifecycle, allocation
  constraints and scoring. A controller's report cannot rewrite world facts.
- Human UI and agents use DispatchCommand -> apply_command -> common validation
  -> dispatch. Commands include controller, camera, units and expected tick.
  Stale, unknown-incident and duplicate dispatches are rejected without depletion.
- command_log records accepted/rejected decisions for replay and debugging.
- SoloMatch creates independent HUMAN and AGENTS worlds from the same graph and
  initial state. Resources, messages, incident outcomes and scores are separate.
- A frozen Scenario event tape supplies identical incidents on identical ticks.
  Separate seeded generators alone are insufficient: conditional spawning can
  consume random draws differently after different decisions. Match mode disables
  per-world random spawning and injects precomputed events instead.
- One shared simulation clock advances both worlds. Agent wall-clock latency
  never advances physics; advance is blocked while the agent turn is running.
  One agent run is allowed per decision tick. Observation snapshots are detached
  and contain only that side's current world, not the opponent or future tape.

## Future incident catalogue and progression

Start with four incident types, mapping onto the existing agency-facing hazards:

| Incident type | Existing hazard | Example progression |
| --- | --- | --- |
| Flood | flood | pooling -> flooded road -> vehicles trapped |
| Building fire | fire | smoke -> established fire -> spreading fire |
| Road collision | trauma | obstruction -> injured casualties -> critical casualties |
| Medical emergency | trauma | urgent -> life-threatening -> critical delay |

These are proposed game stages, not clinical triage categories or operational
response doctrine. Track 4 should define data-driven INCIDENT_PROFILES with
incident_type, stage, stage_elapsed_ticks, stage transition thresholds, agency
requirements, work duration and escalation/de-escalation rules. Keep severity
1..10 as an additional intensity measure rather than making stage a synonym.
The simulator owns transitions. Models describe/estimate stages; their output
must not directly set authoritative progression. Appropriate on-scene capability
should slow/reverse progression; dispatching a unit alone must not resolve it.

Record external escalation drivers (rain bursts, fuel, secondary collisions) in
the shared scenario tape, so differing policies do not change the random future.
Use identical rules on both sides; outcomes diverge only through interventions.
Permit cross-hazard effects later (flood -> collision, fire -> trauma) through
explicit transition/effect data, rather than scattered type-specific UI logic.

Current code retains the three hazard categories and generic severity movement;
the four-profile catalogue and capability-specific stages are a documented next
step, not an implemented progression system. Existing generic on-scene logic
must be replaced before treating stage outcomes as a faithful game mechanic.

## Integration example

```python
from game_session import SoloMatch
from state_schema import DispatchCommand
from agent_orchestrator import process_pending

match = SoloMatch()  # Seed 42, synthetic graph; inject a shared OSM graph if desired
match.advance()     # tick 1 opens the first decision window
view = match.observation("HUMAN")
# UI displays current incidents/resources and captures this command:
receipt = match.submit_human(DispatchCommand("CAM_01", ("FIRE_1", "POL_1"), match.tick, "HUMAN"))

async def agents(world):
    await process_pending(world.state, sim=world, live=False)

# Inside the application's async runner:
# await match.run_agents(agents)
# Advance again only after the human window and agent turn are completed.
```

A valid human command never needs a Qwen report. Validation derives minimum
requirements from world rules, not qwen_report.required_agencies. Reported type,
severity and blockage remain hypotheses inside qwen_report. Existing autonomous
mode remains supported through the same dispatch adapter.

## Follow-up before a playable competition

1. Track 3: add a mode selector and one-human dispatch panel using observation
   and submit_human, plus separate scores/results. Do not call models from human
   controls or expose the opponent board/future tape during a ranked attempt.
2. Integration: define equal observation release times and decision budgets. The
   UI must call the agent adapter once per turn before closing both windows;
   no-op is an explicit wait. The match layer is trusted in-process orchestration,
   not an anti-cheat sandbox. A hosted competition needs server-owned state.
3. Track 4/Track 1: implement the four incident profiles and capability-aware
   progression above. Introduce unique incident IDs before repeated incidents at
   the same camera; version 1 rejects duplicate-camera scenario events to avoid
   overwriting incidents still holding units. Keep the existing cam-keyed schema
   compatible until that migration is coordinated.
4. Version scenario, graph, rules, observations and scoring in saved match data.
   The current command log replays within a supplied scenario/graph/rules version;
   it is not yet a portable versioned match-save format. Reject mismatched replay
   inputs rather than silently comparing different rules.
5. Decide end-of-match unresolved-incident penalties and tie-breakers. Current
   scorecards are shared and deterministic, but competitive balancing is pending.
   The final horizon tick closes decisions; no more commands are accepted then.

## Verification

Tests cover equal-action/equal-score outcomes, intervention-independent arrivals,
state isolation, stale/duplicate command rejection, replay, fixed horizon,
model-latency clock isolation, and Qwen reports that contradict world facts.
