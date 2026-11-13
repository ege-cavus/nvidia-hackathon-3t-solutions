"""Crew requirements: who must be on board for a vehicle to roll.

A vehicle is only dispatchable if the right number of on-shift, uncommitted people
with the right speciality can be found, preferably at the vehicle's own station.
Illustrative figures for the demo.
"""

from __future__ import annotations

CREW_REQUIREMENTS: dict[str, dict[str, int]] = {
    # fire
    "fire_engine": {"crew_commander": 1, "firefighter": 3},
    "aerial_ladder_platform": {"aerial_operator": 1, "firefighter": 1},
    "rescue_tender": {"crew_commander": 1, "rescue_technician": 2},
    "hazmat_unit": {"hazmat_technician": 2},
    "water_rescue_unit": {"water_rescue_technician": 3},
    # ambulance
    "emergency_ambulance": {"paramedic": 1, "ambulance_technician": 1},
    "rapid_response_vehicle": {"paramedic": 1},
    "critical_care_car": {"critical_care_paramedic": 1, "emergency_doctor": 1},
    "hart_team": {"hart_operative": 4},
    # police
    "patrol_car": {"response_officer": 2},
    "armed_response_vehicle": {"firearms_officer": 2},
    "dog_unit": {"dog_handler": 1},
    "public_order_van": {"public_order_sergeant": 1, "public_order_officer": 5},
    "roads_policing_unit": {"roads_policing_officer": 2},
    # city
    "drone": {"drone_pilot": 1},
    "mobile_command_unit": {"incident_commander": 1, "comms_operator": 1},
}

SPECIALITY_CODE = {
    "crew_commander": "CC", "firefighter": "FF", "aerial_operator": "AO", "rescue_technician": "RT",
    "hazmat_technician": "HZ", "water_rescue_technician": "WR", "paramedic": "PM", "ambulance_technician": "AT",
    "critical_care_paramedic": "CCP", "emergency_doctor": "DR", "hart_operative": "HO", "response_officer": "RO",
    "firearms_officer": "AFO", "dog_handler": "DH", "public_order_sergeant": "POS", "public_order_officer": "POO",
    "roads_policing_officer": "RPO", "drone_pilot": "DP", "incident_commander": "IC", "comms_operator": "CO",
}

# Deliberate staffing gaps on this shift (station -> speciality -> people missing),
# so crew shortfalls and cross-station borrowing show up in the demo.
SHIFT_GAPS: dict[str, dict[str, int]] = {
    "Sheffield Central|fire": {"crew_commander": 1},
    "Snig Hill|police": {"firearms_officer": 1},
    "Handsworth|ambulance": {"ambulance_technician": 1},
    "Ecclesfield|police": {"response_officer": 2},
}


def crew_for(resource_type: str) -> dict[str, int]:
    return CREW_REQUIREMENTS.get(resource_type, {})
