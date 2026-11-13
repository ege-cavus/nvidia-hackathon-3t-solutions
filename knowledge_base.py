"""Public-source context plus explicit hackathon rules, NOT operational SOPs.

No historical dataset has been ingested. These per-minute-tick priors are
synthetic starting values pending EA/STATS19 calibration (GitHub issue #5).
Flood zones describe annual risk, not incident probability per simulation tick.
"""
SOURCES = {
    "flood": "https://www.sheffield.gov.uk/public-health/flooding/strategic-flood-risk-assessment",
    "collisions": "https://www.gov.uk/government/statistical-data-sets/road-safety-open-data",
    "local_collisions": "https://sysrp.co.uk/dataportal",
    "jesip": "https://www.jesip.org.uk/joint-doctrine/supporting-decision-making/",
    "arp": "https://www.england.nhs.uk/urgent-emergency-care/improving-ambulance-services/arp/",
}
NODE_HISTORICAL_PROBABILITIES = {
    cam: {"spawn_probability": probability,
          "hazard_weights": dict(zip(("flood", "fire", "trauma"), weights)),
          "provenance": "SYNTHETIC_UNCALIBRATED", "tick_seconds": 60,
          "source_context": [SOURCES["flood"], SOURCES["collisions"]]}
    for cam, probability, weights in [
        ("CAM_01", 0.04, (0.5, 0.1, 0.4)), ("CAM_08", 0.03, (0.3, 0.1, 0.6)),
        ("CAM_10", 0.04, (0.5, 0.1, 0.4)), ("CAM_13", 0.03, (0.2, 0.2, 0.6)),
        ("CAM_21", 0.05, (0.6, 0.1, 0.3)),
    ]
}
AGENCY_KNOWLEDGE_BASE = {
    "FIRE": "Public context: JESIP shared risk assessment, coordination and situational awareness. "
            "SIMULATION RULES (not verified SYFR SOP): severity >=7 fire or flood rescue needs "
            "2 pumps OR 1 pump plus Police cordon. If current flood spawn probability >=0.05, "
            "keep at least one available pump across Eyre/Rivelin. Return then restock for 2 ticks. "
            "If requirements and reserve conflict, defer allocation rather than violate reserve.",
    "AMBULANCE": "Public context: NHS ARP Category 1 has a seven-minute mean response standard, "
                 "not a guarantee per incident. SIMULATION RULES: target arrival <=7 one-minute "
                 "ticks; transport to hospital, handover for 2 ticks, return to home base if different. "
                 "Northern General is the trauma destination in this demo. Royal Hallamshire is "
                 "retained as a team-specified base, not asserted to have an adult emergency department.",
    "POLICE": "Public context: JESIP communication, coordination, joint risk understanding. "
              "SIMULATION RULES (not verified SYP SOP): outer cordon for blocked arterial roads "
              "or severity >=6 trauma/flood; lock on scene until severity <4, then return to base.",
}


def get_agency_sop(agency: str) -> str:
    if agency not in AGENCY_KNOWLEDGE_BASE:
        raise ValueError(f"Unknown agency: {agency}")
    return (AGENCY_KNOWLEDGE_BASE[agency] + "\nSources: " + SOURCES["jesip"]
            + (" ; " + SOURCES["arp"] if agency == "AMBULANCE" else ""))
