"""Separate post-dispatch proposal contract; canonical caller/response JSON stays intact."""
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from .llm import LLMError, chat_json


class OutcomeProposal(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    proposal_id: str = Field(min_length=1, max_length=160)
    incident_id: int
    node_id: str
    world_version: int = Field(ge=0)
    tick: int = Field(ge=0)
    outcome: Literal["no_change", "partial_response", "effective_work", "contained", "resolved"]
    work_credit: int = Field(ge=0, le=1)
    reason: str = Field(min_length=1, max_length=600)


SYSTEM = """You are Qwen Outcome, a post-dispatch assessor for a fictional Sheffield game.
You propose actual graph effects from the supplied observations and explicit GAME POLICY.
Caller claims and dispatch estimates are not observed arrivals or completed work.
Copy proposal_id, incident_id, node_id, world_version and tick exactly from EVIDENCE.
Award work_credit=1 only when eligible_work_credit=1 and the observed typed resources can work.
You may withhold work (credit=0, outcome=no_change) if the supplied evidence is insufficient;
explain why. With insufficient capabilities award zero work: partial_response for relevant
on-scene attendance, otherwise no_change. Partial attendance slows spread but cannot resolve.
With credit=1 evaluate the NEW work total AFTER adding your credit, not observed.effective_work_ticks.
The code provides work_projection_if_credit_awarded to avoid arithmetic mistakes. Use its work_after.
If resolution_threshold_met=true the ONLY permitted work_credit=1 outcome is resolved, even when
observed.resolution_success is still false (that observation is BEFORE this effect is applied).
Otherwise use contained if containment_threshold_met=true, else effective_work.
Example: observed work=4, credit=1, resolution threshold=5 -> NEW work=5 -> resolved.
Extra resources give no bonus.
You cannot change requirements, create resources, release crews, or skip return/cooldown.
Return only the structured proposal. All durations and thresholds are game assumptions."""


def mock_proposal(evidence):
    """Offline demo decision; explicitly labelled mock by the caller."""
    credit = evidence["eligible_work_credit"]
    work = evidence["observed"]["effective_work_ticks"] + credit
    policy = evidence["game_policy"]
    if credit:
        outcome = ("resolved" if work >= policy["resolution_work_ticks"] else
                   "contained" if work >= policy["containment_work_ticks"] else "effective_work")
    else:
        outcome = "partial_response" if evidence["observed"]["response_state"] == "PARTIAL" else "no_change"
    return OutcomeProposal(**{k:evidence[k] for k in
        ("proposal_id", "incident_id", "node_id", "world_version", "tick")},
        outcome=outcome, work_credit=credit, reason="Offline game policy applied to observed resources and elapsed work.")


async def propose(settings, evidence):
    if not settings.live:
        return mock_proposal(evidence), "mock"
    import json
    try:
        proposal, _ = await chat_json(settings.orchestrator, SYSTEM,
            "EVIDENCE:\n" + json.dumps(evidence, default=str), OutcomeProposal, label="Qwen world outcome")
        return proposal, "qwen"
    except LLMError as error:
        # Failure is visible and causes no response benefit. Never invent a successful fallback.
        raise ValueError("Qwen outcome unavailable: " + str(error)) from error
