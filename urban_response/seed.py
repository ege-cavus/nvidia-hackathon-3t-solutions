"""Seed the Resource DB with Sheffield emergency resources.

Station names and coordinates are approximate and unit counts are illustrative,
for demo use only. Replace with authoritative data before any real use.
"""

from __future__ import annotations

from .db import Database
from .resources import CREW_REQUIREMENTS, SHIFT_GAPS, SPECIALITY_CODE

# (agency, station, lat, lon, {resource_type: count})
STATIONS: list[tuple[str, str, float, float, dict[str, int]]] = [
    # South Yorkshire Fire & Rescue, Sheffield stations
    ("fire", "Sheffield Central", 53.3858, -1.4590, {"fire_engine": 3, "aerial_ladder_platform": 1, "hazmat_unit": 1}),
    ("fire", "Elm Lane", 53.4235, -1.4590, {"fire_engine": 2, "rescue_tender": 1}),
    ("fire", "Mansfield Road", 53.3605, -1.4335, {"fire_engine": 2}),
    ("fire", "Lowedges", 53.3240, -1.4900, {"fire_engine": 2, "water_rescue_unit": 1}),
    ("fire", "Parkway", 53.3880, -1.4170, {"fire_engine": 2, "rescue_tender": 1, "aerial_ladder_platform": 1}),
    ("fire", "Stannington", 53.3980, -1.5300, {"fire_engine": 1}),
    # Yorkshire Ambulance Service, Sheffield area
    ("ambulance", "Northern General Hub", 53.4100, -1.4600, {"emergency_ambulance": 5, "rapid_response_vehicle": 2, "hart_team": 1}),
    ("ambulance", "Sheffield Central", 53.3770, -1.4700, {"emergency_ambulance": 4, "rapid_response_vehicle": 1, "critical_care_car": 1}),
    ("ambulance", "Middlewood", 53.4110, -1.5050, {"emergency_ambulance": 3, "rapid_response_vehicle": 1}),
    ("ambulance", "Handsworth", 53.3700, -1.3850, {"emergency_ambulance": 3, "rapid_response_vehicle": 1}),
    # South Yorkshire Police, Sheffield
    ("police", "Snig Hill", 53.3851, -1.4686, {"patrol_car": 6, "public_order_van": 2, "armed_response_vehicle": 2}),
    ("police", "Attercliffe", 53.3950, -1.4280, {"patrol_car": 5, "roads_policing_unit": 2, "dog_unit": 1}),
    ("police", "Ecclesfield", 53.4430, -1.4700, {"patrol_car": 4, "dog_unit": 1}),
    ("police", "Woodseats", 53.3400, -1.4800, {"patrol_car": 4, "public_order_van": 1, "armed_response_vehicle": 1, "roads_policing_unit": 1}),
    # City-owned shared assets
    ("shared", "Sheffield City Resilience Hub", 53.3811, -1.4701, {"drone": 2, "mobile_command_unit": 1}),
]

HOSPITALS = [
    # name, lat, lon, major_trauma, paediatric, ed_capacity
    ("Northern General Hospital", 53.4096, -1.4570, 1, 0, 40),
    ("Royal Hallamshire Hospital", 53.3779, -1.4892, 0, 0, 25),
    ("Sheffield Children's Hospital", 53.3802, -1.4878, 0, 1, 15),
]

CALLSIGN_PREFIX = {"fire": "SY-F", "ambulance": "YAS", "police": "SYP", "shared": "SCC"}
TYPE_CODE = {
    "fire_engine": "PMP",
    "aerial_ladder_platform": "ALP",
    "rescue_tender": "RT",
    "hazmat_unit": "HZ",
    "water_rescue_unit": "WR",
    "emergency_ambulance": "EA",
    "rapid_response_vehicle": "RRV",
    "hart_team": "HART",
    "critical_care_car": "CCC",
    "patrol_car": "PC",
    "public_order_van": "POV",
    "armed_response_vehicle": "ARV",
    "roads_policing_unit": "RPU",
    "dog_unit": "DOG",
    "drone": "UAV",
    "mobile_command_unit": "MCU",
}


def seed(db: Database, force: bool = False) -> int:
    if not db.is_empty() and not force:
        return 0
    n = 0
    with db.tx() as c:
        if force:
            for table in ("inventory_ledger", "incident_log", "crew_assignments", "allocations", "conflicts",
                          "agent_responses", "responses", "incidents", "personnel"):
                c.execute(f"DELETE FROM {table}")
            c.execute("DELETE FROM units")
            c.execute("DELETE FROM hospitals")
        for agency, station, lat, lon, units in STATIONS:
            st_code = "".join(w[0] for w in station.split()).upper()
            staff: dict[str, int] = {}
            for rtype, count in units.items():
                for spec, k in CREW_REQUIREMENTS.get(rtype, {}).items():
                    staff[spec] = staff.get(spec, 0) + k * count
                for i in range(1, count + 1):
                    code = TYPE_CODE[rtype]
                    unit_id = f"{agency[:3].upper()}-{st_code}-{code}-{i}"
                    callsign = f"{CALLSIGN_PREFIX[agency]} {st_code}{code}{i}"
                    c.execute(
                        "INSERT INTO units (unit_id, callsign, agency, resource_type, station, lat, lon) VALUES (?,?,?,?,?,?,?)",
                        (unit_id, callsign, agency, rtype, station, lat, lon),
                    )
                    n += 1
            # One full crew per vehicle on this shift, minus the deliberate gaps.
            for spec, k in staff.items():
                k -= SHIFT_GAPS.get(f"{station}|{agency}", {}).get(spec, 0)
                for i in range(1, k + 1):
                    c.execute(
                        "INSERT INTO personnel (person_id, agency, speciality, station, lat, lon) VALUES (?,?,?,?,?,?)",
                        (f"{agency[:3].upper()}-{st_code}-{SPECIALITY_CODE[spec]}{i:02d}", agency, spec, station, lat, lon),
                    )
        c.executemany(
            "INSERT INTO hospitals (name, lat, lon, major_trauma, paediatric, ed_capacity) VALUES (?,?,?,?,?,?)",
            HOSPITALS,
        )
    return n
