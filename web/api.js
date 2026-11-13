"use strict";
// The only file that knows the urban_response API. Integration = adjust the paths and the norm* field maps here.
//
// Assumed routes (from the Track 3 UI spec; confirm against the FastAPI OpenAPI page):
//   GET  /health                        -> { mode: "mock"|"live", model, fallback }
//   GET  /incidents                     -> [incident]        POST /incidents {category, severity, lat, lon, address, description, casualties, trapped, hazards}
//   GET  /incidents/{id}                -> incident          POST /incidents/{id}/close      POST /incidents/{id}/retry
//   GET  /incidents/{id}/responses      -> [response]        GET  /responses -> [response] (all, for metrics)
//   GET  /events?since_id=N             -> [event]
//   GET  /resources                     -> [per-type availability]   GET /resources/{type}/forecast
//   GET  /units?resource_type=&agency=  -> [unit]            POST /units/{id}/release
//   GET  /stations   GET /hospitals     GET /metrics (optional)      GET /scenario (optional; else scenarios/sheffield_demo.json)
//
// Normalised shapes used by app.js are documented on each norm* function. Unknown fields fall back to safe defaults.
window.SURU_API = (function () {
  let base = "";
  let mock = null;

  async function http(path, method = "GET", body) {
    const res = await fetch(base + path, { method, headers: body ? { "Content-Type": "application/json" } : undefined, body: body ? JSON.stringify(body) : undefined });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.detail || data.error || `HTTP ${res.status}`);
    return data;
  }
  const pick = (...vals) => vals.find((v) => v !== undefined && v !== null);

  // incident: {id, category, severity 1..5, status, lat, lon, address, district, description, casualties, trapped, hazards[], weapons, received_at, lead_agency, routed_agencies[]}
  function normIncident(r) {
    const loc = r.location || {};
    return { id: pick(r.id, r.incident_id), category: pick(r.category, r.type, "other"), severity: Number(pick(r.severity, 1)),
      status: pick(r.status, "pending"), lat: Number(pick(r.lat, loc.lat)), lon: Number(pick(r.lon, r.lng, loc.lon, loc.lng)),
      address: pick(r.address, loc.address, ""), district: pick(r.district, loc.district, ""), description: pick(r.description, ""),
      casualties: pick(r.casualties, 0), trapped: pick(r.trapped, 0), hazards: pick(r.hazards, []), weapons: pick(r.weapons, null),
      received_at: pick(r.received_at, r.created_at), lead_agency: pick(r.lead_agency, r.routing && r.routing.lead, null),
      routed_agencies: pick(r.routed_agencies, r.routing && r.routing.agencies, []) };
  }
  // response: {id, incident_id, created_at, status, routing{lead, agencies[], rationale, executed_by}, agent_responses[], allocations[], unmet[], hard_conflicts[], soft_conflicts[], directives[], cr_rationale, executed_by}
  function normResponse(r) {
    return { id: pick(r.id, r.response_id), incident_id: r.incident_id, created_at: pick(r.created_at, r.ts), status: pick(r.status, "pending"),
      routing: r.routing || {}, agent_responses: pick(r.agent_responses, r.agents, []), allocations: pick(r.allocations, []), unmet: pick(r.unmet, r.unmet_needs, []),
      hard_conflicts: pick(r.hard_conflicts, r.hard_resolutions, []), soft_conflicts: pick(r.soft_conflicts, []), directives: pick(r.directives, r.unified_directives, []),
      cr_rationale: pick(r.cr_rationale, ""), executed_by: pick(r.executed_by, "") };
  }
  // event: {id, ts, incident_id, agent_id, type, summary, payload, executed_by}
  const normEvent = (e) => ({ id: e.id, ts: pick(e.ts, e.created_at), incident_id: e.incident_id, agent_id: pick(e.agent_id, e.agent, ""), type: pick(e.type, e.event_type, ""),
    summary: pick(e.summary, ""), payload: pick(e.payload, e.payload_json, {}), executed_by: pick(e.executed_by, "") });
  const normUnit = (u) => ({ id: pick(u.id, u.unit_id), callsign: pick(u.callsign, u.id), resource_type: pick(u.resource_type, u.type), agency: u.agency,
    station_id: pick(u.station_id, u.station), lat: u.lat, lon: pick(u.lon, u.lng), status: u.status, incident_id: pick(u.incident_id, null) });
  const normStation = (s) => ({ id: pick(s.id, s.station_id, s.name), name: s.name, agency: pick(s.agency, "shared"), lat: s.lat, lon: pick(s.lon, s.lng) });

  const real = {
    mode: "live",
    health: () => http("/health"),
    incidents: () => http("/incidents").then((l) => l.map(normIncident)),
    incident: (id) => http(`/incidents/${id}`).then(normIncident),
    responses: (id) => http(id ? `/incidents/${id}/responses` : "/responses").then((l) => l.map(normResponse)),
    events: (sinceId = 0) => http(`/events?since_id=${sinceId}`).then((l) => l.map(normEvent)),
    stations: () => http("/stations").then((l) => l.map(normStation)),
    hospitals: () => http("/hospitals"),
    units: (q = {}) => http("/units?" + new URLSearchParams(q)).then((l) => l.map(normUnit)),
    resources: () => http("/resources"),
    forecast: (type) => http(`/resources/${type}/forecast`),
    createIncident: (body) => http("/incidents", "POST", body).then(normIncident),
    closeIncident: (id) => http(`/incidents/${id}/close`, "POST"),
    retryIncident: (id) => http(`/incidents/${id}/retry`, "POST"),
    releaseUnit: (id) => http(`/units/${id}/release`, "POST"),
    scenario: () => http("/scenario").catch(() => fetch("scenarios/sheffield_demo.json").then((r) => r.json())),
    metrics: () => http("/metrics").catch(() => null),
  };

  return {
    use(url) {
      base = (url || "").replace(/\/+$/, "");
      mock = base ? null : window.SURU_MOCK;
      return this;
    },
    get mode() { return mock ? "mock" : "live"; },
    get base() { return base; },
    call(name, ...args) {
      const impl = mock || real;
      if (!impl[name]) return Promise.resolve(null);
      return impl[name](...args);
    },
  };
})();
