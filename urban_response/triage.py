"""Intake triage: derive category, severity and coordinates from a canonical incident.

The canonical incident is what the caller said; triage is what the orchestrator concludes
from it. Qwen does this in live mode (orchestrator.triage); rule_triage() is the
deterministic version used in mock mode, as Qwen's fallback, and to check Qwen's output.

Coordinates: caller GPS / alarm panel first, then the Sheffield gazetteer below (offline,
works at the venue), then Qwen's estimate (flagged), then the city centre (flagged).
Gazetteer coordinates are approximate, for the demo.
"""

from __future__ import annotations

import re

from .schemas import CATEGORIES, ExecutedBy, Incident, Triage

SHEFFIELD_BBOX = (53.30, 53.48, -1.62, -1.30)  # lat_min, lat_max, lon_min, lon_max
CITY_CENTRE = (53.3811, -1.4701)

GAZETTEER: dict[str, tuple[float, float]] = {
    "park hill": (53.3800, -1.4610), "south street": (53.3797, -1.4612), "duke street": (53.3790, -1.4625),
    "talbot street": (53.3776, -1.4645), "sheaf street": (53.3806, -1.4598), "sheffield station": (53.3781, -1.4620),
    "hanover way": (53.3770, -1.4830), "broomhall": (53.3760, -1.4870), "ecclesall road": (53.3700, -1.4900),
    "hickmott road": (53.3693, -1.4930), "sharrow": (53.3680, -1.4800), "hunters bar": (53.3653, -1.5000),
    "fitzalan square": (53.3825, -1.4645), "haymarket": (53.3836, -1.4630), "castle market": (53.3840, -1.4640),
    "sheffield parkway": (53.3870, -1.4300), "parkway": (53.3870, -1.4300), "darnall": (53.3880, -1.4170),
    "meadowhall road": (53.4130, -1.4100), "meadowhall": (53.4140, -1.4130), "tinsley": (53.4100, -1.4000),
    "attercliffe": (53.3950, -1.4280), "city hall": (53.3815, -1.4720), "peace gardens": (53.3800, -1.4700),
    "crucible": (53.3804, -1.4669), "fargate": (53.3826, -1.4686), "west street": (53.3810, -1.4790),
    "division street": (53.3795, -1.4780), "london road": (53.3670, -1.4740), "abbeydale road": (53.3600, -1.4840),
    "bramall lane": (53.3703, -1.4709), "hillsborough": (53.4120, -1.5000), "kelham island": (53.3900, -1.4700),
    "northern general": (53.4096, -1.4570), "royal hallamshire": (53.3779, -1.4892), "childrens hospital": (53.3802, -1.4878),
    "crookes": (53.3870, -1.5140), "walkley": (53.3930, -1.4980), "firth park": (53.4170, -1.4440),
    "snig hill": (53.3851, -1.4686), "woodseats": (53.3400, -1.4800), "ecclesfield": (53.4430, -1.4700),
    "lowedges": (53.3240, -1.4900), "stannington": (53.3980, -1.5300), "handsworth": (53.3700, -1.3850),
    "middlewood": (53.4110, -1.5050), "utilita arena": (53.4013, -1.4210), "sheffield arena": (53.4013, -1.4210),
}

_KEYWORDS: list[tuple[str, tuple[str, ...]]] = [
    ("firearms", ("shot", "shots", "gun", "firearm", "shooting", "gunman")),
    ("stabbing", ("stab", "stabbed", "knife", "blade")),
    ("high_rise_fire", ("tower block", "high-rise", "high rise", "floor alight", "tower on fire")),
    ("structure_fire", ("on fire", "fire in", "fire on", "flames", "smoke", "blaze", "alight", "burning", "fire")),
    ("hazmat", ("chemical", "tanker leaking", "toxic", "fumes", "spill")),
    ("gas_leak", ("gas leak", "smell of gas", "gas smell")),
    ("rtc", ("collision", "crash", "car hit", "hit a", "knocked down", "rtc", "swerved", "overturned", "pile-up")),
    ("flood", ("flood", "flooding", "water rising", "standing water")),
    ("cardiac_arrest", ("not breathing", "cardiac", "no pulse", "cpr")),
    ("overdose", ("overdose", "took too many")),
    ("assault", ("assault", "fight", "punched", "attacked", "beaten")),
    ("public_disorder", ("riot", "disorder", "crowd fighting", "brawl")),
    ("missing_person", ("missing", "can't find", "cannot find")),
    ("fall", ("fell", "fallen", "fall from")),
    ("medical", ("unconscious", "chest pain", "seizure", "fitting", "bleeding", "injured", "unwell", "collapsed", "not responding")),
]


def norm(text: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).strip()


def in_sheffield(lat: float, lon: float) -> bool:
    a, b, c, d = SHEFFIELD_BBOX
    return a <= lat <= b and c <= lon <= d


def gazetteer_lookup(*texts: str | None) -> tuple[str, tuple[float, float]] | None:
    """Longest gazetteer name that appears in any of the texts."""
    hay = " " + " ".join(norm(t) for t in texts if t) + " "
    best = None
    for name, coords in GAZETTEER.items():
        if f" {name} " in hay and (best is None or len(name) > len(best[0])):
            best = (name, coords)
    return best


def resolve_location(inc: Incident) -> tuple[float, float, str, str] | None:
    """(lat, lon, resolved_by, place) without any model, or None if unknown."""
    loc = inc.location
    if loc.lat is not None and loc.lon is not None and in_sheffield(loc.lat, loc.lon):
        hit = gazetteer_lookup(loc.address, loc.landmark)
        return loc.lat, loc.lon, "caller", (hit[0].title() if hit else (loc.address or loc.landmark or ""))
    hit = gazetteer_lookup(loc.address, loc.landmark, loc.location_notes, inc.description)
    if hit:
        return hit[1][0], hit[1][1], "gazetteer", hit[0].title()
    return None


_FIRE_SERVICE = re.compile(r"\bfire (engine|engines|crew|crews|brigade|service|appliance)s?\b")
_FLOOR = re.compile(r"\b(\d+(st|nd|rd|th)|fifth|sixth|seventh|eighth|ninth|tenth|top) floor\b")
_FIREISH = re.compile(r"\b(fire|smoke|smoking|flames?|alight|blaze|burning)\b")


def classify(inc: Incident) -> str:
    raw = (inc.description + " " + " ".join(inc.hazards_reported)).lower()
    raw = _FIRE_SERVICE.sub(" ", raw)  # "swerved to avoid a fire engine" is not a fire
    text = " " + norm(raw) + " "
    if _FIREISH.search(raw):
        if " tower" in text or " high rise " in text or _FLOOR.search(raw):
            return "high_rise_fire"
        if any(w in text for w in (" car on fire ", " van on fire ", " vehicle fire ", " lorry on fire ")):
            return "vehicle_fire"
    for cat, words in _KEYWORDS:
        if any(f" {norm(w)} " in text for w in words):
            if cat == "structure_fire" and any(w in text for w in (" tower ", " high rise ", " 9th floor ", " tenth floor ")):
                return "high_rise_fire"
            if cat == "structure_fire" and any(w in text for w in (" car on fire ", " van on fire ", " vehicle fire ")):
                return "vehicle_fire"
            return cat
    if inc.vehicles:
        return "rtc"
    return "other"


def derived_counts(inc: Incident) -> tuple[int, int, bool]:
    casualties = max(inc.casualties_reported or 0, len(inc.patients))
    trapped = inc.people_trapped_reported or 0
    weapons = bool(inc.weapons_mentioned) or any(p.armed for p in inc.persons_of_interest)
    return casualties, trapped, weapons


def rule_severity(inc: Incident, category: str, casualties: int, trapped: int, weapons: bool) -> int:
    sev = 2
    if category in ("high_rise_fire", "firearms", "hazmat"):
        sev = 4
    elif category in ("structure_fire", "stabbing", "cardiac_arrest", "public_disorder", "gas_leak"):
        sev = 3
    if weapons:
        sev = max(sev, 4)
    if trapped:
        sev = max(sev, 3 if trapped == 1 else 4)
    if casualties >= 5:
        sev = max(sev, 5)
    elif casualties >= 3:
        sev = max(sev, 4)
    elif casualties >= 1:
        sev = max(sev, 2)
    if any(p.breathing == "none" or p.conscious is False for p in inc.patients):
        sev = max(sev, 4)
    said = norm(inc.caller_reported_severity)
    negated = re.search(r"\b(not|no|isn t|isnt)\b", said)
    if not negated and any(w in said for w in ("critical", "life threatening", "major", "dying")):
        sev = max(sev, 4)
    if category == "high_rise_fire" and (trapped or casualties):
        sev = 5
    return min(sev, 5)


def rule_triage(inc: Incident) -> Triage:
    category = classify(inc)
    casualties, trapped, weapons = derived_counts(inc)
    resolved = resolve_location(inc)
    if resolved:
        lat, lon, by, place = resolved
    else:
        lat, lon, by, place = CITY_CENTRE[0], CITY_CENTRE[1], "default", inc.location.address or inc.location.landmark or "unknown location"
    return Triage(
        category=category, severity=rule_severity(inc, category, casualties, trapped, weapons),
        lat=lat, lon=lon, location_resolved_by=by, place=place,
        casualties=casualties, trapped=trapped, weapons=weapons, hazards=list(inc.hazards_reported),
        rationale="Keyword classification and severity rules.", executed_by=ExecutedBy.rules,
    )


def check(t: Triage, base: Triage) -> Triage:
    """Guardrails over a model's triage: known category, counts never below what the caller reported,
    coordinates inside Sheffield (else fall back to the rule-resolved ones)."""
    upd = {}
    if t.category not in CATEGORIES:
        upd["category"] = base.category
    upd["casualties"] = max(t.casualties, base.casualties)
    upd["trapped"] = max(t.trapped, base.trapped)
    upd["weapons"] = t.weapons or base.weapons
    upd["hazards"] = sorted(set(t.hazards) | set(base.hazards))
    if base.location_resolved_by in ("caller", "gazetteer"):
        upd.update(lat=base.lat, lon=base.lon, location_resolved_by=base.location_resolved_by, place=t.place or base.place)
    elif not in_sheffield(t.lat, t.lon):
        upd.update(lat=base.lat, lon=base.lon, location_resolved_by="default")
    else:
        upd["location_resolved_by"] = "qwen"
    return t.model_copy(update=upd)
