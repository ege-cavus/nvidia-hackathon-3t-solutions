"""Historical response-time context, separate from authored game outcomes."""
import json
from pathlib import Path

_SOURCE = Path(__file__).resolve().parent / "data/reference/syfr_response_standards_2023_24.json"
_REFERENCE = json.loads(_SOURCE.read_text(encoding="utf-8"))


def fire_response_reference(community_risk=None, incident_category_risk=None):
    """Return a reference target only when both risk classifications are supplied.

    Never infer community risk from a station/camera or convert model severity.
    Supplying classifications selects a historical target; it does not verify
    those classifications or establish operational response-standard compliance.
    """
    matrix = _REFERENCE["target_seconds_by_community_and_incident_risk"]
    for label in (community_risk, incident_category_risk):
        if label is not None and label not in matrix:
            raise ValueError("Risk classification must be HIGH, MEDIUM, LOW or unset")
    assigned = community_risk is not None and incident_category_risk is not None
    return {
        "source": _REFERENCE["source"], "reporting_period": _REFERENCE["reporting_period"],
        "community_risk": community_risk, "incident_category_risk": incident_category_risk,
        "classification_status": "SUPPLIED_NOT_VERIFIED" if assigned else "UNASSIGNED",
        "target_seconds": matrix[community_risk][incident_category_risk] if assigned else None,
        "measurement_scope": _REFERENCE["measurement_scope"],
        "compliance_status": "NOT_ASSESSED",
        "note": "Requires explicit risk classification and measured turnout plus travel; does not control incident resolution or resource release.",
    }
