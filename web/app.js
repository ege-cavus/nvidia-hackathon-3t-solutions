"use strict";
// SURU command dashboard. All backend access goes through window.SURU_API (api.js); with no API address it
// runs the in-browser mock (mock.js) so the UI can be demoed before urban_response is pushed.

// ---------- vocabulary ----------
const AGENCY = {
  police: { label: "Police", color: "#2F6FED" },
  fire: { label: "Fire", color: "#E5484D" },
  ambulance: { label: "Ambulance", color: "#30A46C" },
  shared: { label: "Shared", color: "#8E4EC6" },
};
const AGENTS = {
  Qwen: { label: "Qwen", color: "#3FB8A8" },
  A1: { label: "A1 Police", color: AGENCY.police.color },
  A2: { label: "A2 Ambulance", color: AGENCY.ambulance.color },
  A3: { label: "A3 Fire", color: AGENCY.fire.color },
  CR: { label: "CR", color: "#C9D8DF" },
  rules: { label: "Rules", color: "#7F99A4" },
};
const CATEGORY = {
  rtc: "Road traffic collision", structure_fire: "Structure fire", high_rise_fire: "High-rise fire", firearms: "Firearms",
  assault: "Assault", medical: "Medical emergency", hazmat: "Hazmat", flood: "Flood",
};
const CHIP_OF = { structure_fire: "fire", high_rise_fire: "fire", firearms: "police", assault: "police", medical: "medical", rtc: "traffic", hazmat: "hazmat", flood: "hazmat" };
const SOFT_TEXT = { STAGING_INSIDE_INNER_CORDON: "Staging inside the hot zone", APPROACH_FROM_DOWNWIND: "Approach route is downwind of the hazard" };
const STATUS = {
  pending: ["routing", "… routing"], routing: ["routing", "… routing"], dispatched: ["ok", "✓ dispatched"],
  partial: ["partial", "⚠ partial"], failed: ["failed", "✕ failed"], backlog: ["muted", "backlog"], closed: ["muted", "closed"],
};
const SEV_COLOR = { 5: "#FF5A5F", 4: "#FF8B3D", 3: "#F5B300", 2: "#8FA9B5", 1: "#5E7682" };
const ICON_PATH = {
  police: "M8 1.5 2.5 3.5v4c0 3.3 2.3 5.9 5.5 7 3.2-1.1 5.5-3.7 5.5-7v-4z",
  fire: "M8 1.5c.6 2.3 3.8 3.9 3.8 7.4A3.8 3.8 0 0 1 8 14.6a3.8 3.8 0 0 1-3.8-3.7c0-1.7.9-2.8 1.9-3.6.1 1.1.6 1.9 1.4 2.3C7 7 7.4 3.8 8 1.5z",
  ambulance: "M6 2h4v4h4v4h-4v4H6v-4H2V6h4z",
  shared: "M8 1.5l5.6 3.25v6.5L8 14.5l-5.6-3.25v-6.5z",
};

const $ = (id) => document.getElementById(id);
const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
const cap = (s) => (s ? s[0].toUpperCase() + s.slice(1) : "");
const categoryText = (c) => CATEGORY[c] || cap(String(c || "other").replace(/_/g, " "));
const softText = (code) => SOFT_TEXT[code] || cap(String(code || "").toLowerCase().replace(/_/g, " "));
const chipOf = (inc) => CHIP_OF[inc.category] || "other";
const agencyOf = (a) => AGENCY[a] || AGENCY.shared;
const statusOf = (s) => STATUS[s] || ["muted", s || "unknown"];
const pill = (s) => { const [cls, text] = statusOf(s); return `<span class="pill ${cls}">${esc(text)}</span>`; };
const icon = (agency, lead = false) => `<span class="ag-icon ${lead ? "lead" : ""}" style="--c:${agencyOf(agency).color}" title="${esc(agencyOf(agency).label)}${lead ? " (lead)" : ""}"><svg viewBox="0 0 16 16" aria-hidden="true"><path d="${ICON_PATH[agency] || ICON_PATH.shared}"/></svg></span>`;
const agencyChip = (agency) => `<span class="chip-ag" style="--c:${agencyOf(agency).color}">${esc(agencyOf(agency).label)}</span>`;
const timeOf = (ts) => (ts ? new Date(ts).toLocaleTimeString("en-GB", { hour12: false }) : "");
const minsSince = (ts) => (ts ? Math.max(0, Math.round((Date.now() - new Date(ts).getTime()) / 60000)) : null);
const pct = (n, d) => (d ? Math.round((n / d) * 100) : null);

// ---------- state ----------
const API = window.SURU_API;
const store = { health: null, incidents: [], responses: [], events: [], lastEventId: 0, stations: [] };
const ui = { selected: null, chips: new Set(), tab: "summary", feedPaused: false, feedPending: 0, expandedEvents: new Set(), demoRunning: false };
let baseline = null, lastMetrics = null, lastIncidentCount = 0;

// ---------- data ----------
function latestResponse(id) {
  let best = null;
  for (const r of store.responses) if (r.incident_id === id && (!best || new Date(r.created_at) >= new Date(best.created_at))) best = r;
  return best;
}
const incidentOf = (id) => store.incidents.find((i) => i.id === id);
const visible = (inc) => ui.chips.size === 0 || ui.chips.has(chipOf(inc));

async function poll() {
  try {
    const [incidents, responses, events] = await Promise.all([API.call("incidents"), API.call("responses"), API.call("events", store.lastEventId)]);
    store.incidents = incidents || [];
    store.responses = responses || [];
    if (events && events.length) {
      store.events.push(...events);
      store.lastEventId = Math.max(store.lastEventId, ...events.map((e) => e.id));
      if (ui.feedPaused) ui.feedPending += events.length;
    }
    $("live-dot").classList.toggle("on", !ui.feedPaused);
    renderAll();
  } catch (err) {
    $("live-dot").classList.remove("on");
    setMode("offline", `API unreachable: ${err.message}`);
  }
}

async function pollHealth() {
  try {
    store.health = await API.call("health");
    const h = store.health || {};
    if (API.mode === "mock") setMode("mock", "mock – in-browser demo data");
    else if (h.fallback) setMode("fallback", `${h.mode === "live" ? "live" : "mock"} – fallback in last response`);
    else if (h.mode === "live") setMode("live", `live – ${h.model || "nemotron"}`);
    else setMode("mock", "mock");
  } catch (err) {
    setMode("offline", "API unreachable");
  }
}
function setMode(cls, text) { const el = $("mode"); el.className = "mode " + cls; el.textContent = text; }

// ---------- metrics ----------
function computeMetrics() {
  const latest = store.incidents.map((i) => latestResponse(i.id)).filter(Boolean);
  let requested = 0, allocated = 0, etaTotal = 0, etaMet = 0, hard = 0, autoResolved = 0;
  for (const r of latest) {
    for (const a of r.agent_responses) for (const q of a.requests || []) requested += q.qty || 0;
    allocated += r.allocations.length;
    for (const a of r.allocations) if (a.max_eta_min != null && a.eta_min != null) { etaTotal += 1; if (a.eta_min <= a.max_eta_min) etaMet += 1; }
    for (const h of r.hard_conflicts) { hard += 1; if (!h.overridden) autoResolved += 1; }
  }
  return {
    success: pct(latest.filter((r) => r.status === "dispatched").length, latest.length),
    fill: pct(allocated, requested), eta: pct(etaMet, etaTotal), hard, autoResolved,
  };
}

function renderMetrics() {
  const m = computeMetrics();
  if (store.incidents.length !== lastIncidentCount) { baseline = lastMetrics; lastIncidentCount = store.incidents.length; }
  lastMetrics = m;
  const band = (v) => (v == null ? "none" : v >= 80 ? "good" : v >= 60 ? "warn" : "bad");
  const delta = (key) => {
    if (!baseline || baseline[key] == null || m[key] == null || baseline[key] === m[key]) return "";
    const d = m[key] - baseline[key];
    return `<span class="delta ${d > 0 ? "up" : "down"}">${d > 0 ? "▲" : "▼"} ${Math.abs(d)}</span>`;
  };
  const cell = (key, label, def, headline = false) => `<div class="metric ${headline ? "headline" : ""} ${band(m[key])}" title="${esc(def)}">
      <span class="m-label">${label}</span><span class="m-value">${m[key] == null ? "—" : m[key] + "%"}${delta(key)}</span></div>`;
  $("metrics").innerHTML =
    cell("success", "Response success", "Share of incidents whose latest response is dispatched: every essential request met.", true) +
    cell("fill", "Fill rate", "Allocated units ÷ requested units across the latest responses.") +
    cell("eta", "ETA met", "Allocations arriving within the requesting agent's maximum ETA ÷ allocations with an ETA requirement.") +
    `<div class="metric ${band(pct(m.autoResolved, m.hard))}" title="Hard conflicts where CR's choice passed re-validation ÷ all hard conflicts.">
      <span class="m-label">Conflicts auto-resolved</span><span class="m-value">${m.hard ? `${m.autoResolved}/${m.hard}` : "—"}</span></div>`;
}

// ---------- incident list ----------
function card(inc) {
  const routed = inc.routed_agencies || [];
  const mins = minsSince(inc.received_at);
  return `<button type="button" class="card ${ui.selected === inc.id ? "selected" : ""}" data-select="${esc(inc.id)}" aria-pressed="${ui.selected === inc.id}">
    <span class="card-top"><i class="sev-dot" style="background:${SEV_COLOR[inc.severity] || SEV_COLOR[1]}"></i><b>${esc(inc.id)}</b><span class="sev" style="--c:${SEV_COLOR[inc.severity] || SEV_COLOR[1]}">SEV ${inc.severity}</span></span>
    <span class="card-cat">${esc(categoryText(inc.category))}</span>
    <span class="card-meta">${esc(inc.district || inc.address)}${mins != null ? ` – ${mins} min` : ""}</span>
    <span class="card-foot">${pill(inc.status)}<span class="icons">${routed.map((a) => icon(a, a === inc.lead_agency)).join("")}</span></span>
  </button>`;
}

function renderList() {
  const shown = store.incidents.filter(visible).sort((a, b) => b.severity - a.severity || new Date(b.received_at) - new Date(a.received_at));
  const ongoing = shown.filter((i) => i.status !== "closed" && i.status !== "backlog");
  const backlog = shown.filter((i) => i.status === "backlog");
  const closed = shown.filter((i) => i.status === "closed");
  $("ongoing-count").textContent = ongoing.length ? `– ${ongoing.length}` : "";
  $("ongoing").innerHTML = ongoing.length ? ongoing.map(card).join("")
    : `<p class="empty">${store.incidents.length ? "No ongoing incidents match this filter." : "No incidents yet. Press ▶ Run demo scenario."}</p>`;
  $("backlog-count").textContent = `(${backlog.length})`;
  $("backlog").innerHTML = backlog.map(card).join("");
  $("closed-count").textContent = `(${closed.length})`;
  $("closed").innerHTML = closed.map(card).join("");
  const hasOther = store.incidents.some((i) => chipOf(i) === "other");
  document.querySelector('[data-chip="other"]').hidden = !hasOther;
}

// ---------- map ----------
let map = null;
const routingMarkers = new Map();
const dynMarkers = [];
const ringCoords = (lat, lon, m) => {
  const pts = [];
  for (let i = 0; i <= 64; i++) {
    const t = (i / 64) * 2 * Math.PI;
    pts.push([lon + (m / (111320 * Math.cos((lat * Math.PI) / 180))) * Math.cos(t), lat + (m / 111320) * Math.sin(t)]);
  }
  return pts;
};
const BASEMAP_PAINT = {
  background: { "background-color": "#0a1a22" }, water: { "fill-color": "#0d2c3a" }, waterway: { "line-color": "#0d2c3a" },
  landuse_residential: { "fill-color": "#0c1e27" }, landcover_wood: { "fill-color": "#0d2421" }, park: { "fill-color": "#0d2421" },
  park_outline: { "line-color": "#12302b" }, building: { "fill-color": "#10242e" }, highway_path: { "line-color": "#16303b" },
  highway_minor: { "line-color": "#2a4552" }, highway_major_casing: { "line-color": "#36586a" }, highway_major_inner: { "line-color": "#1b3441" },
  highway_major_subtle: { "line-color": "#1b3441" }, highway_motorway_casing: { "line-color": "#3c6178" }, highway_motorway_inner: { "line-color": "#1d3a4c" },
  railway: { "line-color": "#1a2f38" }, highway_name_other: { "text-color": "#7f99a4", "text-halo-color": "#0a1a22" },
  place_other: { "text-color": "#9db3bd", "text-halo-color": "#0a1a22" }, place_suburb: { "text-color": "#9db3bd", "text-halo-color": "#0a1a22" },
  place_village: { "text-color": "#9db3bd", "text-halo-color": "#0a1a22" }, place_town: { "text-color": "#c9d8df", "text-halo-color": "#0a1a22" },
  place_city: { "text-color": "#c9d8df", "text-halo-color": "#0a1a22" },
};

function triangleImage(color) {
  const s = 28, c = document.createElement("canvas");
  c.width = c.height = s;
  const g = c.getContext("2d");
  g.beginPath(); g.moveTo(s / 2, 4); g.lineTo(s - 4, s - 5); g.lineTo(4, s - 5); g.closePath();
  g.fillStyle = color; g.fill(); g.lineWidth = 2.5; g.strokeStyle = "#0a1a22"; g.stroke();
  return g.getImageData(0, 0, s, s);
}

function initMap() {
  if (typeof maplibregl === "undefined") {
    $("map").innerHTML = `<p class="map-fallback">The map library could not load (offline?). Incidents, details and the agent feed still work.</p>`;
    return;
  }
  map = new maplibregl.Map({ container: "map", style: "https://tiles.openfreemap.org/styles/fiord", center: [-1.4701, 53.3811], zoom: 12, attributionControl: { compact: true } });
  map.addControl(new maplibregl.NavigationControl({ showCompass: false }), "top-right");
  new ResizeObserver(() => map.resize()).observe($("map"));  // the drawer opening and closing changes the map's height
  map.on("load", () => {
    for (const [layer, paint] of Object.entries(BASEMAP_PAINT))
      if (map.getLayer(layer)) for (const [k, v] of Object.entries(paint)) map.setPaintProperty(layer, k, v);
    for (const [a, meta] of Object.entries(AGENCY)) map.addImage("st-" + a, triangleImage(meta.color), { pixelRatio: 2 });
    const empty = { type: "FeatureCollection", features: [] };
    for (const id of ["stations", "incidents", "alloc", "cordons"]) map.addSource(id, { type: "geojson", data: empty });
    map.addLayer({ id: "cordon-inner", type: "line", source: "cordons", filter: ["==", ["get", "ring"], "inner"], paint: { "line-color": ["get", "color"], "line-width": 2 } });
    map.addLayer({ id: "cordon-outer", type: "line", source: "cordons", filter: ["==", ["get", "ring"], "outer"], paint: { "line-color": ["get", "color"], "line-width": 2, "line-dasharray": [3, 2] } });
    map.addLayer({ id: "cordon-evac", type: "line", source: "cordons", filter: ["==", ["get", "ring"], "evac"], paint: { "line-color": ["get", "color"], "line-width": 2, "line-dasharray": [0.6, 1.6] } });
    map.addLayer({ id: "alloc", type: "line", source: "alloc", layout: { "line-cap": "round" }, paint: { "line-color": ["get", "color"], "line-width": 2.5, "line-opacity": 0.9 } });
    map.addLayer({ id: "stations", type: "symbol", source: "stations", layout: { "icon-image": ["concat", "st-", ["get", "agency"]], "icon-allow-overlap": true } });
    map.addLayer({ id: "incidents", type: "circle", source: "incidents", paint: {
      "circle-radius": ["+", 5, ["*", 2.6, ["get", "severity"]]], "circle-color": ["get", "color"], "circle-opacity": 0.92,
      "circle-stroke-color": ["case", ["get", "selected"], "#f2c300", "#ffffff"], "circle-stroke-width": ["case", ["get", "selected"], 3.5, 1.5] } });
    const tip = new maplibregl.Popup({ closeButton: false, closeOnClick: false, className: "tip", offset: 12 });
    map.on("mousemove", "incidents", (e) => {
      const p = e.features[0].properties;
      map.getCanvas().style.cursor = "pointer";
      tip.setLngLat(e.lngLat).setHTML(`<b>${esc(p.id)}</b> – ${esc(categoryText(p.category))} – SEV ${p.severity} – ${esc(p.status)}`).addTo(map);
    });
    map.on("mouseleave", "incidents", () => { map.getCanvas().style.cursor = ""; tip.remove(); });
    map.on("click", "incidents", (e) => select(e.features[0].properties.id));
    map.on("mousemove", "stations", (e) => tip.setLngLat(e.lngLat).setHTML(`${esc(e.features[0].properties.name)} – ${esc(agencyOf(e.features[0].properties.agency).label)}`).addTo(map));
    map.on("mouseleave", "stations", () => tip.remove());
    renderStations();
    renderMap();
  });
}

function renderStations() {
  if (!map || !map.getSource("stations")) return;
  map.getSource("stations").setData({ type: "FeatureCollection", features: store.stations.map((s) => ({
    type: "Feature", properties: { name: s.name, agency: AGENCY[s.agency] ? s.agency : "shared" }, geometry: { type: "Point", coordinates: [s.lon, s.lat] } })) });
}

function statusColor(s) { return { ok: "#4cc38a", partial: "#F5B300", failed: "#FF5A5F", routing: "#c9d8df", muted: "#5E7682" }[statusOf(s)[0]]; }

function stationCoord(a) {
  const s = store.stations.find((x) => x.id === a.station_id) || store.stations.find((x) => x.name === a.station);
  return s ? [s.lon, s.lat] : null;
}

function renderMap() {
  if (!map || !map.getSource("incidents")) return;
  const shown = store.incidents.filter((i) => visible(i) && i.status !== "closed" && Number.isFinite(i.lat));
  map.getSource("incidents").setData({ type: "FeatureCollection", features: shown.map((i) => ({ type: "Feature",
    properties: { id: i.id, category: i.category, severity: i.severity, status: i.status, color: statusColor(i.status), selected: i.id === ui.selected },
    geometry: { type: "Point", coordinates: [i.lon, i.lat] } })) });
  // Pulsing ring for incidents the agents are still routing.
  const routingIds = new Set(shown.filter((i) => statusOf(i.status)[0] === "routing").map((i) => i.id));
  for (const [id, m] of routingMarkers) if (!routingIds.has(id)) { m.remove(); routingMarkers.delete(id); }
  for (const i of shown) if (routingIds.has(i.id) && !routingMarkers.has(i.id)) {
    const el = document.createElement("div"); el.className = "pulse";
    routingMarkers.set(i.id, new maplibregl.Marker({ element: el }).setLngLat([i.lon, i.lat]).addTo(map));
  }
  renderSelectionLayers();
}

function renderSelectionLayers() {
  if (!map || !map.getSource("alloc")) return;
  dynMarkers.splice(0).forEach((m) => m.remove());
  const inc = ui.selected && incidentOf(ui.selected);
  const r = inc && latestResponse(inc.id);
  const allocFeatures = [], cordonFeatures = [];
  if (inc && r && inc.status !== "closed") {
    for (const a of r.allocations) {
      const from = stationCoord(a);
      if (!from) continue;
      allocFeatures.push({ type: "Feature", properties: { color: agencyOf(a.agency).color }, geometry: { type: "LineString", coordinates: [from, [inc.lon, inc.lat]] } });
    }
    // One label per station keeps the line labels readable when several units leave the same base.
    const byStation = new Map();
    for (const a of r.allocations) { const k = a.station_id || a.station; if (!byStation.has(k)) byStation.set(k, []); byStation.get(k).push(a); }
    for (const list of byStation.values()) {
      const from = stationCoord(list[0]);
      if (!from) continue;
      const el = document.createElement("div");
      el.className = "line-label";
      el.style.setProperty("--c", agencyOf(list[0].agency).color);
      el.textContent = list.map((a) => `${a.callsign} ${a.eta_min ?? "?"}′`).join(" – ");
      dynMarkers.push(new maplibregl.Marker({ element: el }).setLngLat([(from[0] * 2 + inc.lon) / 3, (from[1] * 2 + inc.lat) / 3]).addTo(map));
    }
    for (const p of r.agent_responses) {
      const sc = p.scene_constraints || {}, color = agencyOf(p.agency).color;
      for (const [ring, m] of [["inner", sc.inner_cordon_m], ["outer", sc.outer_cordon_m], ["evac", sc.evacuation_radius_m]])
        if (m) cordonFeatures.push({ type: "Feature", properties: { ring, color }, geometry: { type: "LineString", coordinates: ringCoords(inc.lat, inc.lon, m) } });
      if (sc.staging && Number.isFinite(sc.staging.lat)) {
        const el = document.createElement("div");
        el.className = "staging";
        el.style.setProperty("--c", color);
        el.innerHTML = `<i></i><span>Staging – ${esc(agencyOf(p.agency).label)}</span>`;
        el.title = sc.staging.name || "Staging area";
        dynMarkers.push(new maplibregl.Marker({ element: el, anchor: "left" }).setLngLat([sc.staging.lon, sc.staging.lat]).addTo(map));
      }
    }
  }
  map.getSource("alloc").setData({ type: "FeatureCollection", features: allocFeatures });
  map.getSource("cordons").setData({ type: "FeatureCollection", features: cordonFeatures });
}

// ---------- detail drawer ----------
let drawerSig = "", feedSig = "";
function renderDrawer() {
  const drawer = $("drawer");
  const inc = ui.selected && incidentOf(ui.selected);
  document.body.classList.toggle("has-selection", !!inc);
  if (!inc) { drawerSig = ""; drawer.innerHTML = `<p class="empty">Select an incident in the list or on the map to see why the agents decided what they did.</p>`; return; }
  const r = latestResponse(inc.id);
  const history = store.responses.filter((x) => x.incident_id === inc.id);
  // Polls arrive every 2 s; re-render only on change so open "Raw" panels and text selections survive.
  const sig = JSON.stringify([inc, r, history.length, ui.tab]);
  if (sig === drawerSig) return;
  drawerSig = sig;
  const tabs = [["summary", "Summary"], ["resources", "Resources"], ["agencies", "Agencies"], ["conflicts", `Conflicts${r && (r.hard_conflicts.length + r.soft_conflicts.length) ? ` (${r.hard_conflicts.length + r.soft_conflicts.length})` : ""}`], ["history", `History (${history.length})`]];
  const scroll = drawer.querySelector(".d-body")?.scrollTop || 0;
  drawer.innerHTML = `<div class="d-head">
      <h2><span class="mono">${esc(inc.id)}</span> – ${esc(categoryText(inc.category))} – <span style="color:${SEV_COLOR[inc.severity]}">SEV ${inc.severity}</span>${inc.district ? ` – ${esc(inc.district)}` : ""}</h2>
      ${pill(inc.status)}
      <div class="d-actions">
        ${inc.status === "failed" ? `<button class="btn small" type="button" data-act="retry">Retry</button>` : ""}
        ${inc.status !== "closed" ? `<button class="btn small" type="button" data-act="close-incident">Close incident</button>` : ""}
        <button class="btn small ghost" type="button" data-act="deselect" aria-label="Hide detail">Hide</button>
      </div>
    </div>
    <div class="tabs" role="tablist">${tabs.map(([k, t]) => `<button role="tab" type="button" data-tab="${k}" aria-selected="${ui.tab === k}">${t}</button>`).join("")}</div>
    <div class="d-body">${r || ui.tab === "summary" ? TAB[ui.tab](inc, r, history) : `<p class="empty">The agents are still routing this incident.</p>`}</div>`;
  drawer.querySelector(".d-body").scrollTop = scroll;
}

const listOr = (items, empty = "None") => (items && items.length ? `<ul>${items.map((x) => `<li>${esc(x)}</li>`).join("")}</ul>` : `<p class="muted">${empty}</p>`);
const execBadge = (by) => `<span class="exec ${by === "fallback" ? "fallback" : by === "mock" ? "mock" : by === "rules" ? "rules" : "model"}">${esc(by || "unknown")}</span>`;
const raw = (obj) => `<details class="raw"><summary>Raw</summary><pre>${esc(JSON.stringify(obj, null, 2))}</pre></details>`;

const TAB = {
  summary(inc, r) {
    const routing = r ? r.routing : { lead: inc.lead_agency, agencies: inc.routed_agencies };
    const agencies = (routing.agencies || []).map((a) => `${agencyOf(a).label}${a === routing.lead ? " (lead)" : ""}`).join(", ");
    return `<div class="summary">
      <div>
        <p class="desc">${esc(inc.description)}</p>
        <dl class="facts">
          <div><dt>Casualties</dt><dd>${inc.casualties ?? 0}</dd></div><div><dt>Trapped</dt><dd>${inc.trapped ?? 0}</dd></div>
          <div><dt>Hazards</dt><dd>${esc((inc.hazards || []).join(", ") || "None")}</dd></div><div><dt>Weapons</dt><dd>${esc(inc.weapons || "None")}</dd></div>
        </dl>
        <p class="routing">${agencies ? `<b>Qwen routed to ${esc(agencies)}</b>${routing.rationale ? `: ${esc(routing.rationale)}` : ""}` : "Waiting for Qwen to route this incident."}</p>
      </div>
      <div class="directives">
        <h3>Unified directives (CR)</h3>
        ${r && r.directives.length ? `<ol>${r.directives.map((d) => `<li>${esc(d)}</li>`).join("")}</ol>` : `<p class="muted">CR has not issued directives yet.</p>`}
        ${r && r.cr_rationale ? `<p class="muted small">${esc(r.cr_rationale)}</p>` : ""}
      </div>
    </div>`;
  },
  resources(inc, r) {
    const rows = r.allocations.map((a) => `<tr><td class="mono">${esc(a.callsign)}</td><td>${esc(a.type_name || a.resource_type)} <span class="code">${esc(a.resource_type)}</span></td>
      <td>${agencyChip(a.agency)}</td><td>${esc(a.station)}</td>
      <td class="num ${a.max_eta_min != null && a.eta_min > a.max_eta_min ? "late" : ""}">${a.eta_min ?? "—"} min${a.max_eta_min != null ? ` <span class="muted">/ ${a.max_eta_min}</span>` : ""}</td>
      <td>${esc(a.requested_by || "")}</td><td>${a.preempted_from ? `<span class="preempt">${esc(a.preempted_from)}</span>` : ""}</td></tr>`).join("");
    const unmet = r.unmet.length ? `<div class="unmet"><h3>Unmet needs</h3><ul>${r.unmet.map((u) => `<li><b>${u.qty}× ${esc(u.type_name || u.resource_type)}</b>${u.essential ? " (essential)" : ""}${u.mutual_aid ? ` – ${esc(u.mutual_aid)}` : ""}</li>`).join("")}</ul></div>` : "";
    return `<div class="table-wrap"><table class="data"><thead><tr><th>Callsign</th><th>Type</th><th>Agency</th><th>Station</th><th class="num">ETA / max</th><th>Requested by</th><th>Pre-empted from</th></tr></thead>
      <tbody>${rows || `<tr><td colspan="7" class="muted">No units allocated.</td></tr>`}</tbody></table></div>${unmet}`;
  },
  agencies(inc, r) {
    return `<div class="agent-cards">${r.agent_responses.map((p) => `<article class="agent-card" style="--c:${agencyOf(p.agency).color}">
      <header><span class="agent-chip" style="--c:${agencyOf(p.agency).color}">${esc(AGENTS[p.agent_id]?.label || p.agent_id)}</span>${execBadge(p.executed_by)}${p.latency_ms != null ? `<span class="muted small">${(p.latency_ms / 1000).toFixed(1)} s</span>` : ""}</header>
      <p>${esc(p.summary)}</p>
      ${p.confidence != null ? `<div class="conf" title="Model confidence"><span style="width:${Math.round(p.confidence * 100)}%"></span></div><p class="muted small">Confidence ${Math.round(p.confidence * 100)}%</p>` : ""}
      <h4>Tactical plan</h4>${listOr(p.tactical_plan)}
      <h4>Hazards</h4>${listOr(p.hazards)}
      <h4>Safety</h4>${listOr(p.safety_notes)}
      <h4>Knowledge base</h4>${listOr(p.kb_citations, "No citations")}
      ${raw(p)}</article>`).join("") || `<p class="muted">No agency proposals.</p>`}</div>`;
  },
  conflicts(inc, r) {
    const hard = r.hard_conflicts.map((h) => `<article class="conflict hard ${h.overridden ? "overridden" : ""}">
      <header><b>${esc(cap(h.kind || "conflict"))}: ${esc(h.type_name || h.resource_type)}</b><span class="muted">${h.requested} requested, ${h.available} free</span></header>
      <p><b>CR resolution:</b> ${esc(h.resolution)}</p><p class="muted">${esc(h.rationale || "")}</p>
      ${h.overridden ? `<p class="override">CR's first choice failed re-validation; rules applied ${esc(h.fallback || "a fallback")}.</p>` : ""}</article>`).join("");
    const soft = r.soft_conflicts.map((s) => `<article class="conflict soft">
      <header><b>${esc(s.title || softText(s.code))}</b><span class="tag ${esc(s.source)}">${esc(s.source || "")}</span></header>
      <p class="code">${esc(s.code || "")}</p>
      <p>${(s.agencies || []).map(agencyChip).join(" ")} <span class="muted">severity ${esc(s.severity || "")}</span></p>
      <p><b>Suggested:</b> ${esc(s.resolution || s.suggested_resolution || "")}</p></article>`).join("");
    return `<div class="conflicts"><section><h3>Hard conflicts</h3>${hard || `<p class="muted">None: every request was met from free units.</p>`}</section>
      <section><h3>Soft conflicts</h3>${soft || `<p class="muted">None detected.</p>`}</section></div>`;
  },
  history(inc, r, history) {
    return `<table class="data"><thead><tr><th>Time</th><th>Response</th><th>Status</th><th class="num">Units</th><th class="num">Unmet</th></tr></thead><tbody>${
      history.slice().reverse().map((h) => `<tr><td>${timeOf(h.created_at)}</td><td class="mono">${esc(h.id)}</td><td>${pill(h.status)}</td><td class="num">${h.allocations.length}</td><td class="num">${h.unmet.reduce((n, u) => n + (u.qty || 0), 0)}</td></tr>`).join("")}</tbody></table>`;
  },
};

// ---------- agent observability feed ----------
function renderFeed() {
  if (ui.feedPaused) {
    $("feed-new").hidden = ui.feedPending === 0;
    $("feed-new").textContent = `${ui.feedPending} new – resume`;
    return;
  }
  $("feed-new").hidden = true;
  const agentFilter = $("feed-agent").value;
  const follow = $("feed-follow").checked && ui.selected;
  const rows = store.events.filter((e) => {
    const inc = e.incident_id && incidentOf(e.incident_id);
    if (inc && !visible(inc)) return false;
    if (agentFilter && e.agent_id !== agentFilter) return false;
    if (follow && e.incident_id !== ui.selected) return false;
    return true;
  }).slice(-200).reverse();
  const sig = JSON.stringify([rows.map((e) => e.id), [...ui.expandedEvents], ui.selected]);
  if (sig === feedSig) return;
  feedSig = sig;
  if (!rows.length) { $("feed").innerHTML = `<li class="empty">${store.events.length ? "No decisions match these filters." : "Agent decisions appear here as they happen."}</li>`; return; }
  $("feed").innerHTML = rows.map((e) => {
    const meta = AGENTS[e.agent_id] || { label: e.agent_id, color: "#7F99A4" };
    const open = ui.expandedEvents.has(e.id);
    const detail = e.payload && (e.payload.rationale || e.payload.summary);
    return `<li class="ev ${e.incident_id === ui.selected ? "hl" : ""} t-${esc(String(e.type).toLowerCase())}">
      <button type="button" class="ev-row" data-event="${e.id}" aria-expanded="${open}">
        <span class="ev-top"><time>${timeOf(e.ts)}</time><span class="agent-chip ${e.executed_by === "fallback" ? "fallback" : e.executed_by === "mock" ? "mock" : ""}" style="--c:${meta.color}">${esc(meta.label)}</span><span class="ev-type">${esc(e.type)}</span>${e.incident_id ? `<span class="ev-inc mono">${esc(e.incident_id)}</span>` : ""}</span>
        <span class="ev-sum">${esc(e.summary)}</span>
      </button>
      ${open ? `<div class="ev-more">${detail ? `<p>${esc(detail)}</p>` : ""}<p class="muted small">executed by ${esc(e.executed_by || "unknown")}${e.incident_id ? ` – <button type="button" class="link" data-select="${esc(e.incident_id)}">open ${esc(e.incident_id)}</button>` : ""}</p>${raw(e.payload)}</div>` : ""}
    </li>`;
  }).join("");
}

// ---------- resource database ----------
const resUi = { agency: "", expanded: new Set(), units: {}, forecast: {} };
async function renderResources() {
  if (!$("resources-dialog").open) return;
  const [types, hospitals] = await Promise.all([API.call("resources"), API.call("hospitals")]);
  for (const code of resUi.expanded) {
    [resUi.units[code], resUi.forecast[code]] = await Promise.all([API.call("units", { resource_type: code }), API.call("forecast", code)]);
  }
  const rows = (types || []).filter((t) => !resUi.agency || t.agency === resUi.agency).map((t) => {
    const share = t.total ? t.available / t.total : 0;
    const band = t.available === 0 ? "bad" : share < 0.3 ? "warn" : "good";
    const open = resUi.expanded.has(t.code);
    const units = resUi.units[t.code] || [];
    const fc = resUi.forecast[t.code];
    return `<tr class="type-row"><td>${agencyChip(t.agency)}</td>
      <td><button type="button" class="link" data-type="${esc(t.code)}" aria-expanded="${open}">${esc(t.name)} <span class="code">(${esc(t.code)})</span></button></td>
      <td class="num">${t.total}</td>
      <td class="num"><span class="avail ${band}"><span class="avail-bar"><span style="width:${Math.round(share * 100)}%"></span></span>${t.available}</span></td>
      <td class="num">${t.committed}</td><td class="num">${t.out_of_service}</td></tr>
      ${open ? `<tr class="expand"><td colspan="6"><div class="expand-grid">
        <table class="data inner"><thead><tr><th>Callsign</th><th>Station</th><th>Status</th><th>Incident</th><th></th></tr></thead><tbody>${
          units.map((u) => `<tr><td class="mono">${esc(u.callsign)}</td><td>${esc(store.stations.find((s) => s.id === u.station_id)?.name || u.station_id)}</td><td>${esc(u.status.replace(/_/g, " "))}</td>
          <td class="mono">${esc(u.incident_id || "")}</td><td>${u.status === "committed" ? `<button type="button" class="btn small" data-release="${esc(u.id)}">Release</button>` : ""}</td></tr>`).join("")}</tbody></table>
        ${fc ? forecastChart(fc) : ""}</div></td></tr>` : ""}`;
  }).join("");
  $("resources-body").innerHTML = `<div class="table-wrap"><table class="data"><thead><tr><th>Agency</th><th>Type</th><th class="num">Total</th><th class="num">Available</th><th class="num">Committed</th><th class="num">Out of service</th></tr></thead><tbody>${rows}</tbody></table></div>`;
  $("hospitals").innerHTML = `<table class="data"><thead><tr><th>Hospital</th><th class="num">ED capacity</th><th>Major trauma</th><th>Paediatric</th></tr></thead><tbody>${
    (hospitals || []).map((h) => `<tr><td>${esc(h.name)}</td><td class="num">${h.ed_capacity ?? "—"}</td><td>${h.major_trauma ? "Yes" : "No"}</td><td>${h.paediatric ? "Yes" : "No"}</td></tr>`).join("")}</tbody></table>`;
}

function forecastChart(fc) {
  const w = 260, h = 90, max = Math.max(1, fc.total || Math.max(...fc.points.map((p) => p.available)));
  const x = (m) => 8 + (m / 120) * (w - 16), y = (v) => h - 18 - (v / max) * (h - 30);
  let d = "";
  fc.points.forEach((p, i) => { d += i ? `H${x(p.minutes)}V${y(p.available)}` : `M${x(p.minutes)},${y(p.available)}`; });
  return `<figure class="forecast"><figcaption>Available over the next 2 hours</figcaption>
    <svg viewBox="0 0 ${w} ${h}" role="img" aria-label="Forecast of available ${esc(fc.resource_type)} units">
      <line x1="8" x2="${w - 8}" y1="${y(0)}" y2="${y(0)}" class="axis"/><path d="${d}" class="step"/>
      ${[0, 60, 120].map((m) => `<text x="${x(m)}" y="${h - 4}" text-anchor="middle">${m ? `+${m}m` : "now"}</text>`).join("")}
      <text x="${w - 8}" y="${y(max) - 4}" text-anchor="end">${max}</text></svg></figure>`;
}

// ---------- actions ----------
function select(id) {
  if (id !== ui.selected) ui.tab = "summary";
  ui.selected = id;
  $("legend").open = false;  // the map is shorter with the drawer open; keep cordons and staging pins clear
  renderAll();
  const inc = incidentOf(id);
  // Fly after the drawer has opened, so the incident lands in the centre of the resized map.
  if (map && inc && Number.isFinite(inc.lat)) requestAnimationFrame(() => { map.resize(); map.flyTo({ center: [inc.lon, inc.lat], zoom: 14.3, speed: 1.4 }); });
}
function deselect() { ui.selected = null; $("legend").open = true; renderAll(); }

async function runDemo() {
  if (ui.demoRunning) return;
  ui.demoRunning = true;
  const btn = $("run-demo");
  btn.disabled = true;
  try {
    const scenario = await API.call("scenario");
    for (let i = 0; i < scenario.length; i++) {
      btn.textContent = `Running demo ${i + 1}/${scenario.length}…`;
      await API.call("createIncident", scenario[i]);
      poll();
      if (i < scenario.length - 1) await new Promise((r) => setTimeout(r, 3000));
    }
  } catch (err) {
    setMode("offline", `Demo failed: ${err.message}`);
  } finally {
    ui.demoRunning = false;
    btn.disabled = false;
    btn.textContent = "▶ Run demo scenario";
  }
}

function renderAll() {
  renderMetrics();
  renderList();
  renderMap();
  renderDrawer();
  renderFeed();
  if ($("resources-dialog").open) renderResources();
}

// ---------- wiring ----------
document.addEventListener("click", async (ev) => {
  const t = ev.target.closest("button, [data-select]");
  if (!t) return;
  if (t.dataset.select) return select(t.dataset.select);
  if (t.dataset.tab) { ui.tab = t.dataset.tab; return renderDrawer(); }
  if (t.dataset.event) { const id = Number(t.dataset.event); ui.expandedEvents.has(id) ? ui.expandedEvents.delete(id) : ui.expandedEvents.add(id); return renderFeed(); }
  if (t.dataset.type) { resUi.expanded.has(t.dataset.type) ? resUi.expanded.delete(t.dataset.type) : resUi.expanded.add(t.dataset.type); return renderResources(); }
  if (t.dataset.release) { await API.call("releaseUnit", t.dataset.release); await poll(); return renderResources(); }
  if (t.dataset.close !== undefined) return t.closest("dialog").close();
  if (t.dataset.act === "deselect") return deselect();
  if (t.dataset.act === "close-incident") { await API.call("closeIncident", ui.selected); return poll(); }
  if (t.dataset.act === "retry") { await API.call("retryIncident", ui.selected); return poll(); }
  if (t.dataset.chip) {
    const chip = t.dataset.chip;
    if (chip === "all") ui.chips.clear(); else ui.chips.has(chip) ? ui.chips.delete(chip) : ui.chips.add(chip);
    document.querySelectorAll("[data-chip]").forEach((b) => b.setAttribute("aria-pressed", String(b.dataset.chip === "all" ? ui.chips.size === 0 : ui.chips.has(b.dataset.chip))));
    return renderAll();
  }
  if (t.dataset.agency !== undefined && t.closest("#agency-tabs")) {
    resUi.agency = t.dataset.agency;
    document.querySelectorAll("#agency-tabs [role=tab]").forEach((b) => b.setAttribute("aria-selected", String(b === t)));
    return renderResources();
  }
});
$("run-demo").addEventListener("click", runDemo);
$("new-incident").addEventListener("click", () => { $("incident-error").textContent = ""; $("incident-dialog").showModal(); });
$("open-resources").addEventListener("click", () => { $("resources-dialog").showModal(); renderResources(); });
$("about-open").addEventListener("click", () => $("about-dialog").showModal());
$("mode").addEventListener("click", () => { $("api-url").value = API.base; $("connect").showModal(); });
$("feed-agent").addEventListener("change", renderFeed);
$("feed-follow").addEventListener("change", renderFeed);
$("feed-pause").addEventListener("click", (e) => {
  ui.feedPaused = !ui.feedPaused; ui.feedPending = 0;
  e.currentTarget.setAttribute("aria-pressed", String(ui.feedPaused));
  e.currentTarget.textContent = ui.feedPaused ? "Resume" : "Pause";
  $("live-dot").classList.toggle("on", !ui.feedPaused);
  renderFeed();
});
$("feed-new").addEventListener("click", () => $("feed-pause").click());
$("incident-form").addEventListener("submit", async (ev) => {
  ev.preventDefault();
  const f = new FormData(ev.currentTarget);
  const body = Object.fromEntries(f.entries());
  for (const k of ["severity", "lat", "lon", "casualties", "trapped"]) body[k] = Number(body[k]);
  body.hazards = String(body.hazards || "").split(",").map((s) => s.trim()).filter(Boolean);
  try { await API.call("createIncident", body); $("incident-dialog").close(); ev.currentTarget.reset(); poll(); }
  catch (err) { $("incident-error").textContent = `Could not create the incident: ${err.message}`; }
});
$("connect-form").addEventListener("submit", (ev) => {
  ev.preventDefault();
  const url = $("api-url").value.trim();
  try { localStorage.setItem("suru-api", url); } catch { /* storage blocked */ }
  location.search = url ? `?api=${encodeURIComponent(url)}` : "";
});

function renderAbout() {
  const live = API.mode === "live";
  $("about-body").innerHTML = `
    <p>${live ? `Connected to the urban_response API at <code>${esc(API.base)}</code>.` : "No backend address is set, so this page runs a <b>built-in mock</b>: scripted agents, invented unit counts and approximate station locations. Every decision is labelled <span class='exec mock'>mock</span>."}</p>
    <p>Agency colours: ${Object.values(AGENCY).map((a) => `<span class="chip-ag" style="--c:${a.color}">${a.label}</span>`).join(" ")}. Status colours are separate: green dispatched, amber partial, red failed.</p>
    <p><b>Response success</b> is the share of incidents whose latest response met every essential request. <b>Fill rate</b> is allocated ÷ requested units. <b>ETA met</b> counts allocations inside the requesting agent's maximum ETA. <b>Conflicts auto-resolved</b> counts hard conflicts where CR's choice passed re-validation.</p>
    <p>Basemap: OpenFreeMap, © OpenMapTiles, data © OpenStreetMap contributors.</p>
    <p>Simulated decisions for a hackathon demo. Not operational advice.</p>`;
  $("foot-note").textContent = live ? `API ${API.base}` : "Mock data (in-browser)";
}

async function start() {
  const fromQuery = new URLSearchParams(location.search).get("api");
  let stored = null;
  try { stored = localStorage.getItem("suru-api"); } catch { /* storage blocked */ }
  API.use(fromQuery ?? stored ?? window.SURU_API_BASE ?? "");
  renderAbout();
  initMap();
  try { store.stations = (await API.call("stations")) || []; } catch { store.stations = []; }
  renderStations();
  await pollHealth();
  await poll();
  setInterval(poll, 2000);
  setInterval(pollHealth, 10000);
  setInterval(() => renderList(), 30000);  // keeps "n min" ages fresh between polls
}
start();
