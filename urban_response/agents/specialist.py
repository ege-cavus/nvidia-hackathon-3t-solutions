"""Nemotron specialist agents: A1 police, A2 ambulance, A3 fire.

Each agent searches its own knowledge base, then asks Nemotron for a structured
AgentDraft: numeric resource requests, structured scene constraints and
free-text mission details. Agents state professional need. They are not shown
live availability, because resource accounting is the orchestrator's job.
"""

from __future__ import annotations

import json
import logging

from ..config import Settings
from ..db import Database
from ..kb import KnowledgeBase
from ..llm import LLMError, chat_json
from ..schemas import AGENT_ID, AgentResponse, Agency, ExecutedBy, Case, RoutingDecision
from .heuristics import HEURISTICS, AgentDraft

log = logging.getLogger(__name__)

PERSONAS = {
    Agency.police: "You are A1, the South Yorkshire Police HQ resource and response manager for Sheffield.",
    Agency.ambulance: "You are A2, the Yorkshire Ambulance Service HQ resource and response manager for Sheffield.",
    Agency.fire: "You are A3, the South Yorkshire Fire & Rescue HQ resource and response manager for Sheffield.",
}

RULES = """Decide the {agency} response to the incident.
- Request resources by professional need using the knowledge base guidance. Do not reduce requests because you
  expect shortages; the orchestrator does resource accounting across agencies and the city's Resource DB.
- resource_type MUST be one of: {types}. Types owned by other agencies must be requested through them
  (use inter_agency_needs), except shared types.
- Use priority 1 (essential), 2 (important) or 3 (desirable). Set max_eta_min only when lateness makes a unit useless.
- Fill scene_constraints precisely: cordon radii and distances in metres, scene_entry open/controlled/prohibited,
  requires_scene_entry if your crews must go into the scene, road closures and access routes by road name.
- Put agency-specific facts (triage category, command level, threat level, receiving hospital...) in
  mission_details.specialist_metadata.
- Cite the knowledge-base sections you relied on in kb_citations using their [id]."""


class SpecialistAgent:
    def __init__(self, agency: Agency, settings: Settings, db: Database):
        self.agency = agency
        self.agent_id = AGENT_ID[agency]
        self.settings = settings
        self.db = db
        self.kb = KnowledgeBase(settings.kb_dir / agency.value)

    def allowed_types(self) -> list[str]:
        return sorted(t for t, owner in self.db.resource_types().items() if owner in (self.agency.value, "shared"))

    def build_prompt(self, incident: Case, routing: RoutingDecision) -> tuple[str, str, list[str]]:
        query = " ".join([incident.category.replace("_", " "), incident.description, *incident.hazards,
                          *(p.notes or "" for p in incident.incident.patients)])
        chunks = self.kb.search(query, self.settings.kb_top_k)
        kb_text = "\n\n".join(f"[{c.citation}]\n{c.text}" for c in chunks) or "(no matching guidance)"
        system = PERSONAS[self.agency] + "\n\n" + RULES.format(agency=self.agency.value, types=", ".join(self.allowed_types()))
        context = {
            **incident.prompt_view(),  # canonical incident + triage (+ nearby context)
            "agencies_tasked": [a.value for a in routing.agencies],
            "lead_agency": routing.lead_agency.value,
        }
        if self.agency == Agency.ambulance:
            context["receiving_hospitals"] = self.db.hospitals(incident.location.lat, incident.location.lon)
        user = f"INCIDENT AND CONTEXT:\n{json.dumps(context, indent=2, default=str)}\n\nKNOWLEDGE BASE:\n{kb_text}"
        return system, user, [c.citation for c in chunks]

    async def respond(self, incident: Case, routing: RoutingDecision) -> AgentResponse:
        system, user, citations = self.build_prompt(incident, routing)
        latency = None
        if self.settings.live:
            try:
                draft, latency = await chat_json(self.settings.specialist, system, user, AgentDraft, label=f"{self.agent_id} {self.agency.value}")
                executed = ExecutedBy.nemotron
            except LLMError as e:
                log.warning("%s fell back to heuristics: %s", self.agent_id, e)
                draft, executed = HEURISTICS[self.agency](incident), ExecutedBy.fallback
                draft.kb_citations = citations
        else:
            draft, executed = HEURISTICS[self.agency](incident), ExecutedBy.mock
            draft.kb_citations = citations

        return AgentResponse(
            agent_id=self.agent_id,
            agency=self.agency,
            incident_id=incident.incident_id,
            executed_by=executed,
            latency_ms=latency,
            **draft.model_dump(),
        )
