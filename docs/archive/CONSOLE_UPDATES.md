# SURU Console: Dispatch-Screen Review and Vercel ↔ DGX Spark Deployment

**Scope:** the console in [urban_response/static/index.html](../../urban_response/static/index.html), served by [urban_response/api.py](../../urban_response/api.py). It replaces the Streamlit plan in [UI_BRIEFING.md](UI_BRIEFING.md) §7. That doc's feature list (map, success metric, filters, Resource DB, observability) still applies.

- **Part A** reviews the console as a 999/911 dispatch screen and gives prioritised changes.
- **Part B** explains how the site runs on Vercel while the models run on the DGX Spark on the LAN. That part is **built and tested**.

> The app models Sheffield, and the schema's default call source is `"999"`. Label the screen "999" rather than "911", or "emergency calls" if the audience is American.

---

# Part A: Making the console work on a dispatch screen

## A1. How the current console reads today

The console is a good **pipeline debugger** but not yet a **dispatch screen**. It answers "what did each model do on the last run?" well. It answers "what needs my attention right now, across every incident?" poorly.

| What works | What gets in the way on a dispatch floor |
|---|---|
| Live SSE pipeline shows every stage as it lands | **Only one incident at a time.** `RUNNING` disables Dispatch *and* stops you opening other incidents during a run (`openIncident` returns early). A real floor takes overlapping calls. |
| Map with stations, incidents and dispatch lines | **The map is a 360px tile** in the right column. Every incident looks the same (all red), and the default OSM tiles are busy and bright in a dim control room. |
| Agent cards with requests, cordons, KB citations | **The result comes last.** Units sent, ETAs and joint orders are at the *bottom* of a long scroll, below routing, agent cards and conflict tables. |
| `executed_by` badges flag fallback honestly | **Database jargon as status.** "pending / backlog / closed", "Incident DB", "Response DB". `backlog` means *units assigned*, the opposite of what it sounds like. |
| Resource bars, free/total | **No auto-refresh.** State only reloads after your own actions, so a second screen or operator goes stale silently. (Fixed in part: see the connection pill in B5.) |
| Text is set with `textContent` (LLM output is treated as untrusted) | **Admin controls sit beside operational ones.** "Reset city" (wipes every incident, no confirm) and the Live/Rules toggle are in the header next to the title. |
| | **The intake form takes the whole left column** permanently: raw lat/lon fields, `snake_case` categories, severity as a number box. |
| | Raw JSON (`specialist_metadata`) and code names (`STAGING_INSIDE_INNER_CORDON`) are on the main view. |
| | No wall clock, no time-since-call, no "this needs action" signal. |

## A2. Target layout

```
┌───────────────────────────────────────────────────────────────────────────────────────────┐
│ SURU 999 · Sheffield   14:32:07   ● DGX online · Qwen+Nemotron   SIMULATION   [+ New call N] [Resources R] [⚙] │
├───────────────────────────────────────────────────────────────────────────────────────────┤
│ ⚠ NEEDS ACTION (2)  INC-91C0 no fire engines available · INC-7F2A fallback rules used   [ack]   │
├──────────────────────┬───────────────────────────────────────────┬────────────────────────┤
│ CALLS (5)  [filter]  │                                           │ SELECTED: INC-7F2A     │
│ ▲5 High-rise fire    │                                           │ High-rise fire · SEV 5 │
│   Broomhall  06:12 ⏱ │               MAP (dark)                  │ ── WHAT WE SENT ───    │
│   ◐ partial · 9 units│     incidents = shape + number + colour   │ 4 PMP  ETA 4–9 min     │
│ ▲4 Hazmat            │     dispatch lines, cordon rings          │ 2 EA   ETA 6 min       │
│   Tinsley    03:40   │                                           │ ✕ 1 ALP unmet → mutual │
│   ● routing… 12s     │                                           │ ── JOINT ORDERS ───    │
│ ▲3 RTC               │                                           │ 1. RVP Hanover St …    │
│   Darnall    11:02   │                                           │ 2. Fire controls entry │
│   ✓ units assigned   │                                           │ ── WHY ▸ (expand) ──   │
│                      ├───────────────────────────────────────────┤ routing · agents ·     │
│ ▸ Closed (4)         │ AGENT FEED  14:32:01 CR preempted PMP-07… │ conflicts · CR         │
└──────────────────────┴───────────────────────────────────────────┴────────────────────────┘
```

## A3. Prioritised changes

### P0: needed for a credible dispatch screen
1. **Allow several incidents in flight at once.** The backend already supports this (`state["jobs"]` is a dict and each run has its own SSE stream). Drop the global `RUNNING` lock. Keep one `EventSource` per job and show progress *on the incident's card* (`● routing… 12s`, `● agencies 2/3`, `● resolving`). Opening any incident must always work.
2. **Lead with the outcome.** The order in the detail panel should be: **What we sent** (callsign, type, ETA, sorted by ETA) → **Gaps** (red, with the mutual-aid action) → **Joint orders** (unified directives) → collapsed **Why** (routing, agent cards, conflicts, CR rationale). The pipeline story is still one click away.
3. **Use plain status words.** `pending` → **Awaiting dispatch**, `backlog`/`dispatched` → **Units assigned**, `partial` → **Partly resourced**, `failed` → **No units: action needed**, `closed` → **Closed**. Remove "Incident DB" and "Response DB" from the screen.
4. **Add a "Needs action" strip.** One line at the top lists incidents that are `failed` or `partial`, used `fallback`, or have a `high` soft conflict, each with an *Acknowledge* button. This is what a dispatcher scans first.
5. **Show time everywhere.** A live wall clock in the header. Each call card shows a time-since-call counter that turns amber at 7 min and red at 15 min (configurable). The detail panel shows the first ETA.
6. **Show connection freshness.** A header pill reads `DGX online`, `backend offline · last seen 40s ago`, or similar. A stale dispatch screen must say so. *(Built: see B5.)* Extend it with 3-second state polling so other operators' actions appear.
7. **Move admin controls behind ⚙.** Put Reset city (with a typed confirmation) and Live/Rules mode there. The header shows the mode read-only.
8. **Show a persistent `SIMULATION · not for operational use` tag** in the header, as required by PRODUCT.md principle 4.

### P1: clarity at a glance (readable from 2–3 m)
9. **Make the map the main canvas.** It belongs in the centre column at full height, on a dark basemap (Carto Dark Matter tiles). Incident markers combine colour, a severity number and a shape so they are readable without colour vision. The selected incident shows dispatch lines and inner/outer/evacuation cordon rings from `scene_constraints`, so soft conflicts such as "staging inside hot zone" are visible on the map.
10. **Dark theme by default.** Control rooms are dim, and PRODUCT.md requires dark mode with a blue-green theme. Keep the light theme for daylight projectors via ⚙.
11. **Use bigger, steadier type.** 16px body text, 20px+ incident titles, tabular numbers for ETAs and counters. Nothing important under 13px.
12. **Plain English first, codes second.** For example "Ambulance staging inside fire hot zone", with `STAGING_INSIDE_INNER_CORDON` small and grey below it. Show category labels as words with icons, never `snake_case`.
13. **No raw JSON on screen.** Render `specialist_metadata` as key–value pairs and keep the raw view behind a "raw" toggle.
14. **Move intake into a drawer.** `+ New call` (shortcut **N**) opens a side drawer. Address comes first, and a map click sets the location. Category is a grid of word+icon buttons, and severity is a 1–5 segmented control with descriptors ("1 minor … 5 major incident"). Casualties, trapped and weapons are steppers or toggles. Presets live under "Demo scenarios" in the drawer.

### P2: speed and polish
15. **Keyboard operation.** `J/K` move through calls, `Enter` opens one, `N` starts a new call, `R` opens resources, `Esc` closes, `A` acknowledges the top alert.
16. **Resource panel.** Show only types at or below 30% availability by default, grouped by agency, with an "all" toggle. Show station stock (`/api/state` → `stations`) in a modal from the map or the header.
17. **Agent feed.** A compact one-line feed under the map, built from the SSE events across all jobs, e.g. `14:32:01 CR · INC-7F2A · preempted PMP-07 from INC-11AB (sev 2)`. Clicking an entry opens that incident's Why section.
18. **Optional audible chime** for new `failed` incidents, muted by default and toggled in ⚙.
19. **Close needs a confirm**, then a 5-second undo toast. Use toasts for errors instead of banners prepended into the output.

### What not to change
Keep the SSE event model, `textContent`-only rendering, the honest `fallback` badges and the per-agent cards. They're the strongest part of the console.

---

# Part B: Website on Vercel, models on the DGX Spark (built)

## B1. Architecture

```
 Operator browser (any network)
        │ HTTPS
        ▼
 Vercel ── static console (index.html, copied at build)
        │  /backend/*  ──rewrite──►  Vercel Function api/proxy.js
        │                             adds  Authorization: Bearer <secret>   (secret never reaches the browser)
        │                             streams SSE unbuffered
        ▼ HTTPS
 Tunnel (Tailscale Funnel or Cloudflare Tunnel): outbound only, no router ports opened
        ▼
 DGX Spark on the LAN
   ├─ SURU API (FastAPI)  :8080   ← orchestrator, agents, SQLite DBs, SSE job streams
   ├─ Qwen model server   :8001   (vLLM / NIM / SGLang / Ollama, OpenAI-compatible)
   └─ Nemotron server     :8002   (A1–A3 + CR share it)

 LAN screens can skip Vercel entirely: http://<spark-ip>:8080 (trusted, no token).
```

**Why it's split this way:** Vercel can't reach a LAN device, and Vercel Functions can't host the pipeline. The pipeline needs long-running jobs, a persistent SQLite file and the GPUs. So the Spark runs the whole backend, and Vercel only hosts the page plus a thin authenticated proxy. A browser on an HTTPS Vercel page also can't call `http://192.168.x.x` directly (mixed-content and Private Network Access blocking), which is why the tunnel gives the Spark a public HTTPS name.

## B2. What was added

| File | Purpose |
|---|---|
| [web/vercel.json](../../web/vercel.json) | Vercel project: build step, `/backend/*` → proxy rewrite, 300 s function duration for SSE, security headers |
| [web/api/proxy.js](../../web/api/proxy.js) | Proxy function. Allow-lists the console's routes only (`/docs` etc. return 404), injects the bearer token, streams `text/event-stream`, and turns tunnel HTML error pages into `{"detail": "DGX backend unreachable…"}` |
| [web/scripts/sync-console.mjs](../../web/scripts/sync-console.mjs) | Build step: copies `urban_response/static/index.html` into `public/` (one source of truth) and writes `suru-config.js` pointing the page at `/backend` |
| [urban_response/api.py](../../urban_response/api.py) | Token gate (`UR_API_TOKEN`). Direct LAN/loopback clients are trusted, and anything arriving with tunnel/proxy forwarding headers needs the token. Also adds `/healthz` (unauthenticated, reveals nothing), `/suru-config.js` (empty base when served locally), and `/api/config` now reports real model names plus `auth_required` |
| [urban_response/static/index.html](../../urban_response/static/index.html) | All calls go through `API_BASE` (`""` locally, `/backend` on Vercel). Adds a backend-online/offline pill that re-checks every 10 s. The model label shows the real Qwen and Nemotron names |
| [deploy/dgx/env.example](../../deploy/dgx/env.example), [deploy/dgx/start.sh](../../deploy/dgx/start.sh) | Spark config and launcher. It checks that both model servers answer `/models` before starting, and warns if the token is empty |

**Verified locally:**
- The token gate passes 7 cases: LAN direct allowed, tunnel without a token or with a bad token rejected (401), tunnel with the token allowed, `/healthz` open, and a public IP without a token rejected.
- The proxy, run against a live API, passes config, `/docs` blocked, starting a run, the **full SSE stream** (registered → routing → routed → agent_start/done ×N → conflicts_detected → cr_start → cr_decided → committed → end), state, and backend down → clean 502 JSON.
- 2 existing tests in `tests/test_pipeline.py` fail on main's in-progress `backlog`→`dispatched` status rename. That failure is unrelated to these changes.

## B3. Setting up the DGX Spark

**1. Serve the models.** Qwen 3.5 35B-A3B (FP8) on :8001 and Nemotron 3 Nano 30B-A3B (FP8) on :8002, both vLLM from the NGC image, defined in `deploy/dgx/models.compose.yml`. One script does the whole bring-up, including a live smoke test:
```bash
cp deploy/dgx/env.example deploy/dgx/.env      # HF_TOKEN, UR_API_TOKEN=$(openssl rand -hex 32)
./deploy/dgx/setup.sh
```
The first run downloads about 70 GB of weights. Nemotron starts only after Qwen is healthy, so the two servers don't both size their KV cache from the same free memory.

**2. Start the API**
```bash
uv sync                                        # large install: sentence-transformers pulls in torch
uv run urban-response prefetch                 # once, while online: caches the MiniLM dedup model
./deploy/dgx/start.sh                          # → http://<spark-ip>:8080 on the LAN; checks both servers serve the configured names
```
`.env` sets `HF_HUB_OFFLINE=1`, so the duplicate detector never needs the internet during the demo. `start.sh` refuses to start if the model isn't cached.

**3. Open the tunnel.** Pick one:

| Option | Command | URL | Notes |
|---|---|---|---|
| **Tailscale Funnel** (recommended) | `sudo tailscale up` then `tailscale funnel --bg 8080` | stable `https://<spark>.<tailnet>.ts.net` | Free, no domain needed, URL survives restarts |
| Cloudflare named tunnel | `cloudflared tunnel create suru` + route DNS + `cloudflared tunnel run --url http://localhost:8080 suru` | `https://suru.<your-domain>` | Needs a domain on Cloudflare |
| Cloudflare quick tunnel (emergency) | `cloudflared tunnel --url http://localhost:8080` | random `*.trycloudflare.com` | URL changes on every restart, so you have to update Vercel env and redeploy |

Venue Wi-Fi often blocks UDP. If the tunnel won't come up, use `cloudflared … --protocol http2`. Tailscale falls back to HTTPS relays on its own.

Run the API and the tunnel as `systemd` services, or in `tmux`, so they survive SSH disconnects.

## B4. Setting up Vercel

1. Import the GitHub repo into Vercel. **Root Directory: `web`**, Framework preset: **Other**. The build and output settings come from `web/vercel.json`. Leave "Include files outside the Root Directory" on, because the build copies the console from `../urban_response/static/`.
2. Environment variables (Production + Preview):
   - `SURU_BACKEND_URL` = the tunnel URL, e.g. `https://spark.tailnet-1234.ts.net`
   - `SURU_BACKEND_TOKEN` = the same value as `UR_API_TOKEN` on the Spark
3. Turn on **Deployment Protection** (Vercel Authentication or a password). Without it, anyone with the site URL can trigger pipeline runs on the Spark through the proxy.
4. Deploy, open the site, and check that the pill reads **backend online** and the label shows your Qwen/Nemotron model names.

Local test of the Vercel side: `cd web && vercel dev` with a `.env.local` holding the two variables.

## B5. Runbook (demo day)

| Symptom | Check |
|---|---|
| Pill says **backend offline** | `curl https://<tunnel>/healthz` from a phone on mobile data. If it fails, the tunnel or the API is down on the Spark. If it works, check the Vercel env vars and redeploy. |
| Requests return 401 | `SURU_BACKEND_TOKEN` ≠ `UR_API_TOKEN` |
| Live mode greyed out | The API was started without `LLM_MODE=live`, or `start.sh` aborted because a model server didn't answer |
| Agent cards show **fallback rules** | The model server is up but erroring or timing out. Check `docker logs nemotron`, and try `UR_GUIDED_JSON=0` |
| Stream stops after ~5 min | Vercel's function limit (300 s). A single incident run should finish well inside it. If it doesn't, the model is too slow. |
| Venue internet dies | Open `http://<spark-ip>:8080` on the LAN. It's the same console, with no Vercel or tunnel needed. |

## B6. Next steps
- P0 UI items from A3, starting with concurrent incidents and outcome-first detail.
- `GET /events?since_id=`, a global decision feed across all jobs for the agent feed (A3 #17) and for screens that join mid-run.
- Persist jobs' event logs to SQLite, so a page reload during a run can replay it.
