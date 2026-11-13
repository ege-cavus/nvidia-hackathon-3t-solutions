"""Qwen orchestrator: owns the incident from ingestion to completion. No human in the loop.

  0. ingest      the canonical incident (examples/incident.schema.json) gets a numeric id;
                 Qwen triages it (category, severity, coordinates) into a separate Triage;
                 MiniLM clustering (incident_cluster_handoff.py) proposes duplicates and candidates;
                 Qwen makes the final call (probably different incidents are dispatched separately).
                 Duplicates are merged into the existing incident and not dispatched.
  1. route       Qwen picks agencies and the lead agency
  2. fan out     Nemotron A1/A2/A3 respond in parallel
  3. dispatch    Nemotron CR declares the manifest, resolves conflicts, dispatches crewed units
  4. evaluate    Qwen reads code-computed success metrics and gives the verdict
  5. finalise    incident -> backlog; units return automatically at their release time

Every step writes a plain-language line to the incident log (see briefing.py).
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Awaitable, Callable, Literal

from pydantic import BaseModel, Field

from .. import briefing, dedup, triage as triage_rules
from ..config import Settings, get_settings
from ..db import Database
from ..evaluation import compute_metrics, grade, rule_evaluation
from ..llm import LLMError, chat_json
from ..schemas import (
    CATEGORIES,
    Agency,
    Case,
    DedupDecision,
    DedupVerdict,
    Evaluation,
    ExecutedBy,
    Incident,
    IncidentResponse,
    IncidentStatus,
    PipelineResult,
    RoutingDecision,
    Triage,
)
from .conflict_resolver import ConflictResolverAgent
from .heuristics import route as rule_route
from .specialist import SpecialistAgent

log = logging.getLogger(__name__)

EventFn = Callable[[str, dict], Awaitable[None]] | None

SYSTEM = (
    "You are the Sheffield multi-agency emergency response orchestrator. You take incidents in, route them to the "
    "specialist agents (A1 police, A2 ambulance, A3 fire & rescue), and after dispatch you judge whether the "
    "response met the need. You write for dispatchers: short, factual, no speculation."
)


async def _emit(on_event: EventFn, name: str, payload: dict) -> None:
    if on_event:
        try:
            await on_event(name, payload)
        except Exception:  # a broken listener must never break dispatch
            log.exception("event listener failed on %s", name)


class RoutingDraft(BaseModel):
    agencies: list[Agency] = Field(min_length=1)
    lead_agency: Agency
    rationale: str


class DedupDraft(BaseModel):
    verdict: Literal["duplicate", "related", "new"]
    match_incident_id: int | None = Field(default=None, description="Required for duplicate and related; from CANDIDATES")
    reason: str = Field(description="One or two plain sentences")


class TriageDraft(BaseModel):
    category: str = Field(description="One of: " + ", ".join(CATEGORIES))
    severity: int = Field(ge=1, le=5, description="1 minor ... 5 major incident")
    casualties: int = Field(ge=0)
    trapped: int = Field(ge=0)
    weapons: bool
    hazards: list[str] = Field(default_factory=list)
    lat: float | None = Field(default=None, description="Only if you can place it in Sheffield; else null")
    lon: float | None = None
    place: str = Field(default="", description="Short place name, e.g. 'Park Hill flats'")
    rationale: str


class EvaluationDraft(BaseModel):
    score: int = Field(ge=0, le=100)
    verdict: Literal["resolved", "resolved_with_gaps", "follow_up_required"]
    findings: list[str] = Field(default_factory=list, description="At most 4, each one plain sentence")
    headline: str = Field(description="News headline, at most 90 characters, no jargon")
    dek: str = Field(description="One-sentence summary under the headline, at most 200 characters")


class Orchestrator:
    def __init__(self, db: Database, settings: Settings | None = None):
        self.db = db
        self.settings = settings or get_settings()
        self.agents = {a: SpecialistAgent(a, self.settings, db) for a in (Agency.police, Agency.ambulance, Agency.fire)}
        self.cr = ConflictResolverAgent(self.settings, db)
        self._ingest_lock = asyncio.Lock()

    # 0. ingest + de-duplicate ------------------------------------------------- #

    async def triage(self, incident: Incident) -> Triage:
        """Derive category, severity and coordinates. The canonical incident is not modified."""
        base = triage_rules.rule_triage(incident)
        if not self.settings.live:
            return base.model_copy(update={"executed_by": ExecutedBy.mock})
        prompt = (
            "Triage this emergency report for dispatch. Classify the category, grade severity 1-5 (5 = major "
            "incident, life at serious risk or many casualties), count casualties and trapped people, note weapons "
            "and hazards. Give coordinates only if you can place the location in Sheffield with confidence.\n"
            f"REPORT (canonical):\n{json.dumps(incident.canonical_json(), indent=2)}\n\n"
            f"Location already resolved by code: {base.location_resolved_by} ({base.place})"
        )
        try:
            d, _ = await chat_json(self.settings.orchestrator, SYSTEM, prompt, TriageDraft, label="Qwen triage")
        except LLMError as e:
            log.warning("Triage fell back to rules: %s", e)
            return base.model_copy(update={"executed_by": ExecutedBy.fallback})
        t = Triage(category=d.category, severity=d.severity, casualties=d.casualties, trapped=d.trapped,
                   weapons=d.weapons, hazards=d.hazards, lat=d.lat if d.lat is not None else base.lat,
                   lon=d.lon if d.lon is not None else base.lon, place=d.place or base.place,
                   rationale=briefing.clean(d.rationale, 300), executed_by=ExecutedBy.qwen)
        return triage_rules.check(t, base)

    async def decide_duplicate(self, incident: Incident, tri: Triage, proposal: DedupDecision,
                               candidates: list[dict]) -> DedupDecision:
        """Qwen makes the final duplicate call, with MiniLM's clustering as its evidence."""
        base = proposal.model_copy(update={"minilm_verdict": proposal.verdict,
                                           "minilm_match_incident_id": proposal.match_incident_id,
                                           "candidates": candidates})
        if not self.settings.live or not candidates:
            return base
        prompt = (
            "A new emergency report has arrived. Decide whether it is another caller describing an incident we already "
            "have, or a different incident.\n"
            "- duplicate: same event, place and time (callers word things and name places differently). It is merged "
            "into that incident and NOTHING is dispatched for it.\n"
            "- related: a different incident near an active one. It is dispatched separately, with the neighbour as context.\n"
            "- new: unrelated to every candidate. It is dispatched separately.\n"
            "MiniLM sentence-embedding clustering (adjusted distance; 0.85 or less means it clustered them) is your main "
            "evidence, but you make the final call. Merge only if you are confident it is the same incident. If it is "
            "probably a different incident, even if nearby or similar, choose related so it is dispatched separately: "
            "a missed incident costs far more than a second dispatch.\n\n"
            f"NEW REPORT (canonical) AND TRIAGE:\n{json.dumps({'incident': incident.canonical_json(), 'triage': tri.model_dump(mode='json')}, default=str)}\n\n"
            f"MINILM PROPOSAL: {proposal.verdict.value}" + (f" -> incident {proposal.match_incident_id}" if proposal.match_incident_id else "")
            + f" ({proposal.reason})\n\nCANDIDATES:\n{json.dumps(candidates, indent=2)}"
        )
        try:
            d, _ = await chat_json(self.settings.orchestrator, SYSTEM, prompt, DedupDraft, label="Qwen dedup")
        except LLMError as e:
            log.warning("Dedup decision fell back to MiniLM: %s", e)
            return base.model_copy(update={"reason": base.reason + " (agent unavailable; MiniLM verdict used)"})
        by_id = {c["incident_id"]: c for c in candidates}
        if d.verdict == "duplicate" and d.match_incident_id not in by_id:
            log.warning("Qwen chose a duplicate match outside the candidates (%s); using MiniLM", d.match_incident_id)
            return base.model_copy(update={"reason": base.reason + " (agent's match was invalid; MiniLM verdict used)"})
        match = d.match_incident_id if d.match_incident_id in by_id else None
        if d.verdict == "related" and match is None:
            match = min(candidates, key=lambda c: c["distance_m"] if c["distance_m"] is not None else 1e9)["incident_id"]
        c = by_id.get(match, {})
        return base.model_copy(update={
            "verdict": DedupVerdict(d.verdict), "match_incident_id": None if d.verdict == "new" else match,
            "score": c.get("adjusted_distance", base.score), "distance_m": c.get("distance_m"),
            "minutes_apart": c.get("minutes_apart"), "reason": briefing.clean(d.reason, 300), "executed_by": ExecutedBy.qwen,
        })

    async def ingest(self, incident: Incident) -> tuple[Case, DedupDecision]:
        """Number, triage, cluster and decide duplicates.

        The report is registered as pending inside the intake lock, so the next caller is clustered
        against it even while the agent is still deciding; the agent's decision runs outside the lock.
        Reports only ever merge into earlier incidents, so merges cannot form cycles.
        """
        tri = await self.triage(incident)
        async with self._ingest_lock:
            if incident.incident_id is not None and self.db.get_incident(incident.incident_id):
                raise ValueError(f"incident {incident.incident_id} already exists")
            incident.incident_id = self.db.next_id("incidents", "incident_id", incident.received_at)
            proposal, candidates = await asyncio.to_thread(dedup.propose, incident, tri, self.db.recent_incidents(dedup.WINDOW_MIN))
            self.db.insert_incident(incident, tri)
        case = Case(incident=incident, triage=tri)
        self.db.add_log(incident.incident_id, "received", briefing.log_received(case))

        decision = await self.decide_duplicate(incident, tri, proposal, candidates)
        if decision.verdict == DedupVerdict.duplicate:
            parent = self.db.merge_incident(incident.incident_id, decision.match_incident_id)
            decision = decision.model_copy(update={"match_incident_id": parent})
            self.db.set_dedup(incident.incident_id, decision)  # audit trail, also for merged reports
            count = self.db.get_incident(parent)["report_count"]
            where = f", {decision.distance_m:.0f} m away" if decision.distance_m is not None else ""
            mins = f", {decision.minutes_apart:.0f} min after the first" if decision.minutes_apart is not None else ""
            self.db.add_log(parent, "merged", f"Further report merged from {briefing.place(case)}{where}{mins} "
                                              f"({incident.channel}). {count} reports in total.")
            self.db.add_log(incident.incident_id, "merged", f"Merged into {parent}; no separate dispatch.")
            first = self.db.get_case(parent)
            worse = [f"severity {tri.severity}" if tri.severity > first.severity else "",
                     f"{tri.casualties} injured" if tri.casualties > first.casualties_reported else "",
                     f"{tri.trapped} trapped" if tri.trapped > first.people_trapped else ""]
            worse = [w for w in worse if w]
            if worse:
                # Re-assessing a live incident is a later feature; flag it so nobody misses it.
                self.db.add_log(parent, "flag", f"Merged report indicates {', '.join(worse)}. Current response has not been re-assessed.")
        else:
            self.db.set_dedup(incident.incident_id, decision)
            if tri.location_resolved_by in ("qwen", "default"):
                self.db.add_log(incident.incident_id, "flag",
                                f"Location not given precisely; placed at {tri.place or 'city centre'} ({tri.location_resolved_by}).")
            if decision.verdict == DedupVerdict.related:
                dist = f"{decision.distance_m:.0f} m from " if decision.distance_m is not None else "near "
                self.db.add_log(incident.incident_id, "related",
                                f"Separate incident {dist}{decision.match_incident_id}. Crews share scene information.")
        return case, decision

    # 1. routing ---------------------------------------------------------------- #

    async def route(self, incident: Case) -> RoutingDecision:
        agencies, lead, why = rule_route(incident)
        if not self.settings.live:
            return RoutingDecision(incident_id=incident.incident_id, agencies=agencies, lead_agency=lead,
                                   rationale=why, executed_by=ExecutedBy.mock)
        context = {
            "active_with_units_committed": self.db.active_incidents(),
            "pending": [i for i in self.db.incidents(IncidentStatus.pending) if i["incident_id"] != incident.incident_id],
        }
        prompt = (
            "Decide which agencies (police, ambulance, fire) must respond to this incident and which one leads.\n"
            f"INCIDENT (canonical) AND TRIAGE:\n{incident.prompt_json(indent=2)}\n\n"
            f"INCIDENT DB CONTEXT:\n{json.dumps(context, default=str)}"
        )
        try:
            d, _ = await chat_json(self.settings.orchestrator, SYSTEM, prompt, RoutingDraft, label="Qwen route")
            ags = list(dict.fromkeys(d.agencies))
            if d.lead_agency not in ags:
                ags.insert(0, d.lead_agency)
            return RoutingDecision(incident_id=incident.incident_id, agencies=ags, lead_agency=d.lead_agency,
                                   rationale=d.rationale, executed_by=ExecutedBy.qwen)
        except LLMError as e:
            log.warning("Routing fell back to rules: %s", e)
            return RoutingDecision(incident_id=incident.incident_id, agencies=agencies, lead_agency=lead,
                                   rationale=why, executed_by=ExecutedBy.fallback)

    # 4. evaluate ---------------------------------------------------------------- #

    async def evaluate(self, incident: Case, r: IncidentResponse) -> Evaluation:
        metrics = compute_metrics(incident, r)
        head, dek = briefing.default_headline(incident, r)
        base = rule_evaluation(incident, r, metrics, head, dek)
        if not self.settings.live:
            return base.model_copy(update={"executed_by": ExecutedBy.mock})
        summary = {
            "lead": r.routing.lead_agency.value, "status": r.status,
            "manifest": [m.model_dump(mode="json") for m in r.manifest],
            "dispatched": [{"callsign": a.callsign, "type": a.resource_type, "eta_min": a.eta_min, "crew": len(a.crew),
                            "reassigned_from": a.preempted_from} for a in r.allocations],
            "gaps": r.unmet, "orders": [d.model_dump(mode="json") for d in r.directives],
        }
        prompt = (
            "Judge this dispatched response. The METRICS were computed by code and are correct; base your judgement on "
            "them. Score 0-100. Verdict: resolved (all essential needs met in time), resolved_with_gaps, or "
            "follow_up_required (essential needs unmet). Give at most 4 findings, each one plain sentence a dispatcher "
            "understands. Then write a news headline (at most 90 characters) and a one-sentence dek (at most 200 "
            "characters) describing what happened and what was sent. No internal reasoning, no agent names.\n"
            f"INCIDENT (canonical) AND TRIAGE:\n{incident.prompt_json()}\n\nRESPONSE:\n{json.dumps(summary, default=str)}\n\n"
            f"METRICS:\n{json.dumps(metrics)}\n\nRule-based score for reference: {base.score}"
        )
        try:
            d, _ = await chat_json(self.settings.orchestrator, SYSTEM, prompt, EvaluationDraft, label="Qwen evaluate")
        except LLMError as e:
            log.warning("Evaluation fell back to rules: %s", e)
            return base.model_copy(update={"executed_by": ExecutedBy.fallback})
        verdict = d.verdict
        if verdict == "resolved" and metrics["essential_gap_units"]:
            verdict = "resolved_with_gaps"  # cannot call it resolved with essential units missing
        return Evaluation(score=d.score, grade=grade(d.score), verdict=verdict, metrics=metrics,
                          findings=[briefing.clean(f, 200) for f in d.findings[:4]],
                          headline=briefing.clean(d.headline, 90).rstrip("."), dek=briefing.clean(d.dek, 220),
                          executed_by=ExecutedBy.qwen)

    # full loop ------------------------------------------------------------------ #

    async def handle(self, report: Incident, on_event: EventFn = None) -> PipelineResult:
        incident, decision = await self.ingest(report)
        await _emit(on_event, "ingested", {"incident": incident.incident.canonical_json(),
                                           "triage": incident.triage.model_dump(mode="json"),
                                           "dedup": decision.model_dump(mode="json")})
        if decision.verdict == DedupVerdict.duplicate:
            await _emit(on_event, "merged", {"into": decision.match_incident_id, "reason": decision.reason})
            return PipelineResult(incident_id=incident.incident_id, outcome="merged", dedup=decision)

        related = [decision.match_incident_id] if decision.verdict == DedupVerdict.related else []
        if related:
            near = self.db.get_incident(related[0])
            incident.context["nearby_incident"] = {
                "incident_id": related[0], "distance_m": decision.distance_m,
                "category": near["triage"]["category"], "status": near["status"],
                "description": near["incident"]["description"],
            }

        await _emit(on_event, "routing", {})
        routing = await self.route(incident)
        self.db.add_log(incident.incident_id, "routed", briefing.log_routed(routing.lead_agency.value, [a.value for a in routing.agencies]))
        await _emit(on_event, "routed", routing.model_dump(mode="json"))

        async def run_agent(agency: Agency):
            await _emit(on_event, "agent_start", {"agency": agency.value, "agent_id": self.agents[agency].agent_id})
            resp = await self.agents[agency].respond(incident, routing)
            await _emit(on_event, "agent_done", resp.model_dump(mode="json"))
            return resp

        responses = list(await asyncio.gather(*(run_agent(a) for a in routing.agencies)))
        response = await self.cr.resolve_and_commit(incident, routing, responses, on_event=on_event,
                                                    dedup=decision, related=related)

        req, man = sum(response.requested_total.values()), sum(m.quantity for m in response.manifest)
        for line in (briefing.log_trimmed(req, man), briefing.log_dispatched(response), briefing.log_gaps(response)):
            if line:
                self.db.add_log(incident.incident_id, "dispatched", line)

        await _emit(on_event, "evaluating", {})
        response.evaluation = await self.evaluate(incident, response)
        self.db.update_response(response)
        self.db.add_log(incident.incident_id, "evaluated", briefing.log_evaluated(response.evaluation))
        await _emit(on_event, "evaluated", response.evaluation.model_dump(mode="json"))

        self.db.set_status(incident.incident_id, IncidentStatus.backlog)
        self.db.add_log(incident.incident_id, "completed", "Moved to backlog. Units return to service as they are released.")
        await _emit(on_event, "completed", {"incident_id": incident.incident_id, "status": "backlog",
                                            "response": response.model_dump(mode="json")})
        return PipelineResult(incident_id=incident.incident_id, outcome="failed" if response.status == "failed" else "dispatched",
                              dedup=decision, response=response)
