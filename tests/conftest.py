import pytest

from urban_response.config import Settings
from urban_response.db import Database
from urban_response.schemas import Incident, Location
from urban_response.seed import seed


@pytest.fixture
def db():
    d = Database(":memory:")
    seed(d)
    yield d
    d.close()


@pytest.fixture
def settings():
    return Settings(llm_mode="mock")


def make_incident(**kw) -> Incident:
    base = dict(
        category="structure_fire",
        description="test",
        location=Location(lat=53.3811, lon=-1.4701, address="1 Test Street, City Centre", district="City Centre"),
        severity=3,
    )
    base.update(kw)
    return Incident(**base)
