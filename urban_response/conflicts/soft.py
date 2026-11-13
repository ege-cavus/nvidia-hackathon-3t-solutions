"""Soft conflicts: disagreements in the response details.

Two passes:
1. Rules over the structured SceneConstraints (fast, explainable, always on).
2. Semantic review of free-text MissionDetails by the orchestrator, which only
   reports contradictions the rules did not already catch. That pass lives in
   the orchestrator (it needs the LLM); this module provides the rule pass.
"""

from __future__ import annotations

from itertools import permutations

from ..schemas import AgentResponse, Agency, SceneEntry, SoftConflict, SoftConflictSource


def _norm(s: str) -> str:
    return " ".join(s.lower().replace(",", " ").split())


def detect_rules(responses: list[AgentResponse], routed: list[Agency]) -> list[SoftConflict]:
    out: list[SoftConflict] = []
    by_agency = {r.agency: r for r in responses}

    def add(code, agencies, severity, detail, fix=""):
        out.append(SoftConflict(code=code, agencies=list(agencies), severity=severity, detail=detail,
                                suggested_resolution=fix, source=SoftConflictSource.rules))

    for a, b in permutations(responses, 2):
        ca, cb = a.scene_constraints, b.scene_constraints

        # A restricts the scene that B needs to enter.
        if cb.requires_scene_entry and ca.scene_entry != SceneEntry.open:
            sev = "high" if ca.scene_entry == SceneEntry.prohibited else "medium"
            add("ENTRY_RESTRICTED_BUT_REQUIRED", [a.agency, b.agency], sev,
                f"{a.agency.value} sets scene entry '{ca.scene_entry.value}' but {b.agency.value} plans to enter the scene",
                f"{a.agency.value} to brief and escort {b.agency.value} under controlled entry, or {b.agency.value} to treat at the cordon")

        # A's outer cordon is smaller than B's evacuation zone: people are let back in too close.
        if ca.cordon_outer_radius_m and cb.evacuation_radius_m and ca.cordon_outer_radius_m < cb.evacuation_radius_m:
            add("CORDON_INSIDE_EVACUATION_ZONE", [a.agency, b.agency], "medium",
                f"{a.agency.value} outer cordon {ca.cordon_outer_radius_m} m is inside {b.agency.value} evacuation radius {cb.evacuation_radius_m} m",
                f"Set outer cordon to at least {cb.evacuation_radius_m} m")

        # A stages inside B's inner cordon (hot zone).
        if ca.staging_distance_m is not None and cb.cordon_inner_radius_m and ca.staging_distance_m < cb.cordon_inner_radius_m:
            add("STAGING_INSIDE_INNER_CORDON", [a.agency, b.agency], "high",
                f"{a.agency.value} staging at {ca.staging_distance_m} m is inside {b.agency.value} inner cordon ({cb.cordon_inner_radius_m} m)",
                f"Move {a.agency.value} staging beyond {cb.cordon_inner_radius_m} m")

        # A closes a road B needs for access.
        blocked = {_norm(r) for r in ca.road_closures} & {_norm(r) for r in cb.access_routes_required}
        if blocked:
            add("ROAD_CLOSURE_BLOCKS_ACCESS", [a.agency, b.agency], "medium",
                f"{a.agency.value} closes {', '.join(sorted(blocked))}, which {b.agency.value} needs for access",
                "Keep a blue-light lane open through the closure or agree an alternative route")

    # Different staging areas: suggest a single rendezvous point.
    staging = {r.agency: _norm(r.scene_constraints.staging_area) for r in responses if r.scene_constraints.staging_area}
    if len(set(staging.values())) > 1:
        add("STAGING_MISMATCH", staging.keys(), "low",
            "Agencies proposed different staging areas: " + "; ".join(f"{a.value}: {s}" for a, s in staging.items()),
            "Agree a single multi-agency rendezvous point (RVP)")

    # Evidence preservation vs operational entry.
    police = by_agency.get(Agency.police)
    if police and police.scene_constraints.preserve_evidence:
        entering = [r.agency for r in responses if r.agency != Agency.police and r.scene_constraints.requires_scene_entry]
        if entering:
            add("EVIDENCE_VS_ENTRY", [Agency.police, *entering], "low",
                "Police need to preserve evidence while other agencies must enter the scene",
                "Agree a common approach path; life-saving takes precedence over evidence")

    # Inter-agency needs that nobody is covering.
    for r in responses:
        for need in r.scene_constraints.inter_agency_needs:
            if need.agency not in routed:
                add("UNROUTED_AGENCY_NEEDED", [r.agency, need.agency], "high",
                    f"{r.agency.value} needs {need.agency.value} for '{need.need}', but {need.agency.value} was not tasked",
                    f"Task {need.agency.value}")
            elif need.agency in by_agency and not by_agency[need.agency].resource_requests:
                add("NEED_WITHOUT_RESOURCES", [r.agency, need.agency], "medium",
                    f"{r.agency.value} needs {need.agency.value} for '{need.need}', but {need.agency.value} requested no resources",
                    f"{need.agency.value} to review its resource request")

    return _dedupe(out)


def _dedupe(conflicts: list[SoftConflict]) -> list[SoftConflict]:
    seen, out = set(), []
    for c in conflicts:
        key = (c.code, tuple(sorted(a.value for a in c.agencies)), c.detail)
        if key not in seen:
            seen.add(key)
            out.append(c)
    return out
