from urban_response.conflicts.soft import detect_rules
from urban_response.schemas import (
    AgentResponse,
    Agency,
    ExecutedBy,
    InterAgencyNeed,
    MissionDetails,
    SceneConstraints,
    SceneEntry,
)


def resp(agency: Agency, **sc) -> AgentResponse:
    return AgentResponse(
        agent_id="X", agency=agency, incident_id=1, resource_requests=[],
        scene_constraints=SceneConstraints(**sc), mission_details=MissionDetails(summary=""), executed_by=ExecutedBy.mock,
    )


def codes(conflicts):
    return {c.code for c in conflicts}


def test_entry_prohibited_but_required():
    c = detect_rules([resp(Agency.police, scene_entry=SceneEntry.prohibited), resp(Agency.ambulance, requires_scene_entry=True)],
                     [Agency.police, Agency.ambulance])
    hit = [x for x in c if x.code == "ENTRY_RESTRICTED_BUT_REQUIRED"]
    assert hit and hit[0].severity == "high"


def test_cordon_inside_evacuation_and_staging_in_hot_zone():
    c = detect_rules(
        [resp(Agency.police, cordon_outer_radius_m=100, staging_distance_m=150),
         resp(Agency.fire, evacuation_radius_m=400, cordon_inner_radius_m=200)],
        [Agency.police, Agency.fire],
    )
    assert {"CORDON_INSIDE_EVACUATION_ZONE", "STAGING_INSIDE_INNER_CORDON"} <= codes(c)


def test_road_closure_blocks_access_case_insensitive():
    c = detect_rules([resp(Agency.fire, road_closures=["Hanover Way"]), resp(Agency.ambulance, access_routes_required=["hanover  way"])],
                     [Agency.fire, Agency.ambulance])
    assert "ROAD_CLOSURE_BLOCKS_ACCESS" in codes(c)


def test_unrouted_agency_needed():
    c = detect_rules([resp(Agency.ambulance, inter_agency_needs=[InterAgencyNeed(agency=Agency.fire, need="extrication")])],
                     [Agency.ambulance])
    assert "UNROUTED_AGENCY_NEEDED" in codes(c)


def test_consistent_plan_has_no_conflicts():
    c = detect_rules(
        [resp(Agency.police, cordon_outer_radius_m=500, staging_area="RVP A", staging_distance_m=550),
         resp(Agency.fire, evacuation_radius_m=400, cordon_inner_radius_m=200, staging_area="rvp a", staging_distance_m=300)],
        [Agency.police, Agency.fire],
    )
    assert c == []
