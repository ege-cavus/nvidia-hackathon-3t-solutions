# SURU Command UI: Briefing and Build Spec

**Purpose:** replace the current Streamlit page on GitHub (`origin/main:app.py`) with a command dashboard built on the local `urban_response` backend (FastAPI, Qwen orchestrator → Nemotron A1–A3 → Nemotron CR).
**Design goal:** an operator should be able to answer four questions in under five seconds: *What is happening? Where? Are we coping? Why did the agents decide that?*

---

## 1. Where things stand

| | GitHub `origin/main` | Local working copy (not pushed yet) |
|---|---|---|
| Backend | `sheffield_sim.py` tick simulator, camera-keyed incidents, 6 units | `urban_response/`: FastAPI, SQLite Incident/Response/Resource DBs, ~70 units across 15 stations |
| Agents | Qwen triage → Nemotron message board → Qwen takeover | Qwen routes → Nemotron A1/A2/A3 in parallel → Nemotron CR resolves hard and soft conflicts |
| UI | `app.py`: a single 105-line Streamlit page | none |

The two codebases have different data models: camera IDs and ticks on GitHub, `INC-xxxx`, `RSP-xxxx` and real resource types locally. This spec targets the **local** API because that's where the incident detail, conflicts and CR decisions live. Push the local work, ideally to `feat/track3-ui`, before the UI work starts.

---

## 2. What to remove from the current UI

| Current element (`app.py`) | Problem | Action |
|---|---|---|
| **Cosmos 3 CCTV four-panel monitor** + camera multiselect | Asset folders hold only `.gitkeep`, so the demo shows four "Awaiting local Cosmos MP4" boxes. It takes up half the page. | **Remove.** If a frame exists later, show one thumbnail inside the incident detail panel. |
| **Synthetic road graph** (`PathLayer`, grey lines) + "invented links, not actual streets" caption | Looks broken. `map_style=None` means there is no basemap. | **Remove.** Use a real basemap (Carto light/dark) and drop the disclaimer. |
| **Sidebar per-unit dump** (`POL_1: AVAILABLE \| 0 ticks` ×N) | Unreadable with 70 units. It also pushes the controls off screen. | **Move** to the Resource Database modal (§4.5). |
| **Bottom "Live Resource Database" dataframe** (11 columns of tick timers) | Duplicates the sidebar. The tick columns mean nothing to a viewer. | **Replace** with the Resource Database modal. |
| **"Shared Multi-Agent Message Board" raw dataframe** | A table of JSON-ish rows is the opposite of observable. | **Replace** with the Agent Observability timeline (§4.6). |
| **Response Effectiveness Scorecard** (game score formula: `100 − 5·SLA − 3·spill + resolved`) | Arbitrary game metric that doesn't match the local backend. | **Replace** with the Success Metric strip (§4.2). |
| **"Advance tick" / "Retry pending negotiations" buttons** | The local backend is event-driven (`POST /incidents`), so there are no ticks. | **Replace** with `▶ Run demo scenario` and `+ New incident`. Retry moves into the incident detail panel for `failed` responses. |
| **"Live Ollama models" toggle** | Users flip it without the models running, and the page crashes or hangs. | **Replace** with a read-only mode badge from `GET /health` (`mock` / `live · nemotron`). |
| **Sidebar `st.warning` about uncalibrated priors** + **"Data provenance" expander** with raw `st.json` | Permanent yellow noise. | **Move** to a single "About this data" link in the footer. |
| `st.caption` colour legend under the map | Easy to miss. | **Replace** with an on-map legend chip. |

---

## 3. Layout

```
┌──────────────────────────────────────────────────────────────────────────────────────────┐
│ SURU  Sheffield Urban Response        [mode: live · nemotron]  [▶ Run demo] [+ Incident] │
│                                                                    [▦ Resource DB]       │
├──────────────────────────────────────────────────────────────────────────────────────────┤
│  RESPONSE SUCCESS  83%  │  Fill rate 91%  │  ETA met 88%  │  Conflicts auto-resolved 7/8  │
├─────────────────────┬────────────────────────────────────────────┬───────────────────────┤
│ ONGOING INCIDENTS   │                                            │ AGENT OBSERVABILITY ● │
│ [All][Fire][Police] │                  MAP                       │                       │
│ [Medical][Hazmat]…  │                                            │ 12:04:31 Qwen  ROUTE  │
│ ─────────────────── │   ● incidents (sized by severity)          │  INC-7F2A → fire lead │
│ ● INC-7F2A  SEV 5   │   ▲ stations    ■ units en route           │  + police, ambulance  │
│   High-rise fire    │   ◌ cordon rings for selected incident     │ 12:04:33 A3 Fire      │
│   Broomhall · 6 min │                                            │  requests 4× PMP, 1×  │
│   ⚠ partial         │                                            │  ALP (KB: PDA §2)     │
│ ● INC-91C0  SEV 4   │                                            │ 12:04:34 CR  HARD     │
│   Hazmat · Tinsley  │                                            │  shortfall PMP 4>2    │
│   ✓ dispatched      │                                            │  → preempt from       │
│ …                   │                              [legend]      │    INC-11AB (sev 2)   │
│                     │                                            │ …                     │
│ ▸ Backlog (3)       ├────────────────────────────────────────────┤ [filter: incident ▾]  │
│ ▸ Closed (5)        │ INCIDENT DETAIL (opens when one is chosen) │ [pause]               │
└─────────────────────┴────────────────────────────────────────────┴───────────────────────┘
```

- **Left sidebar:** the filter by incident type and the ongoing incidents list.
- **Centre:** the map, with the incident detail drawer underneath. The map stays visible while the detail is open.
- **Right rail:** the live agent observability feed.
- **Top:** global actions and the success metric strip.
- **Resource DB** opens in a modal from the header button, so it never takes permanent space.

---

## 4. Components

### 4.1 Header
- Title, plus a mode badge from `GET /health`: green `live · nemotron`, grey `mock`, amber if any agent returned `fallback` in the last response.
- `▶ Run demo scenario` posts each incident in `scenarios/sheffield_demo.json` to `POST /incidents`, 3 seconds apart, so the observability feed has time to play out.
- `+ Incident` opens a short form (category, severity, address/lat-lon, description, casualties, trapped, hazards) → `POST /incidents`.
- `▦ Resource DB` opens the modal (§4.5).

### 4.2 Success metric strip
One headline number plus three supporting numbers, all computed from `GET /responses` (or a new `GET /metrics`, see §6):

| Metric | Definition | Why |
|---|---|---|
| **Response success** (headline) | % of incidents whose latest response is `dispatched`, i.e. every essential request was met | The one number a judge remembers |
| Fill rate | allocated units ÷ requested units across all responses | Shows partial coverage under strain |
| ETA met | allocations with `eta_min ≤ max_eta_min` ÷ allocations that had an ETA requirement | Speed |
| Conflicts auto-resolved | hard conflicts where CR's choice passed re-validation (not overridden by rules) ÷ all hard conflicts | Proves the agents are doing real work |

Display rules: big number, then a delta since the last incident (`▲ 4`), coloured green ≥ 80%, amber 60–79%, red < 60%. Hovering a metric shows its definition. No charts here; a sparkline is optional.

### 4.3 Sidebar: filter + ongoing incidents
- **Filter chips** (multi-select, "All" by default). Group the backend `category` values into operator-friendly sections:

  | Chip | Categories |
  |---|---|
  | Fire | `structure_fire`, `high_rise_fire` |
  | Police | `firearms`, `assault` |
  | Medical | `medical` |
  | Traffic | `rtc` |
  | Hazmat / Env | `hazmat`, `flood` |

  Unknown categories go in "Other". The filter applies to the list, the map markers **and** the observability feed together.
- **Incident card** (sorted by severity, then newest):
  `● INC-7F2A  SEV 5` / category in plain words / district · time since received / response status pill (`✓ dispatched`, `⚠ partial`, `✕ failed`, `… routing`). Severity colour runs 5 = red to 1 = grey. Show agency icons for the routed agencies, with the lead agency's icon outlined.
- Clicking a card selects the incident: the map flies to it, the detail drawer opens, and the observability feed highlights that incident's entries.
- `pending` and `backlog` incidents count as "ongoing". **Backlog** and **Closed** are collapsed sections below.
- Polls every 2 seconds (`GET /incidents`).

### 4.4 Map
- Basemap: Carto Positron (light) or Dark Matter (dark), centred on Sheffield, `zoom 12`.
- Layers:
  1. **Incidents:** circles sized by severity and coloured by status. A pulsing ring marks incidents still being routed.
  2. **Stations:** small agency-coloured triangles (police blue, fire red, ambulance green, shared purple).
  3. **Allocated units:** lines from station to incident for the selected incident, labelled with callsign and ETA.
  4. **Cordons (selected incident only):** concentric rings from the agents' `scene_constraints`: inner cordon (solid), outer cordon (dashed) and evacuation radius (dotted), with the staging area pinned. This makes soft conflicts like `STAGING_INSIDE_INNER_CORDON` visible at a glance.
- Tooltip: `INC-ID · category · SEV n · status`. Click selects, same as clicking the card.
- Legend chip in the bottom-right corner.

### 4.5 Incident detail drawer (the "incident ID has a bunch of details" view)
Opens under the map. It has a header line and tabs, with the summary shown first:

- **Header:** `INC-7F2A · High-rise fire · SEV 5 · Broomhall` · status pill · `Close incident` button (`POST /incidents/{id}/close`). `Retry` appears only when the status is `failed`.
- **Summary tab:** description, casualties / trapped / hazards / weapons, a routing line (*"Qwen routed to Fire (lead), Ambulance, Police: rationale…"*), and CR's **unified directives** as a numbered list. This is the most important content.
- **Resources tab:** a table of allocations (callsign, type, agency, station, ETA, requested by, *preempted from*). Unmet needs are shown in red below it, with the mutual-aid note.
- **Agencies tab:** one card per agent (A1/A2/A3): summary, tactical plan, hazards, safety notes, KB citations, confidence bar, `executed_by` badge and latency.
- **Conflicts tab:** hard conflicts (kind, resource, requested vs available, CR's chosen resolution and rationale) and soft conflicts (code in plain English, severity, agencies, suggested resolution, a `rules` or `semantic` source tag).
- **History tab:** previous responses for this incident (`GET /incidents/{id}/responses`).

Data comes from `GET /incidents/{id}` + `GET /incidents/{id}/responses`. Use the latest response.

### 4.6 Resource Database (modal, opened from the header button)
- **Top:** a per-type availability table from `GET /resources`. Columns: agency, type (human name *and* code, e.g. "Fire engine (PMP)"), total, available, committed, out of service. The available column shows a bar (green / amber < 30% / red 0).
- **Agency tabs** filter the table: All | Police | Fire | Ambulance | Shared.
- **Row expand:** the unit list for that type (callsign, station, status, committed to `INC-…`), with a `Release` action (`POST /units/{id}/release`). Also a 2-hour availability forecast mini-chart from `GET /resources/{type}/forecast`.
- **Hospitals** section: name, ED capacity, major trauma / paediatric flags.
- The modal closes with Esc. It must never block the live feed for long.

### 4.7 Agent observability (right rail, live)
A reverse-chronological feed of decisions as they happen. Each entry has a timestamp, an agent chip (`Qwen`, `A1 Police`, `A2 Ambulance`, `A3 Fire`, `CR`), an event type, the incident ID and a one-line plain-English summary. The entry expands to show the full rationale or JSON.

| Event | Emitted by | One-liner example |
|---|---|---|
| `ROUTED` | Qwen | INC-7F2A → Fire (lead), Ambulance, Police: "high-rise, persons reported" |
| `PROPOSAL` | A1/A2/A3 | A3 Fire requests 4× fire engine, 1× ALP; inner cordon 50 m (KB: pre_determined_attendance §2) |
| `HARD_CONFLICT` | rules | Shortfall: fire_engine, 4 requested, 2 free |
| `RESOLUTION` | CR | Preempt PMP-07 from INC-11AB (sev 2), mutual aid for 1 more |
| `OVERRIDE` | rules | CR choice invalid → policy fallback `accept_partial` (amber) |
| `SOFT_CONFLICT` | rules / CR | Ambulance staging inside fire inner cordon → move RVP to Hanover St |
| `DISPATCHED` | CR | 9 units allocated, status partial, 1 unmet |

- The chip shows `executed_by`, and the colour flags `fallback` / `mock` so viewers can tell a model decision from a rule.
- Controls: follow the selected incident, pause auto-scroll, and filter by agent.
- A pulsing dot in the header shows the feed is live.

---

## 5. Visual and usability rules
- **One accent colour per agency**, used everywhere (map, chips, cards): Police `#2F6FED`, Fire `#E5484D`, Ambulance `#30A46C`, Shared `#8E4EC6`. Status colours are separate: green ok, amber partial, red failed.
- **Plain English first, codes second.** For example, "Staging inside hot zone" goes first and `STAGING_INSIDE_INNER_CORDON` appears below it in small monospace.
- No raw JSON or dataframes on the main screen. Raw data only appears in an expanded "raw" toggle.
- Selection state is global: one selected incident drives the list highlight, the map focus, the drawer and the feed highlight.
- Empty states should tell the user what to do: e.g. "No incidents yet. Press ▶ Run demo scenario."
- Supports dark mode and stays readable on a projector (min 14px body text, 32px+ headline metric).

---

## 6. Backend gaps to close

| Gap | Needed for | Change |
|---|---|---|
| No live event stream | §4.7 Observability | Add an `events` table (`id, ts, incident_id, agent_id, type, summary, payload_json, executed_by`). Emit from `orchestrator.route`, each `SpecialistAgent` return, hard-conflict detection, CR resolution and validation, soft conflicts, and the final response. Expose `GET /events?since_id=` (polling) or `GET /events/stream` (SSE). |
| No unit-level listing | §4.6 row expand, §4.4 unit lines | `GET /units?resource_type=&agency=` returning unit, callsign, station, lat/lon, status, current `incident_id` |
| No stations/hospitals endpoint | Map station layer, hospitals section | `GET /stations`, `GET /hospitals` |
| Metrics computed client-side | §4.2 | `GET /metrics` returning the four numbers + deltas |
| Category filter | §4.3 | Optional `GET /incidents?category=`. Client-side filtering is fine for the demo. |

Until `/events` exists, the observability feed can be rebuilt after the fact from each stored response (`routing.rationale`, `agent_responses`, `hard_resolutions[].rationale`, `soft_conflicts`, `cr_rationale`). It won't be live, but the demo still works.

---

## 7. Recommended implementation
Stay on **Streamlit** for hackathon speed, but structure it properly:

- `ui/app.py` holds the layout. `ui/api.py` is a thin `httpx` client for the FastAPI server, so the UI never imports DB code.
- Live areas (incident list, metrics, feed) go in `@st.fragment(run_every="2s")` so only they re-render, not the whole page.
- The Resource DB modal uses `@st.dialog`.
- The map uses `st.pydeck_chart(..., on_select="rerun")` for click-to-select, and stores `selected_incident` in `st.session_state`.
- Layout: `st.columns([1, 2.4, 1.2])`. Use custom CSS only for status pills and agency chips.
- Run: `uv run urban-response serve` (API on :8080) + `streamlit run ui/app.py`.

### Demo script (≈ 3 minutes)
1. Empty state → press **▶ Run demo**.
2. The RTC in Darnall appears. Watch Qwen route it and three Nemotron proposals arrive; success is 100%.
3. The firearms incident arrives, and the police contention is resolved by CR.
4. The high-rise fire arrives: a fire-engine shortfall, then CR **preempts** from the lower-severity RTC. The map shows a unit rerouting, and the success metric dips to amber.
5. Open the high-rise incident and show the cordon rings and the soft conflict (*ambulance staging inside the inner cordon*), with CR's unified directive fixing it.
6. Open the **Resource DB** to show depleted fire engines and the 2-hour forecast.
7. Close the RTC; units are released and the metrics recover.

### Done when
- [ ] No CCTV panel, synthetic road graph, tick buttons, raw tables or provenance JSON remain on the main screen
- [ ] Clicking an incident (card or map) opens its full detail within one click
- [ ] Filter chips filter the list, map and feed together
- [ ] Resource DB opens from the header and closes with Esc
- [ ] The observability feed shows each agent decision within 2 seconds of it happening, with `executed_by` visible
- [ ] Headline success metric is visible without scrolling at 1366×768
