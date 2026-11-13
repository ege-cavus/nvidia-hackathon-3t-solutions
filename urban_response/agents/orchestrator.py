"""Qwen orchestrator: owns the incident from ingestion to completion. No human in the loop.

  0. ingest      register the report; de-duplicate against recent incidents
                 (rules score; Qwen judges the ambiguous band). Duplicates are merged, not dispatched.
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

from .. import briefing, dedup
from ..config import Settings, get_settings
from ..db import Database
from ..evaluation import compute_metrics, grade, rule_evaluation
from ..llm import LLMError, chat_json
from ..schemas import (
    Agency,
    DedupDecision,
    DedupVerdict,
    Evaluation,
    ExecutedBy,
    Incident,
    IncidentResponse,
    IncidentStatus,
    PipelineResult,
    RoutingDecision,
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
    match_incident_id: str | None = None
    reason: str


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

    async def _dedup_llm(self, incident: Incident, cands: list[dedup.Candidate]) -> DedupDecision | None:
        view = [{"incident_id": c.incident_id, "status": c.status, "distance_m": c.distance_m, "minutes_apart": c.minutes_apart,
                 "score": c.score, "category": c.incident.category, "description": c.incident.description,
                 "address": c.incident.location.address} for c in cands[:3]]
        prompt = (
            "A new report has arrived close to existing incidents. Decide whether it is another caller describing an "
            "incident we already have (duplicate: merge, do not dispatch again), a different incident nearby (related: "
            "dispatch separately), or unrelated (new). When unsure and lives may be at stake, prefer related over "
            "duplicate: a second dispatch costs less than a missed incident.\n"
            f"NEW REPORT:\n{incident.model_dump_json()}\n\nCANDIDATES:\n{json.dumps(view, indent=2)}"
        )
        try:
            d, _ = await chat_json(self.settings.orchestrator, SYSTEM, prompt, DedupDraft, label="Qwen dedup")
        except LLMError as e:
            log.warning("Dedup fell back to rules: %s", e)
            return None
        match = next((c for c in cands if c.incident_id == d.match_incident_id), cands[0])
        return DedupDecision(incident_id=incident.incident_id, verdict=DedupVerdict(d.verdict),
                             match_incident_id=None if d.verdict == "new" else match.incident_id,
                             score=match.score, distance_m=match.distance_m, minutes_apart=match.minutes_apart,
                             reason=briefing.clean(d.reason, 200), executed_by=ExecutedBy.qwen)

    async def ingest(self, incident: Incident) -> DedupDecision:
        """Register the report atomically with its duplicate check, so simultaneous callers see each other."""
        async with self._ingest_lock:
            if self.db.get_incident(incident.incident_id):
                return DedupDecision(incident_id=incident.incident_id, verdict=DedupVerdict.new, reason="already registered")
            cands = dedup.candidates(incident, self.db.recent_incidents(dedup.WINDOW_MIN))
            decision = dedup.rule_decision(incident, cands)
            if dedup.is_ambiguous(cands) and self.settings.live:
                decision = await self._dedup_llm(incident, cands) or decision
            if decision.verdict == DedupVerdict.duplicate:
                parent = decision.match_incident_id
                self.db.insert_incident(incident, status="merged", duplicate_of=parent)
                count = self.db.get_incident(parent)["report_count"]
                self.db.add_log(parent, "merged",
                                f"Further report merged from {briefing.place(incident)}, {decision.distance_m:.0f} m away and "
                                f"{decision.minutes_apart:.0f} min after the first. {count} reports in total.")
                self.db.add_log(incident.incident_id, "merged", f"Merged into {parent}; no separate dispatch.")
                first = Incident.model_validate(self.db.get_incident(parent)["incident"])
                worse = [f"severity {incident.severity}" if incident.severity > first.severity else "",
                         f"{incident.casualties_reported} injured" if incident.casualties_reported > first.casualties_reported else "",
                         f"{incident.people_trapped} trapped" if incident.people_trapped > first.people_trapped else ""]
                worse = [w for w in worse if w]
                if worse:
                    # Re-assessing a live incident is a later feature; flag it so nobody misses it.
                    self.db.add_log(parent, "flag", f"Merged report indicates {', '.join(worse)}. Current response has not been re-assessed.")
            else:
                self.db.insert_incident(incident)
                self.db.add_log(incident.incident_id, "received", briefing.log_received(incident))
                if decision.verdict == DedupVerdict.related:
                    self.db.add_log(incident.incident_id, "related",
                                    f"Separate incident {decision.distance_m:.0f} m from {decision.match_incident_id}. Crews share scene information.")
            return decision

    # 1. routing ---------------------------------------------------------------- #

    async def route(self, incident: Incident) -> RoutingDecision:
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
            f"INCIDENT:\n{incident.model_dump_json(indent=2)}\n\n"
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

    async def evaluate(self, incident: Incident, r: IncidentResponse) -> Evaluation:
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
            f"INCIDENT:\n{incident.model_dump_json()}\n\nRESPONSE:\n{json.dumps(summary, default=str)}\n\n"
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

    async def handle(self, incident: Incident, on_event: EventFn = None) -> PipelineResult:
        decision = await self.ingest(incident)
        await _emit(on_event, "ingested", {"incident": incident.model_dump(mode="json"), "dedup": decision.model_dump(mode="json")})
        if decision.verdict == DedupVerdict.duplicate:
            await _emit(on_event, "merged", {"into": decision.match_incident_id, "reason": decision.reason})
            return PipelineResult(incident_id=incident.incident_id, outcome="merged", dedup=decision)

        related = [decision.match_incident_id] if decision.verdict == DedupVerdict.related else []
        if related:
            near = self.db.get_incident(related[0])
            incident.metadata["nearby_incident"] = {
                "incident_id": related[0], "distance_m": decision.distance_m,
                "category": near["incident"]["category"], "status": near["status"],
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
