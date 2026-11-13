"use strict";
// In-browser stand-in for the urban_response API, used until the FastAPI backend is pushed (routes in api.js).
// Everything here is MOCK demo data: station coordinates are approximate and unit counts are invented.
// It plays the spec's demo script: RTC, firearms contention, high-rise fire with a pump shortfall and a
// pre-emption, hazmat, medical. Agents are scripted rules, so executed_by is always "mock".
window.SURU_MOCK = (function () {
  const sleepers = [];
  const after = (ms, fn) => sleepers.push(setTimeout(fn, ms));
  const iso = () => new Date().toISOString();
  const rand = (lo, hi) => lo + Math.random() * (hi - lo);
  const distM = (a, b) => {
    const toRad = (d) => (d * Math.PI) / 180;
    const dLat = toRad(b.lat - a.lat), dLon = toRad(b.lon - a.lon);
    const h = Math.sin(dLat / 2) ** 2 + Math.cos(toRad(a.lat)) * Math.cos(toRad(b.lat)) * Math.sin(dLon / 2) ** 2;
    return 2 * 6371000 * Math.asin(Math.sqrt(h));
  };

  const STATIONS = [
    { id: "ST-SNIG", name: "Snig Hill", agency: "police", lat: 53.3846, lon: -1.4678 },
    { id: "ST-SHEP", name: "Shepcote Lane", agency: "police", lat: 53.4021, lon: -1.4125 },
    { id: "ST-ATT", name: "Attercliffe", agency: "police", lat: 53.3858, lon: -1.4405 },
    { id: "ST-ECC", name: "Ecclesfield", agency: "police", lat: 53.4395, lon: -1.4620 },
    { id: "ST-CEN", name: "Central (Eyre St)", agency: "fire", lat: 53.3772, lon: -1.4746 },
    { id: "ST-RIV", name: "Rivelin", agency: "fire", lat: 53.4015, lon: -1.5220 },
    { id: "ST-DAR", name: "Darnall", agency: "fire", lat: 53.3862, lon: -1.4180 },
    { id: "ST-PKW", name: "Parkway", agency: "fire", lat: 53.3885, lon: -1.3975 },
    { id: "ST-LOW", name: "Lowedges", agency: "fire", lat: 53.3335, lon: -1.4815 },
    { id: "ST-BIR", name: "Birley Moor", agency: "fire", lat: 53.3440, lon: -1.4040 },
    { id: "ST-LON", name: "Longley", agency: "ambulance", lat: 53.4105, lon: -1.4720 },
    { id: "ST-HAL", name: "Hallamshire", agency: "ambulance", lat: 53.3785, lon: -1.4942 },
    { id: "ST-OLI", name: "Olive Grove", agency: "ambulance", lat: 53.3630, lon: -1.4650 },
    { id: "ST-HIL", name: "Hillsborough", agency: "ambulance", lat: 53.4020, lon: -1.5010 },
    { id: "ST-JSH", name: "Joint Services Hub", agency: "shared", lat: 53.3990, lon: -1.4150 },
  ];
  const HOSPITALS = [
    { name: "Northern General Hospital", ed_capacity: 42, major_trauma: true, paediatric: false },
    { name: "Royal Hallamshire Hospital", ed_capacity: 30, major_trauma: false, paediatric: false },
    { name: "Sheffield Children's Hospital", ed_capacity: 18, major_trauma: false, paediatric: true },
  ];
  const TYPES = [
    { code: "IRV", name: "Response car", agency: "police" }, { code: "ARV", name: "Armed response vehicle", agency: "police" },
    { code: "RPU", name: "Roads policing unit", agency: "police" }, { code: "DOG", name: "Dog unit", agency: "police" },
    { code: "PMP", name: "Fire engine", agency: "fire" }, { code: "ALP", name: "Aerial ladder platform", agency: "fire" },
    { code: "HMU", name: "Hazmat unit", agency: "fire" }, { code: "RSU", name: "Rescue support unit", agency: "fire" },
    { code: "DCA", name: "Ambulance", agency: "ambulance" }, { code: "RRV", name: "Rapid response vehicle", agency: "ambulance" },
    { code: "HART", name: "Hazardous area response team", agency: "ambulance" },
    { code: "DRONE", name: "Drone unit", agency: "shared" }, { code: "CMD", name: "Command unit", agency: "shared" },
  ];
  const PLAN = {
    IRV: { "ST-SNIG": 5, "ST-SHEP": 4, "ST-ATT": 4, "ST-ECC": 3 }, ARV: { "ST-SHEP": 3 }, RPU: { "ST-SHEP": 2, "ST-ATT": 2 }, DOG: { "ST-SHEP": 2 },
    PMP: { "ST-CEN": 1, "ST-RIV": 1, "ST-DAR": 1, "ST-PKW": 1, "ST-LOW": 1, "ST-BIR": 1 }, ALP: { "ST-CEN": 1 }, HMU: { "ST-DAR": 1 }, RSU: { "ST-CEN": 1 },
    DCA: { "ST-LON": 4, "ST-HAL": 3, "ST-OLI": 3, "ST-HIL": 2 }, RRV: { "ST-LON": 1, "ST-HAL": 1, "ST-OLI": 1 }, HART: { "ST-LON": 1 },
    DRONE: { "ST-JSH": 1 }, CMD: { "ST-JSH": 1 },
  };
  const typeOf = Object.fromEntries(TYPES.map((t) => [t.code, t]));
  const stationOf = Object.fromEntries(STATIONS.map((s) => [s.id, s]));
  const units = [];
  for (const [code, perStation] of Object.entries(PLAN)) {
    let n = 0;
    for (const [station, count] of Object.entries(perStation)) for (let i = 0; i < count; i++) {
      n += 1;
      const id = `${code}-${String(n).padStart(2, "0")}`;
      units.push({ id, callsign: id, resource_type: code, agency: typeOf[code].agency, station_id: station,
        lat: stationOf[station].lat, lon: stationOf[station].lon, status: "available", incident_id: null, eta_min: null, release_at: null });
    }
  }
  units.find((u) => u.id === "ARV-03").status = "out_of_service";
  units.find((u) => u.id === "DCA-12").status = "out_of_service";

  const SCENARIO = [
    { category: "rtc", severity: 3, district: "Darnall", address: "Staniforth Road / Main Road", lat: 53.3859, lon: -1.4212,
      description: "Two-vehicle collision; one person trapped; fuel on the carriageway.", casualties: 3, trapped: 1, hazards: ["fuel spill"] },
    { category: "firearms", severity: 4, district: "Burngreave", address: "Spital Hill", lat: 53.3912, lon: -1.4578,
      description: "Man seen with a handgun outside a shop; no shots heard.", casualties: 0, trapped: 0, hazards: [], weapons: "handgun" },
    { category: "high_rise_fire", severity: 5, district: "Broomhall", address: "Hanover Way tower block, 9th floor", lat: 53.3735, lon: -1.4835,
      description: "Fire on the 9th floor, persons reported, smoke in the stairwell.", casualties: 2, trapped: 4, hazards: ["smoke", "falling glass"] },
    { category: "hazmat", severity: 4, district: "Tinsley", address: "Sheffield Road industrial unit", lat: 53.4075, lon: -1.4035,
      description: "Chemical leak from a storage tank; strong odour; staff evacuated.", casualties: 1, trapped: 0, hazards: ["chlorine"] },
    { category: "medical", severity: 2, district: "Hillsborough", address: "Middlewood Road", lat: 53.4010, lon: -1.5000,
      description: "Elderly man collapsed in the street; conscious and breathing.", casualties: 1, trapped: 0, hazards: [] },
  ];

  // Scripted "agents": routing, per-agency requests and scene constraints by category.
  const ROUTING = {
    rtc: ["ambulance", ["fire", "police"], "Person trapped and fuel on the road: ambulance lead, fire for extrication, police for closure."],
    firearms: ["police", ["ambulance"], "Firearm reported: police lead with armed response; ambulance holds at the RVP."],
    high_rise_fire: ["fire", ["ambulance", "police"], "High-rise with persons reported: fire lead; ambulance for casualties; police for the evacuation cordon."],
    structure_fire: ["fire", ["ambulance"], "Building fire: fire lead; ambulance standby for casualties."],
    hazmat: ["fire", ["ambulance", "police"], "Chemical release: fire lead with hazmat; HART for contaminated casualties; police for the exclusion zone."],
    flood: ["fire", ["police"], "Flooding with people at risk: fire lead for water rescue; police for road closures."],
    medical: ["ambulance", [], "Single casualty, conscious: ambulance only."],
    assault: ["police", ["ambulance"], "Assault with injuries: police lead; ambulance for the casualty."],
  };
  const REQUESTS = {
    rtc: { fire: [["PMP", 2, 10, true], ["RSU", 1, 15, false]], ambulance: [["DCA", 2, 8, true], ["RRV", 1, 8, false]], police: [["RPU", 2, 15, true], ["IRV", 1, 15, false]] },
    firearms: { police: [["ARV", 3, 10, true], ["IRV", 4, 15, false], ["DOG", 1, 20, false]], ambulance: [["DCA", 1, 15, false]] },
    high_rise_fire: { fire: [["PMP", 6, 10, true], ["ALP", 1, 12, true], ["CMD", 1, 20, false]], ambulance: [["DCA", 3, 10, true], ["HART", 1, 20, false]], police: [["IRV", 4, 15, true]] },
    structure_fire: { fire: [["PMP", 3, 10, true]], ambulance: [["DCA", 1, 12, false]] },
    hazmat: { fire: [["PMP", 2, 12, true], ["HMU", 1, 15, true]], ambulance: [["HART", 1, 20, true], ["DCA", 1, 15, false]], police: [["IRV", 2, 15, true], ["DRONE", 1, 20, false]] },
    flood: { fire: [["PMP", 2, 15, true], ["RSU", 1, 20, true]], police: [["IRV", 2, 15, false]] },
    medical: { ambulance: [["DCA", 1, 8, true], ["RRV", 1, 8, false]] },
    assault: { police: [["IRV", 2, 10, true]], ambulance: [["DCA", 1, 10, true]] },
  };
  const CORDONS = { fire: [50, 150, 200], police: [100, 300, 0], ambulance: [0, 0, 0], shared: [0, 0, 0] };
  const AGENT_ID = { police: "A1", ambulance: "A2", fire: "A3" };
  const KB = { police: "SYP firearms command policy §4", fire: "Pre-determined attendance §2", ambulance: "JESIP joint doctrine, scene safety" };
  const DURATION_MIN = { rtc: 60, firearms: 90, high_rise_fire: 150, structure_fire: 120, hazmat: 150, flood: 120, medical: 45, assault: 60 };

  const incidents = [], responses = [], events = [];
  let seq = 0, eventSeq = 0, responseSeq = 0;
  const hex = () => Math.floor(Math.random() * 0xffff).toString(16).toUpperCase().padStart(4, "0");
  const incidentOf = (id) => incidents.find((i) => i.id === id);
  const latestResponse = (id) => [...responses].reverse().find((r) => r.incident_id === id) || null;
  const emit = (incident_id, agent_id, type, summary, payload = {}, executed_by = "mock") => {
    const e = { id: ++eventSeq, ts: iso(), incident_id, agent_id, type, summary, payload, executed_by };
    events.push(e);
    return e;
  };
  const etaFor = (u, inc) => Math.max(2, Math.round(distM(u, inc) / 1000 / 40 * 60 + 1.5));
  const freeCount = (code) => units.filter((u) => u.resource_type === code && u.status === "available").length;
  const heldBy = (incId, code) => units.filter((u) => u.incident_id === incId && u.resource_type === code);

  function proposalFor(inc, agency, isLead) {
    const reqs = (REQUESTS[inc.category] || {})[agency] || [];
    const [inner, outer, evac] = CORDONS[agency];
    const scale = inc.severity >= 5 ? 1.3 : 1;
    // Ambulance stages close in on a high-rise fire on purpose, which trips the staging soft conflict.
    const stagingM = agency === "ambulance" && inc.category === "high_rise_fire" ? 40 : agency === "ambulance" ? 120 : inner ? Math.round(inner * scale) + 70 : 150;
    const staging = { lat: inc.lat + stagingM / 111320, lon: inc.lon + 0.0002, name: agency === "ambulance" ? "Hanover Way (40 m)" : `RVP ${stagingM} m north` };
    const names = reqs.map(([c, q]) => `${q}× ${typeOf[c].name}`).join(", ");
    return {
      agent_id: AGENT_ID[agency], agency, executed_by: "mock", latency_ms: Math.round(rand(650, 1900)), confidence: +rand(0.72, 0.95).toFixed(2),
      summary: `${isLead ? "Lead agency. " : ""}Requesting ${names || "no vehicles"} for a severity ${inc.severity} ${inc.category.replace(/_/g, " ")}.`,
      tactical_plan: [`Approach from ${agency === "fire" ? "the upwind side" : "the nearest arterial road"} and establish an RVP.`,
        isLead ? "Take incident command and brief arriving crews." : "Report to the lead agency's incident commander on arrival.",
        inc.trapped ? `Prioritise the ${inc.trapped} trapped person${inc.trapped > 1 ? "s" : ""}.` : "Confirm casualty numbers on arrival."],
      hazards: inc.hazards.length ? inc.hazards : ["None reported"],
      safety_notes: [inner ? `Inner cordon ${Math.round(inner * scale)} m; outer cordon ${Math.round(outer * scale)} m.` : "Hold at the RVP until the scene is declared safe.", inc.weapons ? `Weapon reported: ${inc.weapons}.` : "PPE as per agency policy."],
      kb_citations: [KB[agency]],
      requests: reqs.map(([code, qty, max_eta_min, essential]) => ({ resource_type: code, qty, max_eta_min, essential })),
      scene_constraints: { inner_cordon_m: Math.round(inner * scale), outer_cordon_m: Math.round(outer * scale), evacuation_radius_m: Math.round(evac * scale), staging },
    };
  }

  function commit(u, inc, eta, req, agency, preempted_from = null) {
    u.status = "committed"; u.incident_id = inc.id; u.eta_min = eta;
    u.release_at = Date.now() + (DURATION_MIN[inc.category] || 60) * 60000;
    return { unit_id: u.id, callsign: u.callsign, resource_type: u.resource_type, type_name: typeOf[u.resource_type].name, agency: u.agency,
      station: stationOf[u.station_id].name, station_id: u.station_id, eta_min: eta, max_eta_min: req.max_eta_min, requested_by: AGENT_ID[agency] || agency, preempted_from };
  }

  function allocate(inc, proposals) {
    const allocations = [], unmet = [], hard = [];
    for (const p of proposals) for (const req of p.requests) {
      const avail = units.filter((u) => u.resource_type === req.resource_type && u.status === "available")
        .map((u) => ({ u, eta: etaFor(u, inc) })).sort((a, b) => a.eta - b.eta);
      const take = avail.slice(0, req.qty);
      take.forEach(({ u, eta }) => allocations.push(commit(u, inc, eta, req, p.agency)));
      let shortfall = req.qty - take.length;
      if (shortfall <= 0) continue;
      const tname = typeOf[req.resource_type].name;
      emit(inc.id, "rules", "HARD_CONFLICT", `Shortfall: ${tname} (${req.resource_type}), ${req.qty} requested, ${take.length} free`,
        { resource_type: req.resource_type, requested: req.qty, available: take.length }, "rules");
      // CR's first choice takes everything it needs from lower-severity incidents.
      const candidates = units.filter((u) => u.resource_type === req.resource_type && u.status === "committed" && incidentOf(u.incident_id).severity < inc.severity)
        .sort((a, b) => incidentOf(a.incident_id).severity - incidentOf(b.incident_id).severity);
      const wanted = candidates.slice(0, shortfall);
      // Validation: a pre-emption may never strip a live incident of its last unit of a type.
      const strips = (set) => set.some((u) => heldBy(u.incident_id, req.resource_type).length - set.filter((x) => x.incident_id === u.incident_id).length < 1);
      let chosen = wanted, overridden = false;
      if (wanted.length && strips(wanted)) {
        overridden = true;
        chosen = [];
        for (const u of candidates) if (chosen.length < shortfall && !strips([...chosen, u])) chosen.push(u);
      }
      const resolution = chosen.length
        ? `Pre-empt ${chosen.map((u) => `${u.callsign} from ${u.incident_id} (sev ${incidentOf(u.incident_id).severity})`).join(", ")}${chosen.length < shortfall ? `; mutual aid for ${shortfall - chosen.length} more` : ""}`
        : `No lower-severity incident holds a ${tname}; request ${shortfall} via regional mutual aid`;
      const rationale = `${inc.id} is severity ${inc.severity}; ${chosen.length ? "lower-severity incidents can spare units without dropping below one per type" : "nothing can be released without leaving another incident uncovered"}.`;
      emit(inc.id, "CR", "RESOLUTION", resolution, { wanted: wanted.map((u) => u.callsign), chosen: chosen.map((u) => u.callsign), rationale });
      if (overridden) emit(inc.id, "rules", "OVERRIDE", `CR choice invalid (would strip ${wanted[0].incident_id} of its last ${tname}) → policy fallback accept_partial`, { fallback: "accept_partial" }, "fallback");
      for (const u of chosen) {
        const from = u.incident_id, fromInc = incidentOf(from), fromResp = latestResponse(from);
        if (fromResp) {
          fromResp.allocations = fromResp.allocations.filter((a) => a.unit_id !== u.id);
          fromResp.unmet.push({ resource_type: u.resource_type, type_name: tname, qty: 1, essential: true, mutual_aid: `Pre-empted by ${inc.id}; replacement requested via mutual aid` });
          fromResp.status = "partial"; fromInc.status = "partial";
        }
        allocations.push(commit(u, inc, etaFor(u, inc) + 3, req, p.agency, from));
      }
      shortfall -= chosen.length;
      hard.push({ kind: "shortfall", resource_type: req.resource_type, type_name: tname, requested: req.qty, available: take.length,
        resolution, rationale, overridden, fallback: overridden ? "accept_partial" : null, preempted: chosen.map((u) => u.callsign) });
      if (shortfall > 0) unmet.push({ resource_type: req.resource_type, type_name: tname, qty: shortfall, essential: req.essential, mutual_aid: `${shortfall}× ${tname} requested via regional mutual aid` });
    }
    return { allocations, unmet, hard };
  }

  function softConflicts(inc, proposals, lead) {
    const out = [];
    const leadP = proposals.find((p) => p.agency === lead);
    for (const p of proposals) {
      if (p === leadP || !leadP || !leadP.scene_constraints.inner_cordon_m) continue;
      if (distM(p.scene_constraints.staging, inc) < leadP.scene_constraints.inner_cordon_m)
        out.push({ code: "STAGING_INSIDE_INNER_CORDON", title: `${cap(p.agency)} staging inside the ${lead} inner cordon`, severity: "high", agencies: [p.agency, lead],
          resolution: "Move the RVP to Hanover Street (120 m, upwind)", source: "rules" });
    }
    if (inc.category === "hazmat") out.push({ code: "APPROACH_FROM_DOWNWIND", title: "Police approach route is downwind of the leak", severity: "medium", agencies: ["police", "fire"],
      resolution: "Approach from the west via Bawtry Road; hold the exclusion zone at 300 m", source: "semantic" });
    return out;
  }
  const cap = (s) => s[0].toUpperCase() + s.slice(1);

  function directives(inc, lead, alloc, soft) {
    const d = [`${cap(lead)} holds incident command at ${inc.address}.`];
    for (const s of soft) d.push(s.resolution + ".");
    if (alloc.unmet.length) d.push(`Work with ${alloc.allocations.length} units until mutual aid arrives; ${alloc.unmet.map((u) => `${u.qty}× ${u.type_name}`).join(", ")} outstanding.`);
    if (inc.trapped) d.push(`Extrication of ${inc.trapped} trapped person${inc.trapped > 1 ? "s" : ""} is the first objective.`);
    if (inc.casualties) d.push(`Casualties go to ${inc.category === "hazmat" ? "Northern General after decontamination" : "Northern General (major trauma)"}.`);
    return d;
  }

  function runPipeline(inc) {
    inc.status = "routing";
    const [lead, others, why] = ROUTING[inc.category] || ROUTING.assault;
    const agencies = [lead, ...others];
    const proposals = [];
    after(600, () => {
      inc.lead_agency = lead; inc.routed_agencies = agencies;
      emit(inc.id, "Qwen", "ROUTED", `${inc.id} → ${cap(lead)} (lead)${others.length ? ", " + others.map(cap).join(", ") : ""}: "${why}"`, { lead, agencies, rationale: why });
    });
    agencies.forEach((agency, i) => after(1300 + i * 350, () => {
      const p = proposalFor(inc, agency, agency === lead);
      proposals.push(p);
      const sc = p.scene_constraints;
      emit(inc.id, p.agent_id, "PROPOSAL", `${p.agent_id} ${cap(agency)} requests ${p.requests.map((r) => `${r.qty}× ${typeOf[r.resource_type].name}`).join(", ") || "nothing"}${sc.inner_cordon_m ? `; inner cordon ${sc.inner_cordon_m} m` : ""} (KB: ${p.kb_citations[0]})`, p);
    }));
    after(2500, () => {
      const alloc = allocate(inc, proposals);
      const soft = softConflicts(inc, proposals, lead);
      for (const s of soft) emit(inc.id, s.source === "rules" ? "rules" : "CR", "SOFT_CONFLICT", `${s.title} → ${s.resolution}`, s, s.source === "rules" ? "rules" : "mock");
      const essentialUnmet = alloc.unmet.some((u) => u.essential);
      const status = alloc.allocations.length ? (essentialUnmet ? "partial" : "dispatched") : "failed";
      const response = { id: `RSP-${hex()}`, seq: ++responseSeq, incident_id: inc.id, created_at: iso(), status,
        routing: { lead, agencies, rationale: why, executed_by: "mock" }, agent_responses: proposals,
        allocations: alloc.allocations, unmet: alloc.unmet, hard_conflicts: alloc.hard, soft_conflicts: soft,
        directives: directives(inc, lead, alloc, soft), cr_rationale: alloc.hard.length ? alloc.hard.map((h) => h.rationale).join(" ") : "No contention; every essential request was met from free units.", executed_by: "mock" };
      responses.push(response);
      inc.status = status; inc.latest_response_id = response.id;
      emit(inc.id, "CR", "DISPATCHED", `${alloc.allocations.length} units allocated, status ${status}${alloc.unmet.length ? `, ${alloc.unmet.reduce((n, u) => n + u.qty, 0)} unmet` : ""}`, { status, unmet: alloc.unmet });
    });
  }

  // Freed units go to the highest-severity partial incident that still needs that type.
  function reallocateFreed() {
    const partial = incidents.filter((i) => i.status === "partial").sort((a, b) => b.severity - a.severity);
    for (const inc of partial) {
      const r = latestResponse(inc.id);
      if (!r) continue;
      for (const need of [...r.unmet]) {
        const free = units.filter((u) => u.resource_type === need.resource_type && u.status === "available")
          .map((u) => ({ u, eta: etaFor(u, inc) })).sort((a, b) => a.eta - b.eta).slice(0, need.qty);
        if (!free.length) continue;
        free.forEach(({ u, eta }) => r.allocations.push(commit(u, inc, eta, { max_eta_min: 15 }, inc.lead_agency)));
        need.qty -= free.length;
        if (need.qty <= 0) r.unmet.splice(r.unmet.indexOf(need), 1);
        const done = !r.unmet.some((u) => u.essential);
        r.status = done ? "dispatched" : "partial"; inc.status = r.status;
        emit(inc.id, "CR", "DISPATCHED", `Backfilled ${free.map((f) => f.u.callsign).join(", ")} from released units; status ${r.status}`, { status: r.status });
      }
    }
  }

  const clone = (x) => JSON.parse(JSON.stringify(x));
  const ok = (x) => Promise.resolve(clone(x));
  const api = {
    mode: "mock",
    health: () => ok({ mode: "mock", model: null, fallback: events.slice(-40).some((e) => e.executed_by === "fallback") }),
    incidents: () => ok(incidents),
    incident: (id) => ok(incidentOf(id)),
    responses: (id) => ok(id ? responses.filter((r) => r.incident_id === id) : responses),
    events: (sinceId = 0) => ok(events.filter((e) => e.id > sinceId)),
    stations: () => ok(STATIONS),
    hospitals: () => ok(HOSPITALS),
    units: ({ resource_type, agency } = {}) => ok(units.filter((u) => (!resource_type || u.resource_type === resource_type) && (!agency || u.agency === agency))),
    resources: () => ok(TYPES.map((t) => {
      const mine = units.filter((u) => u.resource_type === t.code);
      return { ...t, total: mine.length, available: mine.filter((u) => u.status === "available").length,
        committed: mine.filter((u) => u.status === "committed").length, out_of_service: mine.filter((u) => u.status === "out_of_service").length };
    })),
    forecast: (code) => {
      const mine = units.filter((u) => u.resource_type === code);
      const points = [];
      for (let m = 0; m <= 120; m += 15) {
        const at = Date.now() + m * 60000;
        points.push({ minutes: m, available: mine.filter((u) => u.status === "available" || (u.status === "committed" && u.release_at <= at)).length });
      }
      return ok({ resource_type: code, total: mine.length, points });
    },
    createIncident: (body) => {
      const inc = { id: `INC-${hex()}`, status: "pending", received_at: iso(), lead_agency: null, routed_agencies: [], latest_response_id: null,
        casualties: 0, trapped: 0, hazards: [], weapons: null, district: "", address: "", ...body, severity: Number(body.severity) };
      incidents.push(inc);
      runPipeline(inc);
      return ok(inc);
    },
    closeIncident: (id) => {
      const inc = incidentOf(id);
      if (!inc) return Promise.reject(new Error("Unknown incident"));
      inc.status = "closed";
      const released = units.filter((u) => u.incident_id === id);
      released.forEach((u) => { u.status = "available"; u.incident_id = null; u.eta_min = null; u.release_at = null; });
      emit(id, "rules", "CLOSED", `${id} closed; ${released.length} units released`, { released: released.map((u) => u.callsign) }, "rules");
      reallocateFreed();
      return ok(inc);
    },
    retryIncident: (id) => { const inc = incidentOf(id); runPipeline(inc); return ok(inc); },
    releaseUnit: (unitId) => {
      const u = units.find((x) => x.id === unitId);
      if (!u) return Promise.reject(new Error("Unknown unit"));
      const from = u.incident_id;
      u.status = "available"; u.incident_id = null; u.eta_min = null; u.release_at = null;
      if (from) { const r = latestResponse(from); if (r) r.allocations = r.allocations.filter((a) => a.unit_id !== unitId); }
      emit(from, "rules", "RELEASED", `${u.callsign} released manually`, {}, "rules");
      reallocateFreed();
      return ok(u);
    },
    scenario: () => ok(SCENARIO),
    reset: () => { sleepers.forEach(clearTimeout); sleepers.length = 0; incidents.length = 0; responses.length = 0; events.length = 0;
      units.forEach((u) => { if (u.status === "committed") { u.status = "available"; u.incident_id = null; u.eta_min = null; } }); return ok({ ok: true }); },
  };
  return api;
})();
