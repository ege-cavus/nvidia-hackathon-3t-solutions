# Product

<!-- impeccable:product-schema 1 -->

## Platform

web

## Users

Hackathon judges watching a live demo. A team member drives the Streamlit
dashboard (app.py) on a shared screen and walks them through the simulation in a
few minutes. The judges never use it themselves. Their job is to follow what the
agents are doing, see why, and judge the outcome.

## Product Purpose

SURU (Sheffield Urban Response Unit) is a hackathon simulation of emergency
response across Sheffield:

1. Incidents appear at CCTV camera locations.
2. Qwen triages them and selects which agencies to wake.
3. Only the selected Nemotron agency agents (FIRE, POLICE, AMBULANCE) propose units.
4. The proposals reach validated consensus, or a Qwen arbiter takes over.
5. Every decision passes the same deterministic allocator before dispatch.

The dashboard succeeds when judges can see the city, what is happening in it,
what each agent decided and why, and how well the city is being served.

## Positioning

Agent reasoning is visible and bound to the city map. Judges see each incident
on a real Sheffield map. They can open any agency agent and read its
interactions and reasoning on the shared message board. They see what it cost:
the units, stations and capacity a decision ties up, and the city-wide
performance that follows. A generic incident map shows where things are. SURU
shows who decided what, why, and what it cost the rest of the city.

## Operating Context

- Live demo: a presenter clicks to advance one-minute ticks while judges watch a
  shared screen.
- Runs offline by default, with synthetic triage and a synthetic road graph.
  Live Ollama models and an OSM street graph are optional on the demo host.
- Cosmos-generated CCTV clips and frames are supplied locally. Missing ones are
  shown as missing.
- Team workflow: Track 3 leases app.py only, on feat/track3-ui. Contract
  changes go through GitHub issues #1–#6. See CLAUDE.md.

## Capabilities and Constraints

Requested for the demo surface (user brief, 2026-11-13). Not all of this is
built yet:

- A map view of Sheffield as the primary canvas. Incidents appear on it as they
  spawn.
- Police stations, fire stations and ambulance bases shown on the map.
- A sidebar listing incidents, each with its classification (hazard/type, stage,
  severity) and a progress bar for its response lifecycle.
- A city-wide success/performance matrix, built on effectiveness_metrics and
  the score breakdown.
- Additional map layers to be planned: for example roads/closures, unit routes,
  staging points and coverage. These are not yet specified.
- Each station's stock in personnel and vehicles.
- A popup/dialog per agent ("A1/A2/A3") showing that agent's interactions and
  reasoning visually.

Already built (app.py on main): pydeck map, 4-panel CCTV with triage text,
message board table, depletion table, scorecard, provenance expander.

Constraints:
- Streamlit + pydeck. Only app.py is Track 3's to edit.
- Offline-first. Synthetic graph, priors, assets and SOP numbers must stay
  labelled as such. Never present simulated output as operational advice.
- The resource contract has six vehicles (POL_1/2, FIRE_1/2, AMB_1/2) at six
  hubs. Vehicle stock comes from resource_db.

Open decisions (do not invent):
- **Personnel stock.** No crew or staffing field exists in state_schema.py.
  Showing manpower needs a Track 4 schema request (#5). Any numbers would be
  synthetic demo values and must be labelled that way.
- **"A1/A2/A3".** Inferred to mean the three Nemotron agency agents (FIRE,
  POLICE, AMBULANCE). Whether Qwen triage and the Qwen arbiter get their own
  popups is not confirmed.
- **Additional map layers.** Which layers ship, and whether the presenter can
  toggle them, is undecided.
- **Beat the Agents mode** (#6). Not yet confirmed as part of the judge demo.

## Brand Commitments

- Name: SURU — Sheffield Urban Response Unit.
- Binding visual constraint from the user: dark mode, blue-green theme.

## Evidence on Hand

- Simulation and agent pipeline: sheffield_sim.py, agent_orchestrator.py,
  game_session.py. PR #7 adds a staged Park Square flood.
- Station and camera coordinates (team-supplied): state_schema.py.
- Public source links and simulation SOPs: knowledge_base.py.
- CCTV: no Cosmos videos or frames are committed (assets/ only holds .gitkeep).
  The 212-camera GeoJSON is not committed either.
- No validated performance data. Risk priors are SYNTHETIC_UNCALIBRATED, and
  score weights are game rules.

## Product Principles

1. The map is the story. Everything else (incidents, agents, stock, scores)
   should trace back to a place in Sheffield.
2. Show the reasoning, not just the result. Any decision on screen can be
   traced to the agent and message-board entries that produced it.
3. Every dispatch has a visible cost. Show depletion and station stock next to
   the decision that caused them.
4. Be honest about what is simulated. Synthetic data, missing assets and
   unverified rules are labelled where they appear, not hidden in an expander.
5. Legible to someone watching, not driving. A judge who glances at the screen
   should grasp the state without being told where to look.
