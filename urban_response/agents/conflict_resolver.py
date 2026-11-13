"""CR: the Nemotron conflict-resolution agent. It makes the final call and dispatches.

Once A1-A3 have answered, everything stays inside the Nemotron world until dispatch:

  1. deterministic tools: hard-conflict accounting on the raw requests, soft-conflict rules
  2. one Nemotron call (CR) declares
       - the MANIFEST: final quantity per resource type and agency, consolidating overlaps
         and trimming nice-to-haves, but erring on abundance where lives are at stake
       - a resolution for each resource that will run short (from the valid options)
       - extra soft conflicts found in the agents' free text
       - normalised orders per agency (radio style, no reasoning)
  3. code enforces life-safety floors and caps over the manifest, re-detects conflicts on it,
     re-validates CR's resolutions (invalid ones fall back to the rule policy)
  4. code allocates fully crewed units and writes the response (incident -> dispatched)

Steps 1-4 run under a lock so concurrent incidents cannot double-book units or people.
"""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Literal

from pydantic import BaseModel, Field

from .. import manifest as mf
from .. import skills
from ..briefing import normalise_directives
from ..config import Settings
from ..conflicts import hard, soft
from ..db import Database
from ..llm import LLMError, chat_json
from ..schemas import (
    CR_AGENT_ID,
    AgentResponse,
    Agency,
    Directive,
    ExecutedBy,
    HardConflict,
    HardConflictKind,
    HardResolution,
    Case,
    IncidentResponse,
    ManifestItem,
    RequestPriority,
    ResolutionAction,
    RoutingDecision,
    SoftConflict,
    SoftConflictSource,
)

log = logging.getLogger(__name__)

SYSTEM = (
    "You are CR, the Nemotron conflict-resolution agent for Sheffield multi-agency emergency response. "
    "Specialist agents A1 (police), A2 (ambulance) and A3 (fire & rescue) have proposed their responses. "
    "Each plans only for its own speciality, so together they over-ask: overlapping scene assets, nice-to-haves. "
    "You make the final call on what is actually sent. Preserve life first, then prevent escalation, then protect "
    "property and evidence. Never send things just to send them; never under-send where lives are at stake."
)


def system_prompt() -> str:
    """CR's system prompt, with the arbiter skill (nemotron_resource_skills/resource_allocator_skill) as its policy."""
    skill = skills.arbiter_skill()
    if not skill:
        return SYSTEM
    return (SYSTEM + "\n\nARBITER SKILL (nemotron_resource_skills/resource_allocator_skill/SKILL.md): apply this decision "
            "policy when you declare the manifest and resolve shortages.\n" + skill +
            "\n\nHow it maps onto this system: the agents' requests are the specialist proposals. Reassigning a unit "
            "already committed is allowed only when the code offers it as a 'preempt' option (from an incident at least two "
            "severity levels lower); substitutes are allowed only from substitutes_with_spare_units, which is the "
            "interchangeability data. Your decision is final; there is no human review step. Output format: as instructed below.")

INSTRUCTIONS = """Make the final decision for this incident.

1. MANIFEST: declare the final quantity of each resource type to send and which agency it serves.
   - One drone or mobile command unit serves the whole scene; do not send one per agency.
   - The agencies' quantities are the default (ARBITER SKILL). Cut a unit only with a specific rationale: redundant,
     speculative, unsupported by the report, or duplicating a task already covered. Say why in its reason.
   - Where people are injured, trapped or at risk, send what is needed in abundance.
   - Use only resource types from the catalogue. Quantity 0 means not sent.
   - reason: one short line, e.g. "one per P1 casualty", "shared scene overwatch".
   Code will raise any item below the life-safety floors listed, so you cannot cut below them.

2. RESOLUTIONS for resources that will run short (see HARD CONFLICTS and AVAILABILITY). For each, choose one
   action from its valid_actions: preempt (unit_ids only from that conflict's preemptible list), substitute
   (substitute_type from substitutes_with_spare_units), mutual_aid, relax_eta, accept_partial. Set
   mutual_aid_for_remainder=true to request mutual aid for anything left uncovered.

3. ADDITIONAL soft conflicts: contradictions in the agents' plans that the rules (ALREADY FOUND) missed.
   Short UPPER_SNAKE code each. Do not repeat ALREADY FOUND.

4. DIRECTIVES: 3-8 orders that crews will read on a phone. Each is for one agency (or "all"), imperative,
   one sentence, at most 160 characters, no reasoning or hedging. Example: "Stage at Hanover Way north,
   100 m upwind; await escort into the inner cordon." The lead agency is {lead}."""


# ---- Nemotron-facing output model ------------------------------------------ #


class ResolutionChoice(BaseModel):
    resource_type: str
    action: ResolutionAction
    substitute_type: str | None = None
    preempt_unit_ids: list[str] = Field(default_factory=list)
    mutual_aid_for_remainder: bool = False
    rationale: str


class SemanticConflict(BaseModel):
    code: str
    agencies: list[Agency]
    severity: Literal["low", "medium", "high"]
    detail: str
    suggested_resolution: str


class ManifestChoice(BaseModel):
    resource_type: str
    quantity: int = Field(ge=0)
    for_agency: Agency
    priority: int = Field(default=1, ge=1, le=3, description="1 essential, 2 important, 3 desirable")
    reason: str


class CRDecision(BaseModel):
    manifest: list[ManifestChoice]
    resolutions: list[ResolutionChoice] = Field(default_factory=list)
    additional_conflicts: list[SemanticConflict] = Field(default_factory=list)
    directives: list[Directive]
    rationale: str = ""


# ---- rule fallbacks ----------------------------------------------------------- #


def rule_directives(routing: RoutingDecision, responses: list[AgentResponse], conflicts: list[SoftConflict]) -> list[Directive]:
    lead = routing.lead_agency
    lead_resp = next((r for r in responses if r.agency == lead), None)
    rvp = lead_resp.scene_constraints.staging_area if lead_resp and lead_resp.scene_constraints.staging_area else "the lead agency RVP"
    out = [Directive(agency="all", priority="immediate", action=f"{lead.value.capitalize()} leads. All crews report to the single RVP: {rvp}.")]
    for r in responses:
        if r.mission_details.tactical_plan:
            out.append(Directive(agency=r.agency.value, priority="immediate", action=r.mission_details.tactical_plan[0]))
    rank = {"high": "immediate", "medium": "high", "low": "routine"}
    for c in conflicts:
        if c.suggested_resolution:
            who = c.agencies[0].value if len(c.agencies) == 1 else "all"
            out.append(Directive(agency=who, priority=rank[c.severity], action=c.suggested_resolution))
    return out


class ConflictResolverAgent:
    agent_id = CR_AGENT_ID

    def __init__(self, settings: Settings, db: Database):
        self.settings = settings
        self.db = db
        self._lock = asyncio.Lock()

    @staticmethod
    def _actionable(conflicts: list[HardConflict]) -> dict[str, HardConflict]:
        out: dict[str, HardConflict] = {}
        for c in conflicts:
            if c.kind != HardConflictKind.ownership:
                out.setdefault(c.resource_type, c)
        return out

    def build_prompt(self, incident, routing, responses, raw_conflicts, rule_soft, floors) -> str:
        demand = hard.demand_totals(responses)
        actionable = self._actionable(raw_conflicts)
        hard_view = {
            t: {"conflict": json.loads(c.model_dump_json()),
                "valid_actions": [a.value for a in hard.resolution_options(c, self.db, demand)],
                "substitutes_with_spare_units": dict(hard.spare_substitutes(t, self.db, demand))}
            for t, c in actionable.items()
        }
        agents = {
            f"{r.agent_id} {r.agency.value}": {
                "resource_requests": [q.model_dump(mode="json") for q in r.resource_requests],
                "scene_constraints": r.scene_constraints.model_dump(mode="json"),
                "mission_details": r.mission_details.model_dump(mode="json"),
            }
            for r in responses
        }
        availability = {i["resource_type"]: i["available"] for i in self.db.inventory()}
        people = {p["speciality"]: p["available"] for p in self.db.personnel_inventory()}
        return (
            INSTRUCTIONS.format(lead=routing.lead_agency.value)
            + f"\n\nINCIDENT (canonical) AND TRIAGE:\n{incident.prompt_json()}"
            + f"\n\nAGENT RESPONSES:\n{json.dumps(agents, indent=2)}"
            + f"\n\nLIFE-SAFETY FLOORS (code enforces): {json.dumps({k: [v[0], v[1].value, v[2]] for k, v in floors.items()})}"
            + f"\n\nAVAILABILITY (vehicles free now): {json.dumps(availability)}"
            + f"\nPERSONNEL FREE BY SPECIALITY: {json.dumps(people)}"
            + f"\n\nHARD CONFLICTS (on the raw requests):\n{json.dumps(hard_view, indent=2) if hard_view else 'none'}"
            + f"\n\nALREADY FOUND (soft, rules):\n{json.dumps([c.model_dump(mode='json') for c in rule_soft])}"
            + f"\n\nACTIVE INCIDENTS:\n{json.dumps(self.db.active_incidents(), default=str)}"
        )

    async def _decide(self, incident, routing, responses, raw_conflicts, rule_soft, floors):
        """Returns (manifest, resolution choices or None, semantic soft, directives, executed_by, rationale)."""
        fallback_manifest = mf.rule_manifest(incident, routing, responses)
        fallback_dirs = rule_directives(routing, responses, rule_soft)
        if not self.settings.live:
            return fallback_manifest, None, [], fallback_dirs, ExecutedBy.mock, "Rule policy (mock mode)"
        prompt = self.build_prompt(incident, routing, responses, raw_conflicts, rule_soft, floors)
        try:
            d, _ = await chat_json(self.settings.resolver, system_prompt(), prompt, CRDecision, label="CR")
        except LLMError as e:
            log.warning("CR fell back to rules: %s", e)
            return fallback_manifest, None, [], fallback_dirs, ExecutedBy.fallback, f"Rule policy (CR unavailable: {e})"
        manifest = [ManifestItem(resource_type=m.resource_type, quantity=m.quantity, for_agency=m.for_agency,
                                 priority=RequestPriority(m.priority), reason=m.reason[:160]) for m in d.manifest]
        semantic = [SoftConflict(**c.model_dump(), source=SoftConflictSource.semantic) for c in d.additional_conflicts]
        return manifest, d.resolutions, semantic, d.directives or fallback_dirs, ExecutedBy.nemotron, d.rationale

    def _resolve(self, incident, conflicts, demand, choices, executed_by) -> list[HardResolution]:
        actionable = self._actionable(conflicts)
        defaults = {t: hard.default_resolution(c, incident, self.db, demand) for t, c in actionable.items()}
        if choices is None:
            tag = ExecutedBy.rules if executed_by == ExecutedBy.mock else ExecutedBy.fallback
            return [r.model_copy(update={"executed_by": tag}) if executed_by != ExecutedBy.mock else r for r in defaults.values()]
        chosen: dict[str, HardResolution] = {}
        for ch in choices:
            if ch.resource_type in actionable and ch.resource_type not in chosen:
                res = HardResolution(**ch.model_dump(), executed_by=ExecutedBy.nemotron)
                valid = hard.validate_resolution(res, actionable[ch.resource_type], self.db, demand)
                if valid:
                    chosen[ch.resource_type] = valid
                else:
                    log.warning("CR choice for %s rejected by validation: %s", ch.resource_type, ch.action.value)
        return [chosen.get(t) or defaults[t].model_copy(update={"executed_by": ExecutedBy.fallback}) for t in actionable]

    async def resolve_and_commit(
        self, incident: Case, routing: RoutingDecision, responses: list[AgentResponse], on_event=None,
        dedup=None, related: list[str] | None = None,
    ) -> IncidentResponse:
        async def emit(name: str, payload: dict) -> None:
            if on_event:
                try:
                    await on_event(name, payload)
                except Exception:
                    log.exception("event listener failed on %s", name)

        async with self._lock:
            raw_conflicts = hard.detect(incident, responses, self.db)
            rule_soft = soft.detect_rules(responses, routing.agencies)
            floors = mf.life_safety_floors(incident, routing)
            await emit("conflicts_detected", {
                "hard": [c.model_dump(mode="json") for c in raw_conflicts],
                "soft_rules": [c.model_dump(mode="json") for c in rule_soft],
            })

            await emit("cr_start", {})
            manifest, choices, semantic_soft, directives, executed_by, rationale = await self._decide(
                incident, routing, responses, raw_conflicts, rule_soft, floors)

            # Guardrails over CR's declaration, then accounting on what will actually be sent.
            manifest, adjustments = mf.enforce(manifest, incident, routing, responses, self.db.resource_types())
            demands = mf.to_demands(manifest, responses)
            conflicts = hard.detect(incident, demands, self.db)
            demand = hard.demand_totals(demands)
            resolutions = self._resolve(incident, conflicts, demand, choices, executed_by)
            directives = normalise_directives(directives)
            requested = mf.requested_totals(responses)

            await emit("cr_decided", {
                "executed_by": executed_by.value,
                "rationale": rationale,
                "requested_total": requested,
                "manifest": [m.model_dump(mode="json") for m in manifest],
                "manifest_adjustments": adjustments,
                "hard": [c.model_dump(mode="json") for c in conflicts],
                "resolutions": [r.model_dump(mode="json") for r in resolutions],
                "soft_semantic": [c.model_dump(mode="json") for c in semantic_soft],
                "directives": [d.model_dump(mode="json") for d in directives],
            })
            allocations, unmet = hard.allocate(incident, demands, resolutions, routing.lead_agency, self.db,
                                               tasks=mf.tasks(manifest))

            essential = {(m.for_agency.value, m.resource_type) for m in manifest if m.priority == RequestPriority.essential}
            essential_gap = any((u["requested_by"], u["resource_type"]) in essential for u in unmet)
            status = "failed" if not allocations else ("partial" if essential_gap else "dispatched")

            response = IncidentResponse(
                incident_id=incident.incident_id,
                incident=incident.incident,
                triage=incident.triage,
                cr_executed_by=executed_by,
                cr_rationale=rationale,
                routing=routing,
                dedup=dedup,
                related_incidents=related or [],
                agent_responses=responses,
                requested_total=requested,
                manifest=manifest,
                manifest_adjustments=adjustments,
                hard_conflicts=conflicts,
                hard_resolutions=resolutions,
                soft_conflicts=rule_soft + semantic_soft,
                allocations=allocations,
                unmet=unmet,
                directives=directives,
                status=status,
            )
            self.db.record_response(incident, response)
            await emit("committed", response.model_dump(mode="json"))
        return response
