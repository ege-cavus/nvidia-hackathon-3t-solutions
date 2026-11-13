# Authored incident reports and response-driven world state

This is the new integration path. Existing sheffield_sim.py demos remain
experimental; ReportDrivenWorld reuses only graph/resource/scoring helpers.
It never calls legacy random spawning or intensity escalation.

## Files

- schemas/incident.schema.json is an exact copy of the user-supplied jsonformal.txt.
- assets/scenarios/sheffield_reports_demo/initial_reports.json contains four
  synthetic caller reports: station fire, Park Square flood, Parkway collision,
  and Meadowhall medical emergency. Individual examples are also exported under
  generated_spread_example/.
- scenario.json binds reports to graph nodes and privately defines scenario
  severity, response requirements/work thresholds and authored spread links.
- generated_spread_example/ contains an uncontrolled six-tick fixture: the fifth
  report appears at OSM_1952229758 at 2026-11-13T09:06:00Z. world_events.json
  records its parent report separately, keeping caller JSON in the supplied format.
- media_manifest.json has image/video paths and draft image prompts for all five
  reports. No images or videos have been generated. Populate assets/incident_media
  with pre-generated JPGs; missing images are explicitly labelled.

All incidents and reports are fictional. Dates and IDs are deterministic demo
fixtures. Production incident IDs must be assigned transactionally by a database;
this starter uses a local deterministic sequence for spread fixtures, not a
production ID allocator. Caller observations are not verified ground truth.

## Run from this source checkout

```powershell
python -m pip install -e .
python sheffield_sim.py --download-osm --road-report assets/sheffield_road_report.json --ticks 0
python report_world.py --ticks 6 --output demo_output/uncontrolled
python report_world.py --ticks 60 --scripted-response --output demo_output/responded
python -m unittest discover -s tests -v
```

The scripted response is an oracle smoke test using scenario requirements;
it is not Qwen/Nemotron performance evidence. Report-only export needs no model
or media access. The real-road run uses the cached graph; regression tests build
synthetic streets locally and need no network.

## Lifecycle and causality

1. Validate each authored report against the saved Draft 2020-12 schema, including
   timestamp format. Apply the separate node binding to city state.
2. Publish qwen_observation(incident_id): the exact caller report, a timestamped
   local graph snapshot and a pre-generated image/video reference. Keep spread
   rules, required unit counts and success thresholds out of this payload.
3. Submit human/agent decisions through the existing DispatchCommand boundary.
   Duplicate/invalid/unavailable allocations are rejected; routes and turnaround
   use the graph. An authored closure uses provisional staging where supported.
4. Each tick, evaluate observed on-scene capability and accumulated effective work.
   Success metrics produce response_events, reduce world severity, mark containment
   and eventually resolution, and reopen scenario-owned roads when resolved.
5. At a spread link's due time, evaluate the authored conditions after that tick's
   response work. Effective containment suppresses it. Otherwise create one report
   at the target node, using that tick's received_at, and record a parent/child link.

Road connectivity alone does not predict building-to-building fire spread. This
example's secondary ignition is an authored nearby graph-node event. No stochastic
spread, hydrodynamics or clinical outcomes are inferred. The demo uses two ticks
of effective work for containment and five for resolution; these are game rules.
Success metrics are computed from world observations, never accepted blindly from
an LLM's self-assessment. World scoring remains version effectiveness-v2.

## Integration handoffs

Track 2 should wake Qwen once per new report ID and use qwen_observation, adding
only its permitted live resource view. Current legacy process_pending does not
consume this runner automatically. Never send the full scenario/evaluation file
as model input or reuse a stale frame for a different node/report.
Track 3 should show report times, causal links, response success events and image
placeholders. Track 4 owns future profile/schema consolidation and licensed media.
Only one open incident per node is supported; re-ingestion waits for its previous
resources to finish turnaround. Authored node closures outside the five hero
camera mappings require an explicit access/closure policy before enabling them.

## Cosmos image client

cosmos_client.py sends a report manifest prompt to a ready Cosmos3 Generator NIM,
decodes b64_image, verifies the JPEG and atomically saves it as the incident ID.
It never needs the NGC key; that stays with the NIM deployment. Existing images
are skipped unless --overwrite is supplied. Requests are not automatically retried
because a timed-out generation may still be running on the server.

For a localhost-only Spark deployment, open a tunnel from this machine:

```powershell
ssh -N -L 18000:127.0.0.1:8000 USER@SPARK
```

In a separate terminal, after Generator readiness succeeds:

```powershell
python cosmos_client.py --endpoint http://127.0.0.1:18000 --incident-id 20261113000002
```

Preview all payloads without a server or model:

```powershell
python cosmos_client.py --dry-run
```

These prompts generate illustrative synthetic views, not verified reproductions
of Sheffield CCTV. Review images before feeding them to Qwen. Live generation is
separate from the report-world clock; a render must not advance scenario time.
