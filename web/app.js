"use strict";
// SURU shared-command dashboard. Talks to web_api.py; all simulation logic stays in Python.

const AGENTS = [
  { agency: "FIRE", sender: "NEMOTRON_FIRE", label: "A1 – Fire" },
  { agency: "POLICE", sender: "NEMOTRON_POLICE", label: "A2 – Police" },
  { agency: "AMBULANCE", sender: "NEMOTRON_AMBULANCE", label: "A3 – Ambulance" },
];
const HAZARD = { flood: "Flood", fire: "Fire", trauma: "Trauma" };
const HUB_LETTER = { police: "P", fire: "F", ambulance: "A" };
const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

let api = "";
let data = null;
let map = null;
const markers = new Map();
const hidden = new Set();
let openCam = null;

// ---------- backend ----------
function resolveApi() {
  const fromQuery = new URLSearchParams(location.search).get("api");
  let stored = null;
  try { stored = localStorage.getItem("suru-api"); } catch { /* storage blocked */ }
  return (fromQuery || stored || window.SURU_API_BASE || "").replace(/\/+$/, "");
}
function remember(url) { try { localStorage.setItem("suru-api", url); } catch { /* storage blocked */ } }

async function call(path, method = "GET") {
  const res = await fetch(api + path, { method });
  const body = await res.json().catch(() => ({}));
  if (!res.ok) throw new Error(body.error || `HTTP ${res.status}`);
  return body;
}

function setConn(text, cls) { const el = $("conn"); el.textContent = text; el.className = "conn " + (cls || ""); }

async function connect(url) {
  api = url;
  setConn("Connecting…");
  const [state, roads] = await Promise.all([call("/api/state"), call("/api/roads")]);
  remember(url);
  setConn("Backend connected", "ok");
  drawRoads(roads);
  render(state);
  if (data.pipeline.running && !polling) pollWhileRunning();  // reopened mid-run
  if (!telemTimer) { pollTelemetry(); telemTimer = setInterval(pollTelemetry, 3000); }
}

// The pipeline runs in a backend worker; while it runs, poll so agent messages appear as they land.
let polling = false;
async function action(path) {
  try {
    const live = $("live").checked ? "?live=1" : "";
    render(await call(path + live, "POST"));
    setConn("Backend connected", "ok");
    if (data.pipeline.running && !polling) pollWhileRunning();
  } catch (err) {
    setConn("Request failed: " + err.message, "bad");
  }
}

async function pollWhileRunning() {
  polling = true;
  try {
    while (data.pipeline.running) {
      await new Promise((r) => setTimeout(r, 800));
      render(await call("/api/state"));
    }
  } catch (err) {
    setConn("Lost backend: " + err.message, "bad");
  } finally {
    polling = false;
  }
}

function renderControls(p) {
  ["advance", "retry", "reset"].forEach((id) => ($(id).disabled = p.running));
  $("advance").textContent = p.running ? `Agents running ${Math.floor(p.elapsed_seconds)} s` : "Advance tick";
}

// ---------- compute telemetry ----------
let telem = null;
let telemTimer = null;
const gpuHistory = [];
async function pollTelemetry() {
  try {
    telem = await call("/api/telemetry");
    const gpu = telem.gpu.available ? telem.gpu.gpus[0] : null;
    if (gpu && gpu.util_pct != null) { gpuHistory.push(gpu.util_pct); if (gpuHistory.length > 40) gpuHistory.shift(); }
    renderTelemetry();
    if (data) renderAgents(data);
  } catch { /* connection state is reported by the state calls */ }
}

function meter(label, value, pct, note = "") {
  const width = pct == null ? 0 : Math.max(0, Math.min(100, pct));
  return `<div class="meter"><div class="meter-row"><span>${label}</span><strong>${value}</strong></div>
    <div class="meter-bar"><span style="width:${width}%"></span></div>${note ? `<p class="meter-note">${note}</p>` : ""}</div>`;
}

function historyLine(values) {
  if (values.length < 5) return "";
  const pts = values.map((v, i) => `${(i / 39) * 300},${36 - (v / 100) * 34}`).join(" ");
  return `<svg class="history" viewBox="0 0 300 38" preserveAspectRatio="none" aria-label="GPU utilisation, last two minutes"><polyline points="${pts}"/></svg>`;
}

function renderTelemetry() {
  const t = telem, gpu = t.gpu.available ? t.gpu.gpus[0] : null;
  $("hw-host").textContent = `${gpu ? gpu.name : "No NVIDIA GPU detected"} – host ${t.host}`;
  const unified = gpu && gpu.mem_total_mb == null;
  const parts = [];
  if (gpu) {
    parts.push(meter("GPU utilisation", gpu.util_pct == null ? "n/a" : gpu.util_pct + "%", gpu.util_pct) + historyLine(gpuHistory));
    if (!unified && gpu.mem_total_mb) parts.push(meter("GPU memory", `${(gpu.mem_used_mb / 1024).toFixed(1)} / ${(gpu.mem_total_mb / 1024).toFixed(1)} GB`, (gpu.mem_used_mb / gpu.mem_total_mb) * 100));
  }
  parts.push(meter(unified ? "Unified memory" : "System memory", `${t.memory.used_gb} / ${t.memory.total_gb} GB`, t.memory.pct));
  parts.push(meter("CPU", Math.round(t.cpu_pct) + "%", t.cpu_pct));
  if (gpu) parts.push(`<dl class="stats"><div><dt>Power</dt><dd>${gpu.power_w == null ? "n/a" : Math.round(gpu.power_w) + " W"}</dd></div><div><dt>GPU temp</dt><dd>${gpu.temp_c == null ? "n/a" : Math.round(gpu.temp_c) + " °C"}</dd></div></dl>`);
  const o = t.ollama;
  parts.push(o.reachable
    ? `<div class="loaded"><h3>Models in memory</h3>${o.models.length ? `<ul>${o.models.map((m) => `<li><span>${esc(m.name)}</span><span>${m.size_gb} GB</span></li>`).join("")}</ul>` : `<p class="fine">None loaded. Ollama loads a model on its first call.</p>`}</div>`
    : `<p class="fine">Ollama not reachable at ${esc(o.host)}. Agents run on offline rules.</p>`);
  $("hw").innerHTML = parts.join("");
}

// ---------- agents ----------
const AGENT_ROWS = [
  { sender: "QWEN_TRIAGE", label: "Qwen triage", model: "qwen" },
  { sender: "NEMOTRON_FIRE", label: "A1 – Fire", model: "nemotron" },
  { sender: "NEMOTRON_POLICE", label: "A2 – Police", model: "nemotron" },
  { sender: "NEMOTRON_AMBULANCE", label: "A3 – Ambulance", model: "nemotron" },
  { sender: "A_CR", label: "A_cr – Config resolution", model: null },
  { sender: "QWEN_GOLD_ARBITER", label: "Qwen Gold Arbiter", model: "qwen" },
];

function renderAgents(snap) {
  const p = snap.pipeline, board = snap.state.message_board, tick = snap.state.tick;
  $("pipeline").className = "sys-pipeline " + (p.running ? "running" : p.error ? "error" : "");
  $("pipeline").textContent = p.running ? `Running tick ${tick} with ${p.live ? "live models" : "offline rules"} – ${Math.floor(p.elapsed_seconds)} s`
    : p.error ? `Last run failed: ${p.error}`
    : p.last_seconds != null ? `Idle – last round took ${p.last_seconds} s with ${p.live ? "live models" : "offline rules"}` : "Idle – advance a tick to run the agents";
  $("agents").innerHTML = AGENT_ROWS.map((a) => {
    const mine = board.filter((e) => e.sender === a.sender);
    const last = mine[mine.length - 1];
    const model = a.model ? (p.live ? (telem ? telem.models[a.model] : "…") : "Offline rules") : "Not yet active (#3)";
    const now = last && last.tick === tick;
    const state = !a.model ? "Planned" : now ? (p.running ? "Posted just now" : "Active this tick") : last ? `Last active tick ${last.tick}` : "No activity yet";
    const body = last ? `<span class="agent-last"><b>${esc(last.incident_cam)}</b> ${esc(last.proposal_text)}</span>` : "";
    const tag = last ? "button" : "div";
    return `<li><${tag} class="agent ${now ? "now" : ""} ${a.model ? "" : "planned"}" ${last ? `type="button" data-cam="${esc(last.incident_cam)}"` : ""}>
      <span class="agent-top"><span class="agent-name">${esc(a.label)}</span><span class="agent-state">${state}</span></span>
      <span class="agent-model">${esc(model)}${mine.length ? ` – ${mine.length} message${mine.length > 1 ? "s" : ""}` : ""}</span>${body}</${tag}></li>`;
  }).join("");
}

// ---------- derived state ----------
function sevClass(s) { return s >= 8 ? "high" : s >= 5 ? "mid" : "low"; }
function boardFor(cam) { return data.state.message_board.filter((e) => e.incident_cam === cam); }

function progressOf(inc) {
  const units = Object.entries(data.state.resource_db).filter(([, u]) => u.assigned_cam === inc.cam_id);
  const names = units.map(([id]) => id).join(", ");
  const entries = boardFor(inc.cam_id);
  const last = entries[entries.length - 1];
  if (inc.status === "RESOLVED") return { pct: 100, text: "Resolved" };
  if (inc.status === "UNITS_DISPATCHED") {
    const onScene = units.some(([, u]) => u.status === "DEPLETED_ON_SCENE") || inc.response_ticks != null;
    return onScene ? { pct: 80, text: "On scene – " + names } : { pct: 60, text: "Units en route – " + names };
  }
  // A deadlocked arbiter or pipeline entry is the last word for that tick: dispatch was deferred.
  if (last && last.consensus_status === "DEADLOCK_TIMEOUT" && !isAgency(last))
    return { pct: 45, text: "Deferred: retry next tick", deferred: true };
  if (inc.status === "QWEN_ARBITRATING") return { pct: 45, text: "Arbiter deciding" };
  if (inc.status === "NEMOTRON_NEGOTIATING") return { pct: 30, text: "Agencies proposing" };
  return { pct: 10, text: "Awaiting triage" };
}

// ---------- render ----------
function render(snapshot) {
  data = snapshot;
  const s = data.state;
  $("tick").textContent = "Tick " + s.tick;
  const mode = s.routing.mode;
  $("graph-note").textContent = mode.startsWith("synthetic") ? "Synthetic graph – invented links, not real streets" : "OSM road graph – estimated speeds";
  renderControls(data.pipeline);
  renderAgents(data);
  renderGantry(s);
  renderIncidents(s);
  renderMap(s);
  renderCams(s);
  renderBoard(s);
  renderUnits(s);
  renderRules(s);
  if (openCam && $("highway").open) renderHighway(openCam, true);
}

function renderGantry(s) {
  const m = s.effectiveness_metrics;
  const units = Object.values(s.resource_db);
  const free = units.filter((u) => u.status === "AVAILABLE" && u.total_depletion_ticks_remaining === 0).length;
  const cells = [
    ["Score", Math.round(m.overall_score), "/100", `Spillovers ${m.spillovers_occurred} occurred – ${m.spillovers_prevented} prevented`],
    ["Resolved", m.incidents_resolved, "", "Incidents closed"],
    ["SLA met", m.sla_met_count, "", "First arrival ≤ 7 ticks"],
    ["SLA missed", m.sla_missed_count, "", "First arrival > 7 ticks"],
    ["Mean response", m.avg_response_ticks.toFixed(1), "ticks", "Spawn to first arrival"],
    ["Units free", free, "/ " + units.length, "Dispatchable now"],
  ];
  $("gantry").innerHTML = cells.map(([t, v, u, note]) =>
    `<dl class="kpi"><dt>${t}</dt><dd>${v}<small>${u}</small></dd><p>${note}</p></dl>`).join("");
}

function renderIncidents(s) {
  const all = Object.values(s.active_incidents);
  const active = all.filter((i) => i.status !== "RESOLVED").sort((a, b) => b.severity - a.severity || a.spawn_tick - b.spawn_tick);
  const resolved = all.filter((i) => i.status === "RESOLVED");
  $("inc-count").textContent = `– ${active.length} active`;
  if (!all.length) {
    $("incidents").innerHTML = `<p class="empty">No incidents yet. Advance a tick to start the Seed-42 scenario.</p>`;
    return;
  }
  $("incidents").innerHTML = [...active, ...resolved].map((inc) => {
    const p = progressOf(inc);
    const cam = data.cameras[inc.cam_id];
    return `<article class="incident ${inc.status === "RESOLVED" ? "resolved" : ""}">
      <div class="inc-top"><span>${esc(inc.cam_id)} – since tick ${inc.spawn_tick}</span><span class="sev ${sevClass(inc.severity)}">SEV ${inc.severity}</span></div>
      <h3>${esc(cam ? cam.name : inc.cam_id)}</h3>
      <p class="inc-meta">${HAZARD[inc.hazard] || esc(inc.hazard)}${inc.road_blocked ? " – Road blocked" : ""}</p>
      <div class="progress ${p.deferred ? "deferred" : ""}" role="progressbar" aria-valuemin="0" aria-valuemax="100" aria-valuenow="${p.pct}" aria-label="Response progress"><span style="width:${p.pct}%"></span></div>
      <p class="stage ${p.deferred ? "deferred" : ""}">${esc(p.text)}</p>
      <button class="link-btn" type="button" data-cam="${esc(inc.cam_id)}">Open mediation highway</button>
    </article>`;
  }).join("");
}

// ---------- map ----------
function initMap() {
  if (typeof maplibregl === "undefined") {
    $("map").innerHTML = `<div class="map-fallback">Map library could not load (offline?). Incidents and tables still update.</div>`;
    return;
  }
  // Keyless OpenFreeMap vector basemap (OSM data); overlays are added once it loads.
  map = new maplibregl.Map({
    container: "map",
    bounds: [[-1.53, 53.372], [-1.40, 53.418]],
    // Asymmetric padding leaves room for station tags, which extend right of their markers.
    fitBoundsOptions: { padding: $("map").clientWidth > 600 ? { top: 40, bottom: 50, left: 40, right: 110 } : 16 },
    attributionControl: { compact: true },
    style: "https://tiles.openfreemap.org/styles/fiord",
  });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "bottom-right");
  map.on("load", tintBasemap);
}

// Pull the fiord basemap onto the dashboard's asphalt ground; layers missing from a future style are skipped.
const BASEMAP_PAINT = {
  background: { "background-color": "#0a1a22" }, water: { "fill-color": "#0d2c3a" }, waterway: { "line-color": "#0d2c3a" },
  landuse_residential: { "fill-color": "#0c1e27" }, landcover_wood: { "fill-color": "#0d2421" }, park: { "fill-color": "#0d2421" },
  park_outline: { "line-color": "#12302b" }, building: { "fill-color": "#10242e" }, highway_path: { "line-color": "#16303b" },
  highway_minor: { "line-color": "#2a4552" }, highway_major_casing: { "line-color": "#36586a" }, highway_major_inner: { "line-color": "#1b3441" },
  highway_major_subtle: { "line-color": "#1b3441" }, highway_motorway_casing: { "line-color": "#3c6178" },
  highway_motorway_inner: { "line-color": "#1d3a4c" }, railway: { "line-color": "#1a2f38" },
  highway_name_other: { "text-color": "#7f99a4", "text-halo-color": "#0a1a22" }, highway_ref: { "text-color": "#c9d8df", "text-halo-color": "#1f5aa6" },
  place_other: { "text-color": "#9db3bd", "text-halo-color": "#0a1a22" }, place_suburb: { "text-color": "#9db3bd", "text-halo-color": "#0a1a22" },
  place_village: { "text-color": "#9db3bd", "text-halo-color": "#0a1a22" }, place_town: { "text-color": "#c9d8df", "text-halo-color": "#0a1a22" },
  place_city: { "text-color": "#c9d8df", "text-halo-color": "#0a1a22" }, water_name: { "text-color": "#6f8f9c", "text-halo-color": "#0a1a22" },
};
function tintBasemap() {
  for (const [layer, paint] of Object.entries(BASEMAP_PAINT))
    if (map.getLayer(layer)) for (const [prop, value] of Object.entries(paint)) map.setPaintProperty(layer, prop, value);
}
function stationName(hub) {
  const inParens = hub.name.match(/\(([^)]+)\)/);
  return inParens ? inParens[1] : hub.name.replace(/ (Police Station|Fire Station|Hospital A&E|Hospital)$/, "");
}
const AGENCY_SHORT = { POLICE: "POL", FIRE: "FIRE", AMBULANCE: "AMB" };

function whenMap(fn) { if (!map) return; map.loaded() ? fn() : map.once("load", fn); }

function drawRoads(roads) {
  whenMap(() => {
    const synthetic = roads.mode.startsWith("synthetic");
    // Invented straight links clutter a real street basemap; show them only on request.
    const box = document.querySelector('.layers input[data-layer="roads"]');
    box.checked = !synthetic;
    synthetic ? hidden.add("roads") : hidden.delete("roads");
    box.parentElement.lastChild.textContent = synthetic ? " Synthetic road graph" : " OSM road graph";
    if (map.getSource("roads")) map.getSource("roads").setData(roads.roads);
    else {
      map.addSource("roads", { type: "geojson", data: roads.roads });
      map.addLayer({ id: "roads", type: "line", source: "roads", paint: {
        "line-color": "#5b7f8f", "line-width": synthetic ? 1.5 : 1, "line-opacity": 0.8,
        ...(synthetic ? { "line-dasharray": [2, 2] } : {}) } });
      map.addSource("routes", { type: "geojson", data: { type: "FeatureCollection", features: [] } });
      map.addLayer({ id: "routes", type: "line", source: "routes", layout: { "line-cap": "round", "line-join": "round" },
        paint: { "line-color": "#f2c300", "line-width": 3.5 } });
    }
    applyLayers();
    if (data) renderMap(data.state);
  });
}

function placeMarker(key, lngLat, el, title) {
  // The wrapper keeps MapLibre's positioning classes; only its content is swapped on update.
  let m = markers.get(key);
  if (!m) { m = new maplibregl.Marker({ element: document.createElement("div"), anchor: "center" }).setLngLat(lngLat).addTo(map); markers.set(key, m); }
  else m.setLngLat(lngLat);
  m.getElement().replaceChildren(el);
  // Incidents on top, then station tags (they carry stock text), then moving unit dots.
  m.getElement().style.zIndex = key.startsWith("inc:") ? 3 : key.startsWith("hub:") ? 2 : 1;
  el.title = title;
  return m;
}

function renderMap(s) {
  if (!map || !map.getSource("routes")) return;
  const units = Object.entries(s.resource_db);
  const keep = new Set();
  for (const [hubId, hub] of Object.entries(data.hubs)) {
    const own = units.filter(([, u]) => u.home_hub === hubId);
    const free = own.filter(([, u]) => u.status === "AVAILABLE").length;
    const el = document.createElement("div");
    // shortcut: Royal Hallamshire sits just west of Eyre St, so its tag goes left; replace with collision layout if hubs become data-driven.
    el.className = `mk mk-station ${free ? "" : "depleted"} ${hubId === "NHS_ROYAL_HALLAM" ? "tag-left" : ""}`;
    el.innerHTML = `<i>${HUB_LETTER[hub.type]}</i><span class="mk-tag">${esc(stationName(hub))} <b>${free}/${own.length}</b></span>`;
    placeMarker("hub:" + hubId, [hub.lon, hub.lat], el, `${hub.name}: ${free} of ${own.length} ${own.length ? AGENCY_SHORT[own[0][1].agency] : ""} vehicles free (${own.map(([id, u]) => id + " " + u.status).join(", ")})`);
    keep.add("hub:" + hubId);
  }
  for (const inc of Object.values(s.active_incidents)) {
    if (inc.status === "RESOLVED") continue;
    const el = document.createElement("button");
    el.type = "button";
    el.className = `mk mk-incident ${sevClass(inc.severity)}`;
    el.dataset.cam = inc.cam_id;
    el.innerHTML = `<i></i><span class="mk-tag">${esc(inc.cam_id)} – SEV ${inc.severity}</span>`;
    placeMarker("inc:" + inc.cam_id, [inc.location.lon, inc.location.lat], el, `${data.cameras[inc.cam_id]?.name}: ${HAZARD[inc.hazard]} severity ${inc.severity}. Click for the mediation highway.`);
    keep.add("inc:" + inc.cam_id);
  }
  const features = [];
  for (const [id, u] of units) {
    // Units restocking at their own base are shown by the station's stock, not a separate dot.
    if (u.status === "AVAILABLE" || u.status === "DEPLETED_COOLDOWN") continue;
    const el = document.createElement("div");
    el.className = "mk mk-unit";
    el.innerHTML = "<i></i>";
    placeMarker("unit:" + id, [u.lon, u.lat], el, `${id}: ${u.status}, ${u.total_depletion_ticks_remaining} ticks until available`);
    keep.add("unit:" + id);
    const route = u.phases && u.phases[0] && u.phases[0].route;
    if (route) features.push({ type: "Feature", properties: { id }, geometry: { type: "LineString", coordinates: route.path } });
  }
  map.getSource("routes").setData({ type: "FeatureCollection", features });
  for (const [key, m] of markers) if (!keep.has(key)) { m.remove(); markers.delete(key); }
  applyLayers();
}

function applyLayers() {
  if (!map) return;
  for (const id of ["roads", "routes"]) if (map.getLayer(id)) map.setLayoutProperty(id, "visibility", hidden.has(id) ? "none" : "visible");
  for (const [key, m] of markers) {
    const layer = key.startsWith("hub:") ? "stations" : key.startsWith("unit:") ? "units" : null;
    m.getElement().style.display = layer && hidden.has(layer) ? "none" : "";
  }
}

// ---------- tabs ----------
function renderCams(s) {
  $("tab-cams").innerHTML = `<div class="cams">${Object.entries(data.cameras).map(([cam, c]) => {
    const inc = s.active_incidents[cam];
    const media = c.video ? `<video src="${esc(api)}/api/media/${cam}.mp4" muted loop autoplay playsinline></video>`
      : c.frame ? `<img src="${esc(api)}/api/media/${cam}.jpg" alt="First frame from ${esc(c.name)}">`
      : `<div class="placeholder">Awaiting local Cosmos clip: assets/hero_cams_disaster/${cam}.mp4</div>`;
    const report = inc && inc.qwen_report && inc.qwen_report.summary
      ? `<span>${esc(inc.qwen_report.summary)}</span><span class="muted">Agencies woken: ${esc(inc.qwen_report.required_agencies.join(", "))}</span>`
      : `<span class="muted">${inc ? "Awaiting triage" : "No incident"}</span>`;
    return `<figure class="cam" style="margin:0">${media}<figcaption class="cam-body"><strong>${cam} – ${esc(c.name)}</strong>${report}</figcaption></figure>`;
  }).join("")}</div>`;
}

function statusCell(st) {
  const cls = { AGREED: "agreed", QWEN_OVERRIDDEN: "agreed", DEADLOCK_TIMEOUT: "deadlock" }[st] || "";
  return `<span class="status ${cls}">${esc(st)}</span>`;
}

function boardTable(entries) {
  if (!entries.length) return `<tbody><tr><td class="fine">No board entries yet.</td></tr></tbody>`;
  return `<thead><tr><th>Tick</th><th>Camera</th><th>Sender</th><th>Proposal and reasoning</th><th>Units</th><th>Status</th></tr></thead><tbody>${
    entries.slice().reverse().map((e) => `<tr><td class="num">${e.tick}</td><td>${esc(e.incident_cam)}</td><td>${esc(e.sender)}</td><td>${esc(e.proposal_text)}</td><td>${esc((e.proposed_units || []).join(", "))}</td><td>${statusCell(e.consensus_status)}</td></tr>`).join("")}</tbody>`;
}

function renderBoard(s) {
  $("tab-board").innerHTML = `<div class="table-wrap"><table class="data">${boardTable(s.message_board)}</table></div>`;
}

function renderUnits(s) {
  const rows = Object.entries(s.resource_db).map(([id, u]) => `<tr><td><strong>${id}</strong></td><td>${esc(u.agency)}</td><td>${esc(data.hubs[u.home_hub]?.name || u.home_hub)}</td><td>${esc(u.status)}</td><td>${esc(u.assigned_cam || "")}</td>
    <td class="num">${u.travel_to_scene_ticks}</td><td class="num">${u.on_scene_ticks}</td><td class="num">${u.return_to_base_ticks}</td><td class="num">${u.cooldown_ticks_remaining}</td><td class="num"><strong>${u.total_depletion_ticks_remaining}</strong></td></tr>`).join("");
  $("tab-units").innerHTML = `<div class="table-wrap"><table class="data"><thead><tr><th>Unit</th><th>Agency</th><th>Home base</th><th>Status</th><th>Incident</th><th>Travel</th><th>On scene</th><th>Return</th><th>Cooldown</th><th>Total ticks</th></tr></thead><tbody>${rows}</tbody></table></div>
    <p class="fine">Total = travel + on scene + return + cooldown, in one-minute ticks. On-scene work can extend if the hazard escalates; police hold until severity is below 4. Personnel numbers are not modelled.</p>`;
}

function renderRules(s) {
  const src = Object.entries(data.sources).map(([k, url]) => `<li><a href="${esc(url)}" target="_blank" rel="noopener">${esc(k)}</a></li>`).join("");
  $("tab-rules").innerHTML = `<div class="rules">
    <p><strong>Road graph:</strong> ${esc(s.routing.mode)}. Synthetic graphs use invented links and a 30 km/h assumption; OSM travel times use inferred speeds, not real arrival times.</p>
    <p><strong>Score</strong> = clamp(100 − 5 × SLA misses − 3 × spillovers + resolved, 0, 100). SLA counts first arrival within 7 ticks. These are game metrics.</p>
    <p>Risk priors are synthetic and uncalibrated. Numeric pumps, cordons and cooldowns are simulation rules, not verified SYFR, SYP or YAS procedures. Station coordinates are team-supplied demonstration deployments.</p>
    <p>A1, A2 and A3 are the Nemotron agency agents. A_cr, the config resolution agent grounded in JESIP doctrine, is proposed in issue #3 and not yet active.</p>
    <p>Public context sources:</p><ul>${src}</ul>
    <p class="fine">Simulated output for a hackathon demo. Never operational advice.</p></div>`;
}

// ---------- mediation highway ----------
const POS = {
  triage: [85, 215], consensus: [825, 215], outcome: [1035, 215], arbiter: [930, 370], acr: [455, 215],
  r1: { FIRE: [265, 85], POLICE: [265, 215], AMBULANCE: [265, 345] },
  r2: { FIRE: [645, 85], POLICE: [645, 215], AMBULANCE: [645, 345] },
};
const NW = 150, NH = 50;
const isAgency = (e) => e.sender.startsWith("NEMOTRON_");
const edgeStatus = (st) => ({ AGREED: "agreed", QWEN_OVERRIDDEN: "agreed", DEADLOCK_TIMEOUT: "deadlock" }[st] || "pending");

function highwayModel(cam, tick) {
  const inc = data.state.active_incidents[cam];
  const all = boardFor(cam);
  const es = all.filter((e) => e.tick === tick);
  const triage = all.find((e) => e.sender === "QWEN_TRIAGE" && !e.proposal_text.startsWith("Pipeline deferred"));
  const pipelineFail = es.find((e) => e.sender === "QWEN_TRIAGE" && e.proposal_text.startsWith("Pipeline deferred"));
  const selected = (inc.qwen_report && inc.qwen_report.required_agencies) || [];
  const acrIndex = es.findIndex((e) => e.sender === "A_CR");
  const acr = acrIndex >= 0 ? es[acrIndex] : null;
  const round = (e, i) => e.round ?? (acrIndex >= 0 && i > acrIndex ? 2 : 1);
  const r1 = {}, r2 = {};
  es.forEach((e, i) => { if (isAgency(e)) (round(e, i) === 2 ? r2 : r1)[e.sender.replace("NEMOTRON_", "")] = e; });
  const arb = es.find((e) => e.sender === "QWEN_GOLD_ARBITER");
  const agreed = es.some((e) => isAgency(e) && e.consensus_status === "AGREED");

  const nodes = [{ id: "triage", t1: "Qwen triage", t2: "QWEN_TRIAGE", fill: "#1f5aa6", active: !!triage }];
  for (const a of AGENTS) {
    nodes.push({ id: "r1:" + a.agency, pos: POS.r1[a.agency], t1: a.label, t2: selected.includes(a.agency) ? (acr ? "Round 1 proposal" : a.sender) : "Not woken", fill: "#00703c", active: selected.includes(a.agency) });
    if (acr) nodes.push({ id: "r2:" + a.agency, pos: POS.r2[a.agency], t1: a.label, t2: "Round 2 reply", fill: "#00703c", active: !!r2[a.agency] });
  }
  nodes.push(acr ? { id: "acr", pos: POS.acr, t1: "A_cr", t2: "Config resolution – JESIP", fill: "#1f5aa6", active: true }
                 : { id: "acr", pos: POS.acr, t1: "A_cr", t2: "Not yet active (#3)", fill: "#112836", active: true, planned: true });
  nodes.push({ id: "consensus", t1: "Consensus check", t2: "Deterministic allocator", fill: "#ffffff", light: true, active: es.some(isAgency) });
  if (arb || (!agreed && es.some(isAgency))) nodes.push({ id: "arbiter", t1: "Qwen Gold Arbiter", t2: "QWEN_GOLD_ARBITER", fill: "#1f5aa6", active: !!arb });

  let outcome = { t1: "Pending", t2: "No decision this tick", status: "pending" };
  const derived = (text, status) => ({ sender: "ALLOCATOR (derived from board statuses)", tick, proposal_text: text, proposed_units: [], consensus_status: status });
  if (arb && arb.consensus_status === "QWEN_OVERRIDDEN") outcome = { t1: "Dispatched", t2: arb.proposed_units.join(", "), status: "agreed" };
  else if (arb) outcome = { t1: "Deferred", t2: "No valid allocation", status: "deferred" };
  else if (agreed) outcome = { t1: "Dispatched", t2: es.filter(isAgency).flatMap((e) => e.proposed_units).join(", "), status: "agreed" };
  else if (pipelineFail) outcome = { t1: "Deferred", t2: "Pipeline error", status: "deferred" };
  nodes.push({ id: "outcome", t1: outcome.t1, t2: outcome.t2 || "—", fill: outcome.status === "deferred" ? "#e5484d" : outcome.status === "agreed" ? "#00703c" : "#112836", active: outcome.t1 !== "Pending" });

  const edges = [];
  for (const a of AGENTS) {
    const on = selected.includes(a.agency);
    edges.push({ from: "triage", to: "r1:" + a.agency, entry: on ? triage : null, status: on ? "pending" : "idle", label: on ? `Wake ${a.label}` : `${a.label} not selected by triage` });
    const e1 = r1[a.agency];
    if (!e1) continue;
    if (acr) {
      edges.push({ from: "r1:" + a.agency, to: "acr", entry: e1, status: edgeStatus(e1.consensus_status), label: `${a.label} proposal to A_cr` });
      edges.push({ from: "acr", to: "r2:" + a.agency, entry: acr, status: "pending", label: `A_cr joint picture to ${a.label}` });
      const e2 = r2[a.agency];
      if (e2) edges.push({ from: "r2:" + a.agency, to: "consensus", entry: e2, status: edgeStatus(e2.consensus_status), label: `${a.label} round 2 reply` });
    } else {
      // The highway still runs through A_cr: its lane is drawn idle and today's direct path bypasses it.
      edges.push({ from: "r1:" + a.agency, to: "acr", entry: null, status: "idle", label: `${a.label} to A_cr: mediation not yet active (#3)` });
      const [, y] = POS.r1[a.agency];
      edges.push({ from: "r1:" + a.agency, to: "consensus", entry: e1, status: edgeStatus(e1.consensus_status), label: `${a.label} proposal (bypasses A_cr)`,
        via: y === POS.acr[1] ? [POS.acr[0], POS.acr[1] - 70] : null });
    }
  }
  if (!acr && Object.keys(r1).length) edges.push({ from: "acr", to: "consensus", entry: null, status: "idle", label: "A_cr to consensus: not yet active (#3)" });
  if (agreed && !arb) edges.push({ from: "consensus", to: "outcome", entry: derived("All selected agencies' combined proposals passed deterministic validation.", "AGREED"), status: "agreed", label: "Consensus validated" });
  if (nodes.some((n) => n.id === "arbiter")) {
    edges.push({ from: "consensus", to: "arbiter", entry: derived("No valid consensus (deadlock, timeout or failed validation). Qwen Gold Arbiter takes over.", "DEADLOCK_TIMEOUT"), status: "deadlock", label: "Escalated to arbiter" });
    if (arb) edges.push({ from: "arbiter", to: "outcome", entry: arb, status: arb.consensus_status === "QWEN_OVERRIDDEN" ? "agreed" : "deferred", label: "Arbiter decision" });
  }
  return { inc, nodes, edges, acr };
}

function nodePos(n) { return n.pos || POS[n.id]; }

function renderHighway(cam, keepTick) {
  openCam = cam;
  const inc = data.state.active_incidents[cam];
  const entries = boardFor(cam);
  const ticks = [...new Set(entries.map((e) => e.tick))];
  const select = $("hw-round");
  const prev = keepTick ? Number(select.value) : null;
  select.innerHTML = ticks.map((t) => `<option value="${t}">Tick ${t}</option>`).join("");
  select.value = String(ticks.includes(prev) ? prev : ticks[ticks.length - 1] ?? "");
  $("hw-round-wrap").hidden = ticks.length < 2;
  const tick = Number(select.value);
  $("hw-title").textContent = "Mediation highway – " + (data.cameras[cam]?.name || cam);
  $("hw-sub").textContent = `${cam} – ${HAZARD[inc.hazard]} – severity ${inc.severity}${inc.road_blocked ? " – road blocked" : ""} – ${progressOf(inc).text}`;

  const svg = $("hw-svg");
  if (!ticks.length) {
    svg.innerHTML = `<text x="560" y="215" text-anchor="middle" fill="#9db3bd" font-size="16">No agent activity yet. Advance a tick to run triage.</text>`;
    $("hw-table").innerHTML = boardTable([]);
    $("hw-detail").textContent = "Select a line to read that exchange.";
    return;
  }
  const model = highwayModel(cam, tick);
  const byId = Object.fromEntries(model.nodes.map((n) => [n.id, n]));
  const edgeSvg = model.edges.map((e, i) => {
    const [x1, y1] = nodePos(byId[e.from]), [x2, y2] = nodePos(byId[e.to]);
    const sx = x1 + NW / 2, tx = x2 - NW / 2, dx = Math.max(40, (tx - sx) / 2);
    // The arbiter detour sits below consensus and outcome, so it leaves and rejoins through their bottom edges.
    const d = e.to === "arbiter" ? `M${x1},${y1 + NH / 2} C${x1},${y2} ${x1},${y2} ${x2 - NW / 2},${y2}`
      : e.from === "arbiter" ? `M${x1 + NW / 2},${y1} C${x2},${y1} ${x2},${y1} ${x2},${y2 + NH / 2}`
      : e.via ? `M${sx},${y1} C${sx + 50},${y1} ${e.via[0] - 90},${e.via[1]} ${e.via[0]},${e.via[1]} S${tx - 50},${y2} ${tx},${y2}`
      : `M${sx},${y1} C${sx + dx},${y1} ${tx - dx},${y2} ${tx},${y2}`;
    return `<path class="edge ${e.status}" id="edge-${i}" d="${d}"/>
      <path class="hit" d="${d}" data-edge="${i}" tabindex="${e.entry ? 0 : -1}" role="button" aria-label="${esc(e.label)}"><title>${esc(e.label)}${e.entry ? ": " + esc(e.entry.proposal_text) : ""}</title></path>`;
  }).join("");
  const nodeSvg = model.nodes.map((n) => {
    const [x, y] = nodePos(n);
    return `<g class="node ${n.active ? "" : "idle"} ${n.light ? "light" : ""} ${n.planned ? "planned" : ""}" transform="translate(${x - NW / 2},${y - NH / 2})">
      <rect width="${NW}" height="${NH}" rx="6" fill="${n.fill}"/>
      <text class="t1" x="10" y="21">${esc(n.t1)}</text><text class="t2" x="10" y="38">${esc(String(n.t2).slice(0, 24))}</text></g>`;
  }).join("");
  svg.innerHTML = edgeSvg + nodeSvg;
  svg.querySelectorAll(".hit").forEach((el) => {
    const pick = () => selectEdge(model, Number(el.dataset.edge));
    el.addEventListener("click", pick);
    el.addEventListener("keydown", (ev) => { if (ev.key === "Enter" || ev.key === " ") { ev.preventDefault(); pick(); } });
  });
  $("hw-detail").textContent = model.acr ? "Select a line to read that exchange." : "Select a line to read that exchange. A_cr mediation is not active yet, so agencies do not read each other's proposals.";
  $("hw-table").innerHTML = boardTable(entries.filter((e) => e.tick === tick));
}

function selectEdge(model, i) {
  const e = model.edges[i];
  document.querySelectorAll("#hw-svg .edge").forEach((p) => p.classList.toggle("selected", p.id === "edge-" + i));
  if (!e.entry) { $("hw-detail").innerHTML = `<strong>${esc(e.label)}</strong>`; return; }
  const x = e.entry;
  $("hw-detail").innerHTML = `<strong>${esc(e.label)}</strong>
    <span>${esc(x.sender)} – tick ${x.tick}${x.round ? " – round " + x.round : ""} – ${statusCell(x.consensus_status)}</span>
    <span>${esc(x.proposal_text)}</span>${x.proposed_units && x.proposed_units.length ? `<span>Units: ${esc(x.proposed_units.join(", "))}</span>` : ""}`;
}

function openHighway(cam) {
  renderHighway(cam, false);
  if (!$("highway").open) $("highway").showModal();
}

// ---------- wiring ----------
function showConnect(message) {
  $("connect-error").textContent = message || "";
  $("api-url").value = api || "";
  if (!$("connect").open) $("connect").showModal();
}

document.addEventListener("click", (ev) => {
  const target = ev.target.closest("[data-cam]");
  if (target && data) openHighway(target.dataset.cam);
});
$("hw-close").addEventListener("click", () => $("highway").close());
$("highway").addEventListener("close", () => { openCam = null; });
$("hw-round").addEventListener("change", () => renderHighway(openCam, true));
$("advance").addEventListener("click", () => action("/api/tick"));
$("retry").addEventListener("click", () => action("/api/retry"));
$("reset").addEventListener("click", () => action("/api/reset"));
$("conn").addEventListener("click", () => showConnect());
document.querySelectorAll(".layers input").forEach((box) => box.addEventListener("change", () => {
  box.checked ? hidden.delete(box.dataset.layer) : hidden.add(box.dataset.layer);
  applyLayers();
}));
document.querySelectorAll('[role="tab"]').forEach((tab) => tab.addEventListener("click", () => {
  document.querySelectorAll('[role="tab"]').forEach((t) => t.setAttribute("aria-selected", String(t === tab)));
  document.querySelectorAll(".tabpanel").forEach((p) => (p.hidden = p.id !== "tab-" + tab.dataset.tab));
}));
$("connect-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const url = $("api-url").value.trim().replace(/\/+$/, "");
  try { await connect(url); $("connect").close(); }
  catch (err) { $("connect-error").textContent = `Could not reach ${url}: ${err.message}`; }
});

initMap();
api = resolveApi();
if (api) connect(api).catch((err) => { setConn("Backend unreachable", "bad"); showConnect(`Could not reach ${api}: ${err.message}`); });
else showConnect();
