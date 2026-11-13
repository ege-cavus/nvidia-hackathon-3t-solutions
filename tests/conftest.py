import hashlib
import re

import numpy as np
import pytest

from urban_response import dedup
from urban_response.config import Settings
from urban_response.db import Database
from urban_response.schemas import Case, Incident, IncidentLocation, Triage
from urban_response.seed import seed

_counter = iter(range(1, 10_000_000))


def bag_of_words(texts: list[str]) -> np.ndarray:
    """Deterministic offline stand-in for MiniLM: hashed bag of words, unit-normalised."""
    out = np.zeros((len(texts), 256))
    for i, t in enumerate(texts):
        for w in re.findall(r"[a-z]{3,}", t.lower()):
            out[i, int(hashlib.md5(w.encode()).hexdigest(), 16) % 256] += 1.0
    norms = np.linalg.norm(out, axis=1, keepdims=True)
    return out / np.where(norms == 0, 1, norms)


@pytest.fixture(autouse=True)
def offline_embedder():
    dedup.set_embedder(bag_of_words)
    yield
    dedup.set_embedder(None)


@pytest.fixture
def db():
    d = Database(":memory:")
    seed(d)
    yield d
    d.close()


@pytest.fixture
def settings():
    return Settings(llm_mode="mock")


DESCRIPTIONS = {
    "rtc": "Two cars crashed at the junction, collision blocking the road.",
    "structure_fire": "House on fire, flames coming from the roof.",
    "high_rise_fire": "Fire on the 9th floor of a tower block, smoke spreading upwards.",
    "vehicle_fire": "Car on fire in the car park.",
    "firearms": "Shots fired outside a bar, a man with a gun ran off.",
    "hazmat": "Chemical tanker leaking toxic fumes.",
    "medical": "Man unconscious on the pavement.",
}


def make_report(category: str = "rtc", lat: float = 53.3811, lon: float = -1.4701, address: str = "1 Test Street",
                **fields) -> Incident:
    """A canonical incident whose description the triage rules classify as `category`."""
    fields.setdefault("description", DESCRIPTIONS[category] + f" Report {next(_counter)}.")
    return Incident(location=IncidentLocation(address=address, lat=lat, lon=lon, location_source="caller_gps"), **fields)


def make_incident(category: str = "structure_fire", severity: int = 3, casualties_reported: int = 0,
                  people_trapped: int = 0, weapons_involved: bool = False, hazards: list[str] | None = None,
                  lat: float = 53.3811, lon: float = -1.4701, incident_id: int | None = None) -> Case:
    """A Case with a fixed triage, for unit tests of the accounting layer (bypasses intake)."""
    inc = Incident(incident_id=incident_id or 20261113_000000 + next(_counter),
                   location=IncidentLocation(address="1 Test Street, City Centre", lat=lat, lon=lon),
                   description=DESCRIPTIONS.get(category, "test"))
    tri = Triage(category=category, severity=severity, lat=lat, lon=lon, place="City Centre",
                 casualties=casualties_reported, trapped=people_trapped, weapons=weapons_involved, hazards=hazards or [])
    return Case(incident=inc, triage=tri)
