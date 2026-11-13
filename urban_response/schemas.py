"""Canonical data model.

Everything that crosses an agent boundary is one of these types. Resource
requests are strictly typed and numeric so hard-conflict accounting is pure
code. Scene constraints are structured so the common soft conflicts can be
detected by rules; free-text mission details go to the orchestrator for
semantic review.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone
from enum import Enum
from typing import Literal

from pydantic import BaseModel, Field, model_validator


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
    minilm = "minilm"  # MiniLM sentence-embedding clustering (incident_cluster_handoff.py)
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
# Incident: the canonical intake record (examples/incident.schema.json)
# --------------------------------------------------------------------------- #
#
# This is THE incident. It is what callers, alarm panels and apps send, what is stored in
# the incident DB, what the MiniLM de-duplicator clusters, and what every agent is shown.
# It is never rewritten. Everything the pipeline derives from it (category, severity,
# resolved coordinates) lives in a separate Triage object.


class Caller(BaseModel):
    name: str | None = None
    phone: str | None = None
    relationship: Literal["witness", "involved", "passer_by", "resident", "third_party"] | None = None
    on_scene: bool | None = None


class IncidentLocation(BaseModel):
    address: str | None = None
    landmark: str | None = None
    lat: float | None = None
    lon: float | None = None
    location_source: Literal["caller_gps", "caller_stated", "cell_mast", "alarm_panel"] | None = None
    location_notes: str | None = None

    @model_validator(mode="after")
    def _some_place(self):
        if not (self.address or self.landmark or (self.lat is not None and self.lon is not None)):
            raise ValueError("location needs an address, a landmark, or lat and lon")
        return self


class Patient(BaseModel):
    approx_age: str | None = None
    sex: Literal["male", "female", "unknown"] | None = None
    conscious: bool | None = None
    breathing: Literal["normal", "shallow", "none"] | None = None
    bleeding: bool | None = None
    notes: str | None = None


class PersonOfInterest(BaseModel):
    role: Literal["suspect", "missing", "victim", "other"]
    description: str | None = None
    direction_of_travel: str | None = None
    armed: bool | None = None


class Vehicle(BaseModel):
    type: str | None = None
    description: str | None = None
    registration: str | None = None
    occupants: int | None = Field(default=None, ge=0)


class Incident(BaseModel):
    """Canonical incident report. Field-for-field the JSON Schema in examples/incident.schema.json."""

    incident_id: int | None = Field(default=None, description="YYYYMMDDNNNNNN, assigned by the database on ingest")
    received_at: datetime = Field(default_factory=_now)
    channel: Literal["999", "112", "text", "app", "alarm"] = "999"
    caller: Caller | None = None
    location: IncidentLocation
    description: str = Field(min_length=1)
    caller_reported_severity: str | None = None
    casualties_reported: int | None = Field(default=None, ge=0)
    people_trapped_reported: int | None = Field(default=None, ge=0)
    patients: list[Patient] = Field(default_factory=list)
    hazards_reported: list[str] = Field(default_factory=list)
    weapons_mentioned: bool | None = None
    persons_of_interest: list[PersonOfInterest] = Field(default_factory=list)
    vehicles: list[Vehicle] = Field(default_factory=list)

    def canonical_json(self) -> dict:
        """The record as stored and as shown to agents: schema fields only, nulls kept."""
        return self.model_dump(mode="json")


CATEGORIES = [
    "rtc", "structure_fire", "high_rise_fire", "vehicle_fire", "hazmat", "gas_leak", "flood", "medical",
    "cardiac_arrest", "overdose", "fall", "assault", "stabbing", "firearms", "robbery", "public_disorder",
    "missing_person", "other",
]


class Triage(BaseModel):
    """What the orchestrator derives from a canonical incident at intake. Never written back into it."""

    category: str = Field(description="One of CATEGORIES")
    severity: int = Field(ge=1, le=5, description="1 = minor, 5 = major incident")
    lat: float
    lon: float
    location_resolved_by: Literal["caller", "gazetteer", "qwen", "default"] = "caller"
    place: str = Field(default="", description="Short human place name for logs and briefs")
    casualties: int = 0
    trapped: int = 0
    weapons: bool = False
    hazards: list[str] = Field(default_factory=list)
    rationale: str = ""
    executed_by: ExecutedBy = ExecutedBy.rules


class Location(BaseModel):
    lat: float
    lon: float
    address: str = ""
    district: str = ""


class Case(BaseModel):
    """Read-only working view used inside the pipeline: canonical incident + triage.

    Downstream code reads the derived fields below; agents are shown prompt_view(), which is
    the canonical incident plus the triage, never a rewritten incident.
    """

    incident: Incident
    triage: Triage
    context: dict = Field(default_factory=dict, description="e.g. a nearby related incident")

    @property
    def incident_id(self) -> int:
        return self.incident.incident_id

    @property
    def received_at(self) -> datetime:
        return self.incident.received_at

    @property
    def description(self) -> str:
        return self.incident.description

    @property
    def category(self) -> str:
        return self.triage.category

    @property
    def severity(self) -> int:
        return self.triage.severity

    @property
    def location(self) -> Location:
        loc = self.incident.location
        return Location(lat=self.triage.lat, lon=self.triage.lon,
                        address=loc.address or loc.landmark or self.triage.place, district=self.triage.place)

    @property
    def casualties_reported(self) -> int:
        return self.triage.casualties

    @property
    def people_trapped(self) -> int:
        return self.triage.trapped

    @property
    def hazards(self) -> list[str]:
        return self.triage.hazards

    @property
    def weapons_involved(self) -> bool:
        return self.triage.weapons

    @property
    def source(self) -> str:
        return self.incident.channel

    def prompt_view(self) -> dict:
        out = {"incident": self.incident.canonical_json(), "triage": self.triage.model_dump(mode="json")}
        if self.context:
            out["context"] = self.context
        return out

    def prompt_json(self, indent: int | None = None) -> str:
        return json.dumps(self.prompt_view(), indent=indent, default=str)


# --------------------------------------------------------------------------- #
# Orchestrator routing
# --------------------------------------------------------------------------- #


class RoutingDecision(BaseModel):
    incident_id: int
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
    incident_id: int
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
    preempted_from: int | None = None
    crew: list[CrewMember] = Field(default_factory=list)
    task: str = ""
    timeline: list[dict] = Field(default_factory=list, description="en_route / on_scene / returning / cooldown, simulated minutes")
    back_in_service_min: float | None = Field(default=None, description="Simulated minutes until it is available again")


# --------------------------------------------------------------------------- #
# Ingestion: duplicate detection
# --------------------------------------------------------------------------- #


class DedupVerdict(str, Enum):
    new = "new"  # nothing comparable nearby
    duplicate = "duplicate"  # another report of an incident we already have: merge, don't dispatch
    related = "related"  # a different incident close to an active one: dispatch, share scene context


class DedupDecision(BaseModel):
    incident_id: int
    verdict: DedupVerdict
    match_incident_id: int | None = None
    method: str = Field(default="minilm", description="minilm = incident_cluster_handoff.py clustering")
    cluster_size: int = 1
    minilm_verdict: DedupVerdict | None = Field(default=None, description="What MiniLM proposed")
    minilm_match_incident_id: int | None = None
    candidates: list[dict] = Field(default_factory=list, description="Incidents the deciding agent was shown")
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

    response_id: int | None = Field(default=None, description="YYYYMMDDNNNNNN, assigned by the database")
    incident_id: int
    created_at: datetime = Field(default_factory=_now)
    decided_by: str = CR_AGENT_ID
    cr_executed_by: ExecutedBy = ExecutedBy.rules
    cr_rationale: str = ""
    incident: Incident | None = Field(default=None, description="Canonical incident this response answers (snapshot)")
    triage: Triage | None = None
    routing: RoutingDecision
    dedup: DedupDecision | None = None
    related_incidents: list[int] = Field(default_factory=list)
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
    incident_id: int
    outcome: Literal["dispatched", "merged", "failed"]
    dedup: DedupDecision
    response: IncidentResponse | None = None
