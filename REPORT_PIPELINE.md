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
  reports. Local text-only image drafts have been generated but are unapproved
  and are not bundled. Populate assets/incident_media with reviewed JPGs;
  missing images are explicitly labelled. File existence alone is not approval.

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

## Reference-conditioned Cosmos trial

cosmos_reference.py uses Cosmos Generator's image2video mode with a local photo.
The NIM does not offer image-to-image; extract later frames from the generated
clip for a still-image demo. The first frame is a baseline, not evidence that
the requested hazard appeared. See NVIDIA's [generation API guide](https://nvidia.github.io/cosmos-framework/skills/cookbooks/cosmos3/nim/generation/).

The default trial uses assets/reference_views/park_square.jpg, credited to Chris
Morgan under CC BY-SA 2.0. Keep the source credit, licence and change description
with derivatives. These photos are area references, not verified CCTV feeds.
park_square_flood_trial.json records the prompt, reference, seed, attribution and
review criteria. It requests 49 frames at 12 fps, 480 tier, from the existing NIM.

```powershell
python cosmos_reference.py --dry-run
python cosmos_reference.py --endpoint http://127.0.0.1:18000
```

Outputs go to ignored demo_output/cosmos_reference/: one MP4, first/middle/last
JPEGs and a JSON sidecar recording reference/output hashes and settings without
the base64 input. The client checks that all requested frames decode before
publishing the clip. Existing outputs are skipped; use --overwrite explicitly
for a replacement. No request is retried automatically. Rendering can take many
minutes and must run outside the scenario tick loop.

Outputs start at GENERATED_PENDING_REVIEW, approved_for_qwen=false. Inspect road
and bridge geometry, camera stability and visible water in later frames before
selecting any image for a report. A caller's possible stranded car/occupants
remain unverified if absent from the image. This trial never updates the incident
manifest or world state automatically.

A completed [Park Square smoke check](assets/reference_views/park_square_flood_trial/README.md)
includes three review frames, provenance and live Qwen results. Water is visible,
but camera drift/geometry defects remain. Qwen detected flooding; its image-only
answer inferred unsupported water depth. The image-plus-report answer kept the
possible stranded car unconfirmed. One scene is not reliability evidence, and
none of these responses dispatches resources or modifies world truth.

## Multiple callers reporting the same incident

assets/scenarios/sheffield_reports_demo/same_incident_reports.json contains twelve
slightly varied caller reports about the original station fire. Each report still
validates against the supplied schema and carries the same canonical incident_id.
The bundle's report_links array assigns unique observation_id values outside the
caller report object. Wording, channel, viewpoint, reported severity and unknown
casualty details vary; none is asserted to be new world truth.

Run the scheduled observations after the main report is ingested:

```powershell
python report_world.py --ticks 3 --follow-up-bundle assets/scenarios/sheffield_reports_demo/same_incident_reports.json --output demo_output/follow-ups
```

Four observations arrive per minute, twelve by tick3. They attach to the existing
incident, do not increase incident counts, do not change resources/score/spread,
and do not overwrite the first caller's report. The world exports follow_up_reports
and follow_up_observations separately. Ingestion rejects unknown canonical IDs,
duplicate observation IDs and reports arriving before the original or in the future.

Track2 should deduplicate follow-ups by observation_id, not incident_id, and use
qwen_follow_up_observation(id) when reviewing new caller evidence. Its image is
explicitly a reference to the original report/time, not a fresh capture. Decide
whether corroboration warrants another Qwen call separately from world truth.
Production storage should use a separate observation row/ID referencing the
canonical incident, rather than attempting duplicate incident primary keys.
