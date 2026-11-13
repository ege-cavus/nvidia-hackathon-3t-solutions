"""Canonical data model.

Everything that crosses an agent boundary is one of these types. Resource
requests are strictly typed and numeric so hard-conflict accounting is pure
code. Scene constraints are structured so the common soft conflicts can be
detected by rules; free-text mission details go to the orchestrator for
semantic review.
"""

from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Literal
from uuid import uuid4

from pydantic import BaseModel, Field


def _now() -> datetime:
    return datetime.now(timezone.utc)


# --------------------------------------------------------------------------- #
# Enums
# --------------------------------------------------------------------------- #


class Agency(str, Enum):
    police = "police"  # Nemotron A1
    ambulance = "ambulance"  # Nemotron A2
    fire = "fire"  # Nemotron A3
    shared = "shared"  # owned by the city, usable by any agency (drones, command units)


AGENT_ID = {Agency.police: "A1", Agency.ambulance: "A2", Agency.fire: "A3"}
CR_AGENT_ID = "CR"  # Nemotron conflict-resolution agent: makes the final call


class IncidentStatus(str, Enum):
    pending = "pending"  # ingested, pipeline running
    merged = "merged"  # duplicate report of another incident; folded into it, nothing dispatched
    dispatched = "dispatched"  # CR committed units; awaiting Qwen evaluation
    backlog = "backlog"  # evaluated and finalised; units still out until their release time
    closed = "closed"  # every unit and crew member back in service


class ExecutedBy(str, Enum):
    nemotron = "nemotron"  # produced by a Nemotron model (A1-A3 or CR)
    qwen = "qwen"  # produced by the Qwen orchestrator model
    fallback = "fallback"  # the model call failed; deterministic rules filled in
    mock = "mock"  # LLM_MODE=mock, no model server involved
    rules = "rules"  # pure code (conflict accounting etc.)
    floor = "floor"  # life-safety minimum enforced by code over the CR manifest


class SceneEntry(str, Enum):
    open = "open"
    controlled = "controlled"  # entry only with the agency's permission / PPE
    prohibited = "prohibited"


class RequestPriority(int, Enum):
    essential = 1  # the response fails without it
    important = 2
    desirable = 3


# --------------------------------------------------------------------------- #
# Incident (input)
# --------------------------------------------------------------------------- #


class Location(BaseModel):
    lat: float
    lon: float
    address: str = ""
    district: str = ""


class Incident(BaseModel):
    """Normalised incident as received by the orchestrator."""

    incident_id: str = Field(default_factory=lambda: f"INC-{uuid4().hex[:8].upper()}")
    received_at: datetime = Field(default_factory=_now)
    source: str = "999"
    category: str = Field(description="e.g. structure_fire, rtc, assault, medical, hazmat, flood")
    description: str
    location: Location
    severity: int = Field(ge=1, le=5, description="1 = minor, 5 = major incident")
    casualties_reported: int = 0
    people_trapped: int = 0
    hazards: list[str] = Field(default_factory=list)
    weapons_involved: bool = False
    metadata: dict = Field(default_factory=dict)


# --------------------------------------------------------------------------- #
# Orchestrator routing
# --------------------------------------------------------------------------- #


class RoutingDecision(BaseModel):
    incident_id: str
    agencies: list[Agency]
    lead_agency: Agency
    rationale: str
    executed_by: ExecutedBy


# --------------------------------------------------------------------------- #
# Specialist agent response
# --------------------------------------------------------------------------- #


class ResourceRequest(BaseModel):
    resource_type: str = Field(description="Must match a resource_type in the Resource DB")
    quantity: int = Field(ge=1)
    priority: RequestPriority = RequestPriority.essential
    max_eta_min: float | None = Field(default=None, description="Units further away than this are not useful")
    expected_duration_min: int = Field(default=60, ge=1, description="How long the units will be committed")
    justification: str = ""


class InterAgencyNeed(BaseModel):
    agency: Agency
    need: str = Field(description="e.g. 'extrication of trapped casualty', 'traffic control at junction'")


class SceneConstraints(BaseModel):
    """Structured tactical picture; this is what soft-conflict rules read."""

    scene_entry: SceneEntry = SceneEntry.open
    cordon_inner_radius_m: int = 0
    cordon_outer_radius_m: int = 0
    evacuation_radius_m: int = 0
    staging_area: str = ""
    staging_distance_m: int | None = Field(default=None, description="Distance of staging area from the scene")
    requires_scene_entry: bool = False
    road_closures: list[str] = Field(default_factory=list)
    access_routes_required: list[str] = Field(default_factory=list)
    preserve_evidence: bool = False
    inter_agency_needs: list[InterAgencyNeed] = Field(default_factory=list)


class MissionDetails(BaseModel):
    summary: str
    tactical_plan: list[str] = Field(default_factory=list)
    hazards_identified: list[str] = Field(default_factory=list)
    safety_notes: list[str] = Field(default_factory=list)
    specialist_metadata: dict = Field(default_factory=dict, description="Agency-specific fields, e.g. triage category")


class AgentResponse(BaseModel):
    agent_id: str
    agency: Agency
    incident_id: str
    resource_requests: list[ResourceRequest]
    scene_constraints: SceneConstraints
    mission_details: MissionDetails
    kb_citations: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.7, ge=0, le=1)
    executed_by: ExecutedBy
    latency_ms: int | None = None


# --------------------------------------------------------------------------- #
# Conflicts
# --------------------------------------------------------------------------- #


class HardConflictKind(str, Enum):
    crew_shortfall = "crew_shortfall"  # vehicles exist but not enough on-shift people with the right speciality
    shortfall = "shortfall"  # requested > available in the Resource DB
    contention = "contention"  # several agencies want the same pool and it cannot cover them all
    ownership = "ownership"  # agency asked for another agency's resource type
    unknown_resource = "unknown_resource"
    eta_unmet = "eta_unmet"  # units exist, but none within the requested max ETA


class HardConflict(BaseModel):
    kind: HardConflictKind
    resource_type: str
    requested: int
    available: int
    agencies: list[Agency]
    detail: str
    preemptible: list[dict] = Field(
        default_factory=list,
        description="Units of this type held by lower-severity active incidents: {unit_id, incident_id, severity}",
    )


class SoftConflictSource(str, Enum):
    rules = "rules"
    semantic = "semantic"  # found by the orchestrator LLM reading mission details


class SoftConflict(BaseModel):
    code: str = Field(description="Stable identifier, e.g. ENTRY_PROHIBITED_BUT_REQUIRED")
    agencies: list[Agency]
    severity: Literal["low", "medium", "high"]
    detail: str
    suggested_resolution: str = ""
    source: SoftConflictSource


# --------------------------------------------------------------------------- #
# Resolution and final plan
# --------------------------------------------------------------------------- #


class ResolutionAction(str, Enum):
    accept_partial = "accept_partial"  # dispatch what is available, record the gap
    preempt = "preempt"  # take units from a lower-severity incident
    mutual_aid = "mutual_aid"  # request from neighbouring authorities (Rotherham, Barnsley, Derbyshire)
    substitute = "substitute"  # use a different resource type
    relax_eta = "relax_eta"  # accept units beyond the requested max ETA


class HardResolution(BaseModel):
    resource_type: str
    action: ResolutionAction
    substitute_type: str | None = None
    preempt_unit_ids: list[str] = Field(default_factory=list)
    mutual_aid_for_remainder: bool = Field(default=False, description="Request mutual aid for whatever the action does not cover")
    rationale: str
    executed_by: ExecutedBy


class CrewMember(BaseModel):
    person_id: str
    speciality: str
    station: str


class UnitAllocation(BaseModel):
    unit_id: str
    callsign: str
    resource_type: str
    agency: Agency
    station: str
    eta_min: float
    requested_by: Agency
    preempted_from: str | None = None
    crew: list[CrewMember] = Field(default_factory=list)
    task: str = ""


# --------------------------------------------------------------------------- #
# Ingestion: duplicate detection
# --------------------------------------------------------------------------- #


class DedupVerdict(str, Enum):
    new = "new"  # nothing comparable nearby
    duplicate = "duplicate"  # another report of an incident we already have: merge, don't dispatch
    related = "related"  # a different incident close to an active one: dispatch, share scene context


class DedupDecision(BaseModel):
    incident_id: str
    verdict: DedupVerdict
    match_incident_id: str | None = None
    score: float = 0.0
    distance_m: float | None = None
    minutes_apart: float | None = None
    reason: str = ""
    executed_by: ExecutedBy = ExecutedBy.rules


# --------------------------------------------------------------------------- #
# CR final declaration
# --------------------------------------------------------------------------- #


class ManifestItem(BaseModel):
    """CR's final declaration of what is actually sent, per resource type and agency."""

    resource_type: str
    quantity: int = Field(ge=0)
    for_agency: Agency
    priority: RequestPriority = RequestPriority.essential
    reason: str = Field(default="", description="One short line: why this many")


class Directive(BaseModel):
    """One radio-style order for one agency. Imperative, no reasoning."""

    agency: Literal["police", "ambulance", "fire", "all"]
    action: str = Field(description="Imperative order, at most ~160 characters")
    priority: Literal["immediate", "high", "routine"] = "high"


# --------------------------------------------------------------------------- #
# Qwen evaluation (after dispatch)
# --------------------------------------------------------------------------- #


class Evaluation(BaseModel):
    score: int = Field(ge=0, le=100)
    grade: Literal["A", "B", "C", "D", "F"]
    verdict: Literal["resolved", "resolved_with_gaps", "follow_up_required"]
    metrics: dict = Field(default_factory=dict, description="Deterministic success metrics computed in code")
    findings: list[str] = Field(default_factory=list, description="At most 4 plain-language findings")
    headline: str = ""
    dek: str = ""
    executed_by: ExecutedBy = ExecutedBy.rules


class LogEntry(BaseModel):
    ts: datetime
    kind: str
    text: str


class IncidentResponse(BaseModel):
    """The final response to an incident, decided by the CR agent and stored in the response DB.

    One incident can have several responses over time (e.g. a re-dispatch); each response
    points at its incident through incident_id (foreign key).
    """

    response_id: str = Field(default_factory=lambda: f"RSP-{uuid4().hex[:8].upper()}")
    incident_id: str
    created_at: datetime = Field(default_factory=_now)
    decided_by: str = CR_AGENT_ID
    cr_executed_by: ExecutedBy = ExecutedBy.rules
    cr_rationale: str = ""
    routing: RoutingDecision
    dedup: DedupDecision | None = None
    related_incidents: list[str] = Field(default_factory=list)
    agent_responses: list[AgentResponse]
    requested_total: dict[str, int] = Field(default_factory=dict, description="What A1-A3 asked for, summed per type")
    manifest: list[ManifestItem] = Field(default_factory=list, description="CR's final declaration")
    manifest_adjustments: list[str] = Field(default_factory=list, description="Life-safety floors code applied over CR")
    hard_conflicts: list[HardConflict]
    hard_resolutions: list[HardResolution]
    soft_conflicts: list[SoftConflict]
    allocations: list[UnitAllocation]
    unmet: list[dict] = Field(default_factory=list, description="{resource_type, quantity, requested_by} still uncovered")
    directives: list[Directive] = Field(default_factory=list, description="Normalised orders per agency")
    status: Literal["dispatched", "partial", "failed"] = "dispatched"
    evaluation: Evaluation | None = None


class PipelineResult(BaseModel):
    incident_id: str
    outcome: Literal["dispatched", "merged", "failed"]
    dedup: DedupDecision
    response: IncidentResponse | None = None
