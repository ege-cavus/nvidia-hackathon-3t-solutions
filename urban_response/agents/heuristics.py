"""Deterministic stand-ins for the models.

Used when LLM_MODE=mock (develop and demo without a model server) and as the
fallback when a live model call fails. Outputs are tagged executed_by=mock or
executed_by=fallback so they are never mistaken for model output.

They loosely follow the pre-determined attendance tables in knowledge_base/.
"""

from __future__ import annotations

import math
import re

from pydantic import BaseModel, Field

from ..schemas import (
    Agency,
    Incident,
    InterAgencyNeed,
    MissionDetails,
    RequestPriority,
    ResourceRequest,
    SceneConstraints,
    SceneEntry,
)

FIRE_CATS = {"structure_fire", "high_rise_fire", "vehicle_fire", "wildfire"}
HAZ_CATS = {"hazmat", "gas_leak", "chemical_spill"}
CRIME_CATS = {"assault", "stabbing", "firearms", "robbery", "public_disorder", "terrorism"}
MEDICAL_CATS = {"medical", "cardiac_arrest", "overdose", "fall"}

E, I, D = RequestPriority.essential, RequestPriority.important, RequestPriority.desirable


class AgentDraft(BaseModel):
    """What a specialist model is asked to produce."""

    resource_requests: list[ResourceRequest]
    scene_constraints: SceneConstraints
    mission_details: MissionDetails
    kb_citations: list[str] = Field(default_factory=list)
    confidence: float = Field(default=0.7, ge=0, le=1)


def main_road(incident: Incident) -> str:
    addr = incident.location.address.split(",")[0].strip()
    return re.sub(r"^\d+[a-zA-Z]?\s+", "", addr) or incident.location.district or "scene approach road"


def route(incident: Incident) -> tuple[list[Agency], Agency, str]:
    cat = incident.category
    agencies: set[Agency] = set()
    if cat in FIRE_CATS | HAZ_CATS | {"flood", "rescue"} or incident.people_trapped:
        agencies.add(Agency.fire)
    if incident.casualties_reported or incident.people_trapped or cat in MEDICAL_CATS | {"rtc", "stabbing", "firearms"}:
        agencies.add(Agency.ambulance)
    if cat in CRIME_CATS | {"rtc"} or incident.weapons_involved or incident.severity >= 3:
        agencies.add(Agency.police)
    if not agencies:
        agencies.add(Agency.police)

    if cat in CRIME_CATS or incident.weapons_involved:
        lead = Agency.police
    elif cat in FIRE_CATS | HAZ_CATS | {"flood", "rescue"} or incident.people_trapped:
        lead = Agency.fire
    elif Agency.ambulance in agencies:
        lead = Agency.ambulance
    else:
        lead = next(iter(agencies))
    order = [a for a in (Agency.police, Agency.ambulance, Agency.fire) if a in agencies]
    return order, lead, f"Rule-based routing for category '{cat}', severity {incident.severity}"


def fire(incident: Incident) -> AgentDraft:
    cat, sev, road = incident.category, incident.severity, main_road(incident)
    hazmat = cat in HAZ_CATS or any("chem" in h or "gas" in h for h in incident.hazards)
    reqs: list[ResourceRequest] = []
    plan: list[str] = []

    if cat in FIRE_CATS or cat in HAZ_CATS:
        pumps = 2 + (sev >= 3) + 2 * (sev >= 4)
        if cat == "high_rise_fire":
            pumps = max(pumps, 4)
            reqs.append(ResourceRequest(resource_type="aerial_ladder_platform", quantity=1, priority=E, max_eta_min=15,
                                        expected_duration_min=180, justification="Upper-floor rescue and external firefighting"))
            plan.append("Establish bridgehead two floors below fire floor; aerial for external rescue")
        reqs.insert(0, ResourceRequest(resource_type="fire_engine", quantity=pumps, priority=E, max_eta_min=10,
                                       expected_duration_min=120 + 30 * sev, justification="Pre-determined attendance"))
        plan.append("Offensive internal attack with BA teams; search and rescue priority")
    if cat == "rtc" and incident.people_trapped:
        reqs.append(ResourceRequest(resource_type="fire_engine", quantity=2, priority=E, max_eta_min=12, expected_duration_min=90))
        plan.append("Stabilise vehicles; hydraulic extrication")
    if incident.people_trapped:
        reqs.append(ResourceRequest(resource_type="rescue_tender", quantity=1, priority=E, max_eta_min=15,
                                    expected_duration_min=120, justification=f"{incident.people_trapped} person(s) trapped"))
    if hazmat:
        reqs.append(ResourceRequest(resource_type="hazmat_unit", quantity=1, priority=E, expected_duration_min=240,
                                    justification="Detection, identification, monitoring; decontamination"))
        plan.append("Identify substance (DIM); set up decontamination at inner cordon")
    if cat == "flood":
        reqs += [ResourceRequest(resource_type="water_rescue_unit", quantity=1, priority=E, expected_duration_min=180),
                 ResourceRequest(resource_type="fire_engine", quantity=1, priority=I, expected_duration_min=180)]
        plan.append("Swift-water rescue teams; no wading above knee height")

    inner = 200 if hazmat else (100 if cat == "high_rise_fire" or sev >= 4 else 50)
    evac = 400 if hazmat else (150 if cat == "high_rise_fire" else (100 if sev >= 4 else 0))
    needs = []
    if sev >= 3:
        needs.append(InterAgencyNeed(agency=Agency.police, need="outer cordon and traffic management"))
    if incident.casualties_reported or incident.people_trapped:
        needs.append(InterAgencyNeed(agency=Agency.ambulance, need="casualty care at the cordon"))

    return AgentDraft(
        resource_requests=reqs,
        scene_constraints=SceneConstraints(
            scene_entry=SceneEntry.prohibited if (hazmat or sev >= 3) else SceneEntry.controlled,
            cordon_inner_radius_m=inner,
            cordon_outer_radius_m=inner * 2,
            evacuation_radius_m=evac,
            staging_area=f"Upwind RVP on {road}",
            staging_distance_m=inner + 100,
            requires_scene_entry=True,
            road_closures=[road] if sev >= 3 else [],
            inter_agency_needs=needs,
        ),
        mission_details=MissionDetails(
            summary=f"Fire & rescue response to {cat.replace('_', ' ')} at {incident.location.address}",
            tactical_plan=plan or ["Assess and make safe"],
            hazards_identified=incident.hazards + (["structural collapse"] if sev >= 4 else []),
            safety_notes=["All crews in BA inside inner cordon", "Entry control point at inner cordon"],
            specialist_metadata={"incident_command_level": "Level 2" if sev >= 4 else "Level 1",
                                 "water_supply": "hydrant + relay if > 4 pumps"},
        ),
    )


def ambulance(incident: Incident) -> AgentDraft:
    cat, sev, road = incident.category, incident.severity, main_road(incident)
    c = max(incident.casualties_reported, incident.people_trapped, 1 if cat in MEDICAL_CATS else 0)
    mci = c >= 5
    hazardous = cat in HAZ_CATS or cat == "firearms" or incident.weapons_involved
    reqs: list[ResourceRequest] = []
    if c:
        n = c if c <= 4 else 4 + math.ceil((c - 4) / 2)
        reqs.append(ResourceRequest(resource_type="emergency_ambulance", quantity=n, priority=E, max_eta_min=18 if sev >= 3 else None,
                                    expected_duration_min=90, justification=f"{c} casualty(ies)"))
    if sev >= 3 or cat == "cardiac_arrest":
        reqs.append(ResourceRequest(resource_type="rapid_response_vehicle", quantity=1, priority=E if cat == "cardiac_arrest" else I,
                                    max_eta_min=8, expected_duration_min=60, justification="Category 1 response target"))
    if incident.people_trapped or sev >= 4:
        reqs.append(ResourceRequest(resource_type="critical_care_car", quantity=1, priority=I, max_eta_min=20,
                                    expected_duration_min=120, justification="Pre-hospital critical care for trapped / major trauma"))
    if mci or hazardous:
        reqs.append(ResourceRequest(resource_type="hart_team", quantity=1, priority=E, expected_duration_min=180,
                                    justification="Hazardous area / mass casualty capability"))

    needs = []
    if incident.people_trapped:
        needs.append(InterAgencyNeed(agency=Agency.fire, need="extrication of trapped casualties"))
    if incident.weapons_involved or cat in CRIME_CATS:
        needs.append(InterAgencyNeed(agency=Agency.police, need="scene declared safe before crews approach"))

    triage = "P1" if sev >= 4 else ("P2" if sev >= 2 else "P3")
    return AgentDraft(
        resource_requests=reqs,
        scene_constraints=SceneConstraints(
            scene_entry=SceneEntry.open,
            staging_area=f"Ambulance loading point on {road}",
            staging_distance_m=50,
            requires_scene_entry=bool(c) and not (cat in HAZ_CATS),
            access_routes_required=[road],
            inter_agency_needs=needs,
        ),
        mission_details=MissionDetails(
            summary=f"Ambulance response: {c} casualty(ies), {'major incident (METHANE)' if mci else 'standard'}",
            tactical_plan=(["Declare major incident; send METHANE", "Establish casualty clearing station"] if mci else [])
            + ["Triage on arrival (sieve then sort)", "Treat and convey to receiving hospital"],
            hazards_identified=incident.hazards,
            safety_notes=["Do not enter hot zone without HART"] if hazardous else [],
            specialist_metadata={"triage_category": triage, "response_category": "C1" if sev >= 4 or cat == "cardiac_arrest" else "C2",
                                 "major_incident": mci, "expected_casualties": c},
        ),
    )


def police(incident: Incident) -> AgentDraft:
    cat, sev, road = incident.category, incident.severity, main_road(incident)
    armed = cat == "firearms" or incident.weapons_involved
    crime = cat in CRIME_CATS or armed
    reqs = [ResourceRequest(resource_type="patrol_car", quantity=2 + 2 * (sev >= 3) + 2 * (sev >= 4), priority=E,
                            expected_duration_min=120 if crime else 60, justification="Cordons, scene management, witnesses")]
    plan = ["Establish cordons and log entry/exit"]
    if armed:
        reqs.append(ResourceRequest(resource_type="armed_response_vehicle", quantity=2, priority=E, max_eta_min=15,
                                    expected_duration_min=180, justification="Weapons reported"))
        reqs.append(ResourceRequest(resource_type="dog_unit", quantity=1, priority=D, expected_duration_min=120))
        plan.insert(0, "ARV to locate and contain threat; scene not safe until declared by Tactical Firearms Commander")
    if cat == "public_disorder":
        reqs.append(ResourceRequest(resource_type="public_order_van", quantity=2, priority=E, expected_duration_min=240))
    if cat == "rtc":
        reqs.append(ResourceRequest(resource_type="roads_policing_unit", quantity=1, priority=E, expected_duration_min=150,
                                    justification="Collision investigation and road closure"))
    if sev >= 4:
        reqs.append(ResourceRequest(resource_type="drone", quantity=1, priority=D, expected_duration_min=120,
                                    justification="Overhead situational awareness"))
    if sev >= 5:
        reqs.append(ResourceRequest(resource_type="mobile_command_unit", quantity=1, priority=I, expected_duration_min=360))

    outer = 300 if armed else (100 if sev >= 3 else 50)
    needs = [InterAgencyNeed(agency=Agency.ambulance, need="casualty treatment")] if incident.casualties_reported else []
    return AgentDraft(
        resource_requests=reqs,
        scene_constraints=SceneConstraints(
            scene_entry=SceneEntry.prohibited if armed else (SceneEntry.controlled if crime else SceneEntry.open),
            cordon_inner_radius_m=100 if armed else (25 if crime else 0),
            cordon_outer_radius_m=outer,
            staging_area=f"Police RVP at {incident.location.district or road} junction",
            staging_distance_m=outer + 50,
            road_closures=[road] if (sev >= 3 or cat == "rtc") else [],
            preserve_evidence=crime or cat == "rtc",
            inter_agency_needs=needs,
        ),
        mission_details=MissionDetails(
            summary=f"Police response to {cat.replace('_', ' ')}, {'armed' if armed else 'unarmed'}",
            tactical_plan=plan + (["Identify and arrest suspects; secure evidence"] if crime else ["Traffic management"]),
            hazards_identified=incident.hazards,
            safety_notes=["Unarmed officers hold outer cordon only"] if armed else [],
            specialist_metadata={"crime_scene": crime, "command": "Gold/Silver/Bronze" if sev >= 4 else "Bronze",
                                 "threat_level": "high" if armed else "low"},
        ),
    )


HEURISTICS = {Agency.police: police, Agency.ambulance: ambulance, Agency.fire: fire}
