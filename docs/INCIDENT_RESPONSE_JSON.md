# Incident and Response JSON

Canonical shapes, from a live run (Park Hill flats fire). Lists show one item.

## Incident

```json
{
  "incident_id": 20261113000001,
  "received_at": "2026-11-13T15:18:29.866785Z",
  "channel": "text",
  "caller": {
    "name": null,
    "phone": "07700 900304",
    "relationship": "resident",
    "on_scene": true
  },
  "location": {
    "address": null,
    "landmark": "Park Hill flats",
    "lat": null,
    "lon": null,
    "location_source": "caller_stated",
    "location_notes": null
  },
  "description": "Fire in our block at Park Hill, smoke in the stairwell, we have left the building.",
  "caller_reported_severity": null,
  "casualties_reported": 0,
  "people_trapped_reported": null,
  "patients": [],
  "hazards_reported": [
    "smoke"
  ],
  "weapons_mentioned": false,
  "persons_of_interest": [],
  "vehicles": []
}
```

## Response

```json
{
  "response_id": 20261113000002,
  "incident_id": 20261113000001,
  "created_at": "2026-11-13T15:20:01.119907Z",
  "decided_by": "CR",
  "cr_executed_by": "nemotron",
  "cr_rationale": "Life first: the escape stair is compromised and occupancy is unknown in a block of up to 13 storeys. We send 5 pumps, the ALP and the rescue tender (PPV for the only stair), with 3 ambulances and an RRV. The area's only critical care car and HART stay uncommitted until persons are reported. One MCU and one drone serve the whole scene. The public order van is dropped. The roads policing unit is kept because an active RTC sits on the Duke Street access route. Fire's 100/250 m cordons and 150 m debris zone apply as lead agency.",
  "incident": {
    "incident_id": 20261113000001,
    "received_at": "2026-11-13T15:18:29.866785Z",
    "channel": "text",
    "caller": {
      "name": null,
      "phone": "07700 900304",
      "relationship": "resident",
      "on_scene": true
    },
    "location": {
      "address": null,
      "landmark": "Park Hill flats",
      "lat": null,
      "lon": null,
      "location_source": "caller_stated",
      "location_notes": null
    },
    "description": "Fire in our block at Park Hill, smoke in the stairwell, we have left the building.",
    "caller_reported_severity": null,
    "casualties_reported": 0,
    "people_trapped_reported": null,
    "patients": [],
    "hazards_reported": [
      "smoke"
    ],
    "weapons_mentioned": false,
    "persons_of_interest": [],
    "vehicles": []
  },
  "triage": {
    "category": "high_rise_fire",
    "severity": 4,
    "lat": 53.38,
    "lon": -1.461,
    "location_resolved_by": "gazetteer",
    "place": "Park Hill flats",
    "casualties": 0,
    "trapped": 0,
    "weapons": false,
    "hazards": [
      "multi-storey residential block"
    ],
    "rationale": "Fire reported in a residential block at Park Hill flats, a multi-storey complex of up to 13 storeys. Smoke in the stairwell means the shared escape route may be compromised for other residents. The caller and household have evacuated. No casualties reported. Number trapped is not reported, so trapp\u2026",
    "executed_by": "qwen"
  },
  "routing": {
    "incident_id": 20261113000001,
    "agencies": [
      "fire",
      "ambulance",
      "police"
    ],
    "lead_agency": "fire",
    "rationale": "High-rise residential fire at Park Hill flats (up to 13 storeys), severity 4. Smoke in the shared stairwell means the escape route may be compromised. Other residents may still be inside, and the number trapped is unknown. Fire leads for firefighting, search and rescue, and evacuation of the block. Ambulance attends for possible smoke inhalation among residents still inside or evacuating. Caller's household has evacuated with no casualties reported. Police attend for cordon, traffic and crowd control, and to support evacuation of a large multi-occupancy block. No weapons reported. No other active or pending incidents competing for units.",
    "executed_by": "qwen"
  },
  "dedup": {
    "incident_id": 20261113000001,
    "verdict": "new",
    "match_incident_id": null,
    "method": "minilm",
    "cluster_size": 1,
    "minilm_verdict": "new",
    "minilm_match_incident_id": null,
    "candidates": [],
    "score": 0.0,
    "distance_m": null,
    "minutes_apart": null,
    "reason": "No other report in the last hour to compare with.",
    "executed_by": "minilm"
  },
  "related_incidents": [],
  "agent_responses": [
    {
      "agent_id": "A3",
      "agency": "fire",
      "incident_id": 20261113000001,
      "resource_requests": [
        {
          "resource_type": "fire_engine",
          "quantity": 4,
          "priority": 1,
          "max_eta_min": null,
          "expected_duration_min": 180,
          "justification": "High-rise residential fire. The minimum attendance is 4 pumps regardless of initial reports [pre_determined_attendance#High-rise residential fire]. Severity 4 also requires 4 pumps on its own [pre_determined_attendance#Dwelling fire]. Crews are needed for firefighting from a bridgehead, BA entry control, stairwell search and rescue, and water supply."
        }
      ],
      "scene_constraints": {
        "scene_entry": "prohibited",
        "cordon_inner_radius_m": 100,
        "cordon_outer_radius_m": 250,
        "evacuation_radius_m": 150,
        "staging_area": "South Street / Duke Street junction, south-west of the Park Hill complex. RVP for all agencies, outside the inner cordon.",
        "staging_distance_m": 200,
        "requires_scene_entry": true,
        "road_closures": [
          "South Street (Park Hill frontage)"
        ],
        "access_routes_required": [
          "Duke Street"
        ],
        "preserve_evidence": false,
        "inter_agency_needs": [
          {
            "agency": "police",
            "need": "Outer cordon at 250 m and traffic control on South Street, Duke Street and Talbot Street. Keep pump and ALP access routes clear."
          }
        ]
      },
      "mission_details": {
        "summary": "A fire is reported in a residential block at Park Hill flats (up to 13 storeys) with smoke in the stairwell. The caller's household has evacuated, but other occupants may still be inside and the shared escape route is compromised. High-rise attendance is mobilised: 5 pumps, an ALP, a rescue tender, a command unit and a drone. The incident is run under Level 2 command.",
        "tactical_plan": [
          "First pump: confirm the fire floor and location, and establish entry control at the building entrance."
        ],
        "hazards_identified": [
          "Smoke-logged common stairwell compromising the single escape route"
        ],
        "safety_notes": [
          "Other agencies must not enter while fire is active; entry is prohibited and only BA crews may cross the inner cordon [scene_safety_and_cordons#Inner cordon and entry control]."
        ],
        "specialist_metadata": {
          "incident_type": "high_rise_residential_fire",
          "command_level": "Level 2",
          "pda_applied": "High-rise residential fire (4 pumps + 1 ALP) plus 1 pump for persons possibly involved",
          "total_pumps": 5,
          "evacuation_strategy": "To be decided by IC on arrival. Escape route is compromised, so lean towards simultaneous evacuation of affected floors.",
          "bridgehead": "Two floors below fire floor (fire floor TBC)",
          "building_height_storeys": 13,
          "collapse_zone_m": "Not yet set. About 60 m (1.5 x height) if damage extends beyond one floor.",
          "persons_reported": "Caller household evacuated; others unknown",
          "max_first_pump_arrival_min": 8,
          "max_second_pump_arrival_min": 10
        }
      },
      "kb_citations": [
        "pre_determined_attendance#High-rise residential fire"
      ],
      "confidence": 0.85,
      "executed_by": "nemotron",
      "latency_ms": 31023
    }
  ],
  "requested_total": {
    "fire_engine": 5,
    "aerial_ladder_platform": 1,
    "rescue_tender": 1,
    "mobile_command_unit": 2,
    "drone": 2,
    "emergency_ambulance": 2,
    "rapid_response_vehicle": 1,
    "critical_care_car": 1,
    "hart_team": 1,
    "patrol_car": 4,
    "roads_policing_unit": 1,
    "public_order_van": 1
  },
  "manifest": [
    {
      "resource_type": "fire_engine",
      "quantity": 5,
      "for_agency": "fire",
      "priority": 1,
      "reason": "High-rise PDA of 4 plus 1 for possible occupants on a smoke-logged stair"
    }
  ],
  "manifest_adjustments": [],
  "hard_conflicts": [],
  "hard_resolutions": [],
  "soft_conflicts": [
    {
      "code": "ENTRY_RESTRICTED_BUT_REQUIRED",
      "agencies": [
        "ambulance",
        "fire"
      ],
      "severity": "high",
      "detail": "ambulance sets scene entry 'prohibited' but fire plans to enter the scene",
      "suggested_resolution": "ambulance to brief and escort fire under controlled entry, or fire to treat at the cordon",
      "source": "rules"
    }
  ],
  "allocations": [
    {
      "unit_id": "FIR-SC-PMP-2",
      "callsign": "SY-F SCPMP2",
      "resource_type": "fire_engine",
      "agency": "fire",
      "station": "Sheffield Central",
      "eta_min": 2.7,
      "requested_by": "fire",
      "preempted_from": null,
      "crew": [
        {
          "person_id": "FIR-SC-CC02",
          "speciality": "crew_commander",
          "station": "Sheffield Central"
        }
      ],
      "task": "High-rise PDA of 4 plus 1 for possible occupants on a smoke-logged stair"
    }
  ],
  "unmet": [],
  "directives": [
    {
      "agency": "all",
      "action": "RVP South Street west side, upwind, outside 250 m; approach via Sheaf St/Park Square and avoid the Duke St/Talbot St RTC.",
      "priority": "immediate"
    }
  ],
  "status": "dispatched",
  "evaluation": {
    "score": 94,
    "grade": "A",
    "verdict": "resolved",
    "metrics": {
      "requested_units": 22,
      "manifest_units": 18,
      "trimmed_units": 4,
      "dispatched_units": 18,
      "personnel_dispatched": 45,
      "fill_rate": 1.0,
      "essential_fill_rate": 1.0,
      "essential_gap_units": 0,
      "arrival": {
        "fire": {
          "first_arrival_min": 2.7,
          "target_min": 8,
          "met": true
        },
        "ambulance": {
          "first_arrival_min": 2.7,
          "target_min": 7,
          "met": true
        },
        "police": {
          "first_arrival_min": 2.9,
          "target_min": 10,
          "met": true
        },
        "shared": {
          "first_arrival_min": 2.6,
          "target_min": 15,
          "met": true
        }
      },
      "arrival_targets_met": 1.0,
      "crew_complete_rate": 1.0,
      "hard_conflicts": 0,
      "resolved_by_cr": 0,
      "rule_fallbacks": 0,
      "soft_conflicts": 8,
      "mutual_aid_requests": 0,
      "preempted_units": 0,
      "floors_applied": 0
    },
    "findings": [
      "Every requested essential resource was sent, and first units from fire, ambulance and police arrived within 3 minutes, well inside their targets."
    ],
    "headline": "Fire crews rush to Park Hill flats as smoke fills stairwell",
    "dek": "Residents fled a fire at Park Hill flats; five fire engines, a high-reach ladder, three ambulances and police were sent, with the first crews arriving in under three minutes.",
    "executed_by": "qwen"
  }
}
```
