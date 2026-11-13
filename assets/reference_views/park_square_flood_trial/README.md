# Park Square reference-conditioned flood trial

Synthetic demonstration, not footage of a real flood or a verified CCTV feed.

Original photograph: **Chris Morgan**, [Park Square roundabout](https://commons.wikimedia.org/wiki/File:Park_Square_roundabout_-_geograph.org.uk_-_6539765.jpg),
17 July 2020, [CC BY-SA 2.0](https://creativecommons.org/licenses/by-sa/2.0/).
The original is ../park_square.jpg. These derivative frames retain CC BY-SA 2.0.
Changes: Cosmos Nano generated a fictional flood and altered the viewpoint/scene;
JPEG frames were extracted from that generated clip. Keep this credit with reuse.

| Frame | What it demonstrates |
| --- | --- |
| [First](20261113000002-reference-frame-000.jpg) | Reference-conditioned dry baseline |
| [Middle](20261113000002-reference-frame-024.jpg) | Water entering foreground lanes |
| [Final](20261113000002-reference-frame-048.jpg) | Clearly visible foreground floodwater |

The existing Spark Nano Generator produced 49 frames at 12 fps, 832 x 480,
seed 42. See ../park_square_flood_trial.json for the request configuration and
generation.json for input/output hashes and review notes. The MP4 stays local
under demo_output/cosmos_reference/ and is ignored by Git.

Visual review: Park Square remains recognisable and floodwater is visible.
Camera zoom/drift and distorted road markings fail the fixed-camera criterion.
No stranded vehicle or occupants are confirmed. **Smoke-test sample only**;
these frames have not replaced the report's incident media or advanced world time.

## Live Qwen check

qwen_review.json contains three independent requests to the Spark's installed
qwen3.6:35b-a3b via Ollama: original photo, final generated frame, and final frame
with the authored Park Square report. Each request used temperature 0,
think=false, JSON schema output and keep_alive=0. No allocation was requested.

- Original photo: Qwen returned no visible hazard. It also missed a small car,
  so its scene description is not fully accurate.
- Generated frame alone: Qwen identified a flooded roadway, but made unsupported
  claims about water depth and immobilising vehicles, despite an instruction
  against inferring depth. Those claims must not become world truth.
- Generated frame with caller report: Qwen identified standing water and kept
  the possible stranded car/occupants visually unconfirmed; depth remained unknown.

This is one scene and one response per input, not a reliability benchmark.
Uncertainty, schema checks and deterministic dispatch validation remain necessary.
Images and caller claims are evidence for triage, not automatic outcome scores.
