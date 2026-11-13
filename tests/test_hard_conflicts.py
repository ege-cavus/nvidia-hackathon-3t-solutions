from urban_response.conflicts import hard
from urban_response.schemas import (
    AgentResponse,
    Agency,
    ExecutedBy,
    HardConflictKind,
    MissionDetails,
    RequestPriority,
    ResolutionAction,
    ResourceRequest,
    SceneConstraints,
)

from .conftest import make_incident


def resp(agency: Agency, *reqs: ResourceRequest) -> AgentResponse:
    return AgentResponse(
        agent_id="X", agency=agency, incident_id=1, resource_requests=list(reqs),
        scene_constraints=SceneConstraints(), mission_details=MissionDetails(summary=""), executed_by=ExecutedBy.mock,
    )


def req(rtype: str, n: int, **kw) -> ResourceRequest:
    return ResourceRequest(resource_type=rtype, quantity=n, **kw)


def kinds(conflicts):
    return {(c.kind, c.resource_type) for c in conflicts}


def test_no_conflict_when_resources_suffice(db):
    inc = make_incident()
    assert hard.detect(inc, [resp(Agency.fire, req("fire_engine", 2))], db) == []


def test_shortfall_against_resource_db(db):
    inc = make_incident()
    c = hard.detect(inc, [resp(Agency.fire, req("aerial_ladder_platform", 3))], db)
    assert kinds(c) == {(HardConflictKind.shortfall, "aerial_ladder_platform")}
    assert c[0].requested == 3 and c[0].available == 2


def test_contention_between_agencies_on_shared_pool(db):
    inc = make_incident()
    c = hard.detect(inc, [resp(Agency.police, req("drone", 2)), resp(Agency.fire, req("drone", 1))], db)
    assert kinds(c) == {(HardConflictKind.contention, "drone")}
    assert set(c[0].agencies) == {Agency.police, Agency.fire}


def test_ownership_and_unknown(db):
    inc = make_incident()
    c = hard.detect(inc, [resp(Agency.police, req("fire_engine", 1), req("tank", 1))], db)
    assert (HardConflictKind.ownership, "fire_engine") in kinds(c)
    assert (HardConflictKind.unknown_resource, "tank") in kinds(c)


def test_eta_unmet(db):
    inc = make_incident()
    c = hard.detect(inc, [resp(Agency.fire, req("rescue_tender", 1, max_eta_min=1))], db)
    assert kinds(c) == {(HardConflictKind.eta_unmet, "rescue_tender")}


def test_allocator_priority_and_lead(db):
    """Two agencies contend for 2 drones: essential beats desirable regardless of agency."""
    inc = make_incident()
    responses = [
        resp(Agency.police, req("drone", 2, priority=RequestPriority.desirable)),
        resp(Agency.fire, req("drone", 1, priority=RequestPriority.essential)),
    ]
    allocs, unmet = hard.allocate(inc, responses, [], Agency.police, db)
    by = [a.requested_by for a in allocs]
    assert by.count(Agency.fire) == 1 and by.count(Agency.police) == 1
    assert unmet == [{"resource_type": "drone", "quantity": 1, "requested_by": "police", "reason": "insufficient resources"}]


def test_allocator_never_double_books_and_respects_log(db):
    inc = make_incident()
    a1, _ = hard.allocate(inc, [resp(Agency.fire, req("fire_engine", 3))], [], Agency.fire, db)
    ids = [a.unit_id for a in a1]
    assert len(set(ids)) == 3


def test_preemption_from_lower_severity_incident(db):
    from urban_response.schemas import IncidentResponse, RoutingDecision

    low = make_incident(severity=2)
    responses = [resp(Agency.fire, req("aerial_ladder_platform", 2))]
    allocs, _ = hard.allocate(low, responses, [], Agency.fire, db)
    routing = RoutingDecision(incident_id=low.incident_id, agencies=[Agency.fire], lead_agency=Agency.fire,
                              rationale="", executed_by=ExecutedBy.rules)
    db.insert_incident(low.incident, low.triage)
    db.record_response(low, IncidentResponse(incident_id=low.incident_id, routing=routing, agent_responses=responses,
                                             hard_conflicts=[], hard_resolutions=[], soft_conflicts=[], allocations=allocs))

    high = make_incident(severity=5)
    high_resp = [resp(Agency.fire, req("aerial_ladder_platform", 1))]
    conflicts = hard.detect(high, high_resp, db)
    assert conflicts[0].kind == HardConflictKind.shortfall and len(conflicts[0].preemptible) == 2

    res = hard.default_resolution(conflicts[0], high, db)
    assert res.action == ResolutionAction.preempt
    allocs2, unmet = hard.allocate(high, high_resp, [res], Agency.fire, db)
    assert len(allocs2) == 1 and allocs2[0].preempted_from == low.incident_id and not unmet


def test_substitute_respects_same_plan_demand(db):
    """A substitute already fully used by this plan is not a valid option."""
    inc = make_incident()
    rrv_total = len(db.available_units("rapid_response_vehicle", 0, 0))
    responses = [resp(Agency.ambulance, req("critical_care_car", 2), req("rapid_response_vehicle", rrv_total))]
    conflict = hard.detect(inc, responses, db)[0]
    demand = hard.demand_totals(responses)
    assert ResolutionAction.substitute not in hard.resolution_options(conflict, db, demand)
    assert ResolutionAction.substitute in hard.resolution_options(conflict, db)


def test_validate_rejects_invalid_llm_choice(db):
    inc = make_incident(severity=5)
    conflict = hard.detect(inc, [resp(Agency.fire, req("aerial_ladder_platform", 5))], db)[0]
    from urban_response.schemas import HardResolution

    bogus = HardResolution(resource_type="aerial_ladder_platform", action=ResolutionAction.preempt,
                           preempt_unit_ids=["NOT-A-UNIT"], rationale="", executed_by=ExecutedBy.qwen)
    assert hard.validate_resolution(bogus, conflict, db) is None
