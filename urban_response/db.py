"""SQLite storage.

  Resource DB   units, personnel, hospitals           all Sheffield vehicles and on-shift people
  Incident DB   incidents                             pending -> backlog -> closed
  Response DB   responses (FK incident_id)            the CR agent's final decision per dispatch
                agent_responses (FK response_id)      what A1-A3 proposed
                allocations (FK response_id)          units committed, with expected release times
                conflicts (FK response_id)            hard / soft / preempted records
                crew_assignments (FK response_id)     people on each dispatched vehicle
  Ledger        inventory_ledger                      every dispatch / reassignment / return
  Incident log  incident_log                          plain-language timeline per incident

Active allocations and their expected release times drive cross-incident
accounting and scheduling.
"""

from __future__ import annotations

import json
import math
import os
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator

from .schemas import Case, Incident, IncidentResponse, IncidentStatus, Triage

SCHEMA_VERSION = 6  # bump when tables change; older demo DBs are rebuilt

SCHEMA = """
CREATE TABLE IF NOT EXISTS units (
    unit_id       TEXT PRIMARY KEY,
    callsign      TEXT NOT NULL,
    agency        TEXT NOT NULL,           -- police | ambulance | fire | shared
    resource_type TEXT NOT NULL,
    station       TEXT NOT NULL,
    lat           REAL NOT NULL,
    lon           REAL NOT NULL,
    status        TEXT NOT NULL DEFAULT 'available'   -- available | out_of_service
);
CREATE INDEX IF NOT EXISTS idx_units_type ON units(resource_type);

CREATE TABLE IF NOT EXISTS personnel (
    person_id   TEXT PRIMARY KEY,
    agency      TEXT NOT NULL,
    speciality  TEXT NOT NULL,
    station     TEXT NOT NULL,
    lat         REAL NOT NULL,
    lon         REAL NOT NULL,
    on_shift    INTEGER NOT NULL DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_personnel_spec ON personnel(speciality);

CREATE TABLE IF NOT EXISTS hospitals (
    name          TEXT PRIMARY KEY,
    lat           REAL NOT NULL,
    lon           REAL NOT NULL,
    major_trauma  INTEGER NOT NULL DEFAULT 0,
    paediatric    INTEGER NOT NULL DEFAULT 0,
    ed_capacity   INTEGER NOT NULL
);

-- Incident DB -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS incidents (
    incident_id   INTEGER PRIMARY KEY,                -- YYYYMMDDNNNNNN
    received_at   TEXT NOT NULL,
    channel       TEXT NOT NULL,
    category      TEXT NOT NULL,                      -- from triage
    severity      INTEGER NOT NULL,                   -- from triage
    lat           REAL NOT NULL,                      -- resolved by triage
    lon           REAL NOT NULL,
    place         TEXT NOT NULL DEFAULT '',
    status        TEXT NOT NULL DEFAULT 'pending',    -- pending | merged | dispatched | backlog | closed
    duplicate_of  INTEGER REFERENCES incidents(incident_id),
    report_count  INTEGER NOT NULL DEFAULT 1,
    dispatched_at TEXT,
    completed_at  TEXT,
    closed_at     TEXT,
    incident_json TEXT NOT NULL,                      -- canonical incident, exactly as received
    triage_json   TEXT NOT NULL,
    dedup_json    TEXT                                -- MiniLM proposal + the agent's final duplicate decision
);
CREATE INDEX IF NOT EXISTS idx_incidents_status ON incidents(status);

-- Response DB -----------------------------------------------------------------
CREATE TABLE IF NOT EXISTS responses (
    response_id    INTEGER PRIMARY KEY,               -- YYYYMMDDNNNNNN
    incident_id    INTEGER NOT NULL REFERENCES incidents(incident_id),
    created_at     TEXT NOT NULL,
    status         TEXT NOT NULL,                     -- dispatched | partial | failed
    decided_by     TEXT NOT NULL,                     -- CR
    cr_executed_by TEXT NOT NULL,                     -- nemotron | fallback | mock
    response_json  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_responses_incident ON responses(incident_id);

CREATE TABLE IF NOT EXISTS agent_responses (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    response_id  INTEGER NOT NULL REFERENCES responses(response_id),
    agent_id     TEXT NOT NULL,                       -- A1 | A2 | A3
    agency       TEXT NOT NULL,
    executed_by  TEXT NOT NULL,
    payload_json TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS allocations (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    unit_id             TEXT NOT NULL REFERENCES units(unit_id),
    response_id         INTEGER NOT NULL REFERENCES responses(response_id),
    incident_id         INTEGER NOT NULL REFERENCES incidents(incident_id),
    requested_by        TEXT NOT NULL,
    start_at            TEXT NOT NULL,
    expected_release_at TEXT NOT NULL,                -- end of the vehicle's cycle (timing.py)
    timeline_json       TEXT,                         -- en_route / on_scene / returning / cooldown
    released_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_alloc_active ON allocations(unit_id) WHERE released_at IS NULL;

CREATE TABLE IF NOT EXISTS crew_assignments (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    person_id           TEXT NOT NULL REFERENCES personnel(person_id),
    unit_id             TEXT NOT NULL REFERENCES units(unit_id),
    response_id         INTEGER NOT NULL REFERENCES responses(response_id),
    incident_id         INTEGER NOT NULL REFERENCES incidents(incident_id),
    start_at            TEXT NOT NULL,
    expected_release_at TEXT NOT NULL,
    released_at         TEXT
);
CREATE INDEX IF NOT EXISTS idx_crew_active ON crew_assignments(person_id) WHERE released_at IS NULL;

CREATE TABLE IF NOT EXISTS inventory_ledger (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    ts          TEXT NOT NULL,
    event       TEXT NOT NULL,       -- dispatched | reassigned | returned
    item_kind   TEXT NOT NULL,       -- unit | person
    item_id     TEXT NOT NULL,
    item_type   TEXT NOT NULL,       -- resource_type or speciality
    incident_id INTEGER,
    response_id INTEGER,
    detail      TEXT
);

CREATE TABLE IF NOT EXISTS incident_log (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    incident_id INTEGER NOT NULL REFERENCES incidents(incident_id),
    ts          TEXT NOT NULL,
    kind        TEXT NOT NULL,
    text        TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS conflicts (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    response_id  INTEGER NOT NULL REFERENCES responses(response_id),
    incident_id  INTEGER NOT NULL REFERENCES incidents(incident_id),  -- for 'preempted': the incident that lost the unit
    layer        TEXT NOT NULL,                       -- hard | soft | preempted
    payload_json TEXT NOT NULL,
    created_at   TEXT NOT NULL
);
"""

# Blue-light travel model: straight-line distance * road factor at average urban speed.
ROAD_FACTOR = 1.35
AVG_SPEED_KMH = 45.0
TURNOUT_MIN = 1.5


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp, dl = math.radians(lat2 - lat1), math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def eta_minutes(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    km = haversine_km(lat1, lon1, lat2, lon2) * ROAD_FACTOR
    return round(TURNOUT_MIN + km / AVG_SPEED_KMH * 60, 1)


def _iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


def time_scale() -> float:
    """Simulated minutes per real minute (UR_TIME_SCALE). 1 = real time; 30 = units return 30x faster."""
    try:
        return max(float(os.getenv("UR_TIME_SCALE", "1")), 0.01)
    except ValueError:
        return 1.0


def _now() -> datetime:
    return datetime.now(timezone.utc)


class Database:
    def __init__(self, path: Path | str):
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version != SCHEMA_VERSION:
            # Demo database from an older build: rebuild it (it is reseeded on startup).
            self._conn.execute("PRAGMA foreign_keys = OFF")
            for (name,) in self._conn.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'").fetchall():
                self._conn.execute(f"DROP TABLE IF EXISTS {name}")
            self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.executescript(SCHEMA)
        self._conn.execute(f"PRAGMA user_version = {SCHEMA_VERSION}")

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        try:
            yield self._conn
            self._conn.commit()
        except Exception:
            self._conn.rollback()
            raise

    def close(self) -> None:
        self._conn.close()

    # ------------------------------------------------------------------ #
    # Resource DB
    # ------------------------------------------------------------------ #

    def is_empty(self) -> bool:
        units = self._conn.execute("SELECT COUNT(*) FROM units").fetchone()[0]
        people = self._conn.execute("SELECT COUNT(*) FROM personnel").fetchone()[0]
        return units == 0 or people == 0

    def resource_types(self) -> dict[str, str]:
        """resource_type -> owning agency."""
        rows = self._conn.execute("SELECT DISTINCT resource_type, agency FROM units").fetchall()
        return {r["resource_type"]: r["agency"] for r in rows}

    def inventory(self) -> list[dict]:
        """Per resource type: total, in service, committed, available now."""
        rows = self._conn.execute(
            """
            SELECT u.resource_type, u.agency,
                   COUNT(*) AS total,
                   SUM(u.status = 'available') AS in_service,
                   SUM(a.id IS NOT NULL) AS committed
            FROM units u
            LEFT JOIN allocations a ON a.unit_id = u.unit_id AND a.released_at IS NULL
            GROUP BY u.resource_type, u.agency
            ORDER BY u.agency, u.resource_type
            """
        ).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["available"] = d["in_service"] - d["committed"]
            out.append(d)
        return out

    def available_units(self, resource_type: str, lat: float, lon: float) -> list[dict]:
        """Free, in-service units of a type, nearest first, with ETA to (lat, lon)."""
        rows = self._conn.execute(
            """
            SELECT u.* FROM units u
            WHERE u.resource_type = ? AND u.status = 'available'
              AND NOT EXISTS (SELECT 1 FROM allocations a WHERE a.unit_id = u.unit_id AND a.released_at IS NULL)
            """,
            (resource_type,),
        ).fetchall()
        units = [dict(r) | {"eta_min": eta_minutes(r["lat"], r["lon"], lat, lon)} for r in rows]
        return sorted(units, key=lambda u: u["eta_min"])

    def committed_units(self, resource_type: str) -> list[dict]:
        """Units of a type held by active incidents. lat/lon are the incident's (where the unit is now)."""
        rows = self._conn.execute(
            """
            SELECT u.unit_id, u.callsign, u.station, i.lat, i.lon, a.incident_id, i.severity,
                   a.expected_release_at
            FROM allocations a
            JOIN units u ON u.unit_id = a.unit_id
            JOIN incidents i ON i.incident_id = a.incident_id
            WHERE a.released_at IS NULL AND u.resource_type = ?
            ORDER BY i.severity ASC, a.expected_release_at ASC
            """,
            (resource_type,),
        ).fetchall()
        return [dict(r) for r in rows]

    def stations(self) -> list[dict]:
        """Per station: location, agency, units total and free now (for the map)."""
        rows = self._conn.execute(
            """
            SELECT u.station, u.agency, u.lat, u.lon, COUNT(*) AS total,
                   SUM(u.status = 'available' AND a.id IS NULL) AS available
            FROM units u
            LEFT JOIN allocations a ON a.unit_id = u.unit_id AND a.released_at IS NULL
            GROUP BY u.station, u.agency
            ORDER BY u.agency, u.station
            """
        ).fetchall()
        return [dict(r) for r in rows]

    # ---- personnel ---------------------------------------------------------- #

    def available_personnel(self, speciality: str) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT p.* FROM personnel p
            WHERE p.speciality = ? AND p.on_shift = 1
              AND NOT EXISTS (SELECT 1 FROM crew_assignments c WHERE c.person_id = p.person_id AND c.released_at IS NULL)
            ORDER BY p.person_id
            """,
            (speciality,),
        ).fetchall()
        return [dict(r) for r in rows]

    def crew_of_unit(self, unit_id: str) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT p.person_id, p.speciality, p.station, c.incident_id FROM crew_assignments c
            JOIN personnel p ON p.person_id = c.person_id
            WHERE c.unit_id = ? AND c.released_at IS NULL
            """,
            (unit_id,),
        ).fetchall()
        return [dict(r) for r in rows]

    def personnel_inventory(self) -> list[dict]:
        rows = self._conn.execute(
            """
            SELECT p.agency, p.speciality, COUNT(*) AS total, SUM(p.on_shift) AS on_shift,
                   SUM(c.id IS NOT NULL) AS committed
            FROM personnel p
            LEFT JOIN crew_assignments c ON c.person_id = p.person_id AND c.released_at IS NULL
            GROUP BY p.agency, p.speciality ORDER BY p.agency, p.speciality
            """
        ).fetchall()
        return [dict(r) | {"available": r["on_shift"] - r["committed"]} for r in rows]

    def unit(self, unit_id: str) -> dict | None:
        r = self._conn.execute("SELECT * FROM units WHERE unit_id = ?", (unit_id,)).fetchone()
        return dict(r) if r else None

    def hospitals(self, lat: float, lon: float) -> list[dict]:
        rows = self._conn.execute("SELECT * FROM hospitals").fetchall()
        hs = [dict(r) | {"eta_min": eta_minutes(lat, lon, r["lat"], r["lon"])} for r in rows]
        return sorted(hs, key=lambda h: h["eta_min"])

    # ------------------------------------------------------------------ #
    # Incident DB
    # ------------------------------------------------------------------ #

    def next_id(self, table: str, column: str, when: datetime | None = None) -> int:
        """Next YYYYMMDDNNNNNN id for the day (callers hold the ingest or commit lock)."""
        day = int((when or _now()).astimezone(timezone.utc).strftime("%Y%m%d")) * 1_000_000
        top = self._conn.execute(f"SELECT MAX({column}) FROM {table} WHERE {column} BETWEEN ? AND ?",
                                 (day, day + 999_999)).fetchone()[0]
        return (top or day) + 1

    def insert_incident(self, incident: Incident, triage: Triage, status: str = "pending",
                        duplicate_of: int | None = None) -> int:
        """Register a canonical incident with its triage. Assigns incident_id if missing. Returns it."""
        if incident.incident_id is None:
            incident.incident_id = self.next_id("incidents", "incident_id", incident.received_at)
        with self.tx() as c:
            c.execute(
                """
                INSERT INTO incidents (incident_id, received_at, channel, category, severity, lat, lon, place, status,
                                       duplicate_of, incident_json, triage_json)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (incident.incident_id, _iso(incident.received_at), incident.channel, triage.category, triage.severity,
                 triage.lat, triage.lon, triage.place, status, duplicate_of, incident.model_dump_json(),
                 triage.model_dump_json()),
            )
            if duplicate_of:
                c.execute("UPDATE incidents SET report_count = report_count + 1 WHERE incident_id = ?", (duplicate_of,))
        return incident.incident_id

    def merge_incident(self, child_id: int, parent_id: int) -> int:
        """Fold a report into an earlier incident. Follows chains to the root and moves any reports
        already merged into the child. Returns the root id."""
        root = parent_id
        seen = {child_id}
        while True:
            r = self._conn.execute("SELECT duplicate_of FROM incidents WHERE incident_id = ?", (root,)).fetchone()
            if not r or r["duplicate_of"] is None or r["duplicate_of"] in seen:
                break
            seen.add(root)
            root = r["duplicate_of"]
        if root == child_id:
            raise ValueError("cannot merge an incident into itself")
        with self.tx() as c:
            moved = c.execute("SELECT COUNT(*) FROM incidents WHERE duplicate_of = ?", (child_id,)).fetchone()[0]
            c.execute("UPDATE incidents SET duplicate_of = ? WHERE duplicate_of = ?", (root, child_id))
            c.execute("UPDATE incidents SET status = 'merged', duplicate_of = ?, report_count = 1 WHERE incident_id = ?",
                      (root, child_id))
            c.execute("UPDATE incidents SET report_count = report_count + ? WHERE incident_id = ?", (1 + moved, root))
        return root

    def set_dedup(self, incident_id: int, decision) -> None:
        with self.tx() as c:
            c.execute("UPDATE incidents SET dedup_json = ? WHERE incident_id = ?", (decision.model_dump_json(), incident_id))

    def set_status(self, incident_id: int, status: IncidentStatus | str) -> None:
        status = IncidentStatus(status).value
        col = {"dispatched": "dispatched_at", "backlog": "completed_at", "closed": "closed_at"}.get(status)
        with self.tx() as c:
            if col:
                c.execute(f"UPDATE incidents SET status = ?, {col} = ? WHERE incident_id = ?", (status, _iso(_now()), incident_id))
            else:
                c.execute("UPDATE incidents SET status = ? WHERE incident_id = ?", (status, incident_id))

    def recent_incidents(self, within_minutes: float,
                         statuses: tuple[str, ...] = ("pending", "dispatched", "backlog", "merged")) -> list[dict]:
        """Canonical incidents received recently that a new report could duplicate (for MiniLM clustering)."""
        since = _iso(_now() - timedelta(minutes=within_minutes))
        marks = ",".join("?" * len(statuses))
        rows = self._conn.execute(
            f"SELECT incident_id, status, duplicate_of, report_count, incident_json, triage_json FROM incidents "
            f"WHERE status IN ({marks}) AND received_at >= ? ORDER BY received_at",
            (*statuses, since),
        ).fetchall()
        return [{"incident_id": r["incident_id"], "status": r["status"], "duplicate_of": r["duplicate_of"],
                 "report_count": r["report_count"], "incident": Incident.model_validate_json(r["incident_json"]),
                 "triage": Triage.model_validate_json(r["triage_json"])} for r in rows]

    def get_case(self, incident_id: int) -> Case | None:
        r = self._conn.execute("SELECT incident_json, triage_json FROM incidents WHERE incident_id = ?", (incident_id,)).fetchone()
        if not r:
            return None
        return Case(incident=Incident.model_validate_json(r["incident_json"]), triage=Triage.model_validate_json(r["triage_json"]))

    def incidents(self, status: IncidentStatus | str | None = None) -> list[dict]:
        """Incidents with their latest response id and units still committed."""
        q = """
            SELECT i.incident_id, i.received_at, i.category, i.severity, i.lat, i.lon, i.status,
                   i.duplicate_of, i.report_count, i.dispatched_at, i.completed_at, i.closed_at,
                   COALESCE(json_extract(i.incident_json, '$.location.address'),
                            json_extract(i.incident_json, '$.location.landmark'), i.place) AS address,
                   (SELECT r.response_id FROM responses r WHERE r.incident_id = i.incident_id
                    ORDER BY r.created_at DESC LIMIT 1) AS latest_response_id,
                   COUNT(a.id) AS units_committed,
                   MAX(a.expected_release_at) AS expected_release_at
            FROM incidents i
            LEFT JOIN allocations a ON a.incident_id = i.incident_id AND a.released_at IS NULL
        """
        args: tuple = ()
        if status:
            q += " WHERE i.status = ?"
            args = (IncidentStatus(status).value,)
        q += " GROUP BY i.incident_id ORDER BY i.received_at DESC"
        return [dict(r) for r in self._conn.execute(q, args).fetchall()]

    def active_incidents(self) -> list[dict]:
        """Incidents that still hold units (scheduling / pre-emption context)."""
        return [i for i in self.incidents() if i["units_committed"]]

    def get_incident(self, incident_id: int) -> dict | None:
        r = self._conn.execute("SELECT * FROM incidents WHERE incident_id = ?", (incident_id,)).fetchone()
        if not r:
            return None
        d = dict(r)
        d["incident"] = json.loads(d.pop("incident_json"))
        d["triage"] = json.loads(d.pop("triage_json"))
        d["dedup"] = json.loads(d.pop("dedup_json")) if d.get("dedup_json") else None
        d["responses"] = [x["response_id"] for x in self.responses(incident_id)]
        d["merged_reports"] = [
            {"incident_id": x["incident_id"], "received_at": x["received_at"],
             "incident": json.loads(x["incident_json"])}
            for x in self._conn.execute(
                "SELECT incident_id, received_at, incident_json FROM incidents WHERE duplicate_of = ? ORDER BY received_at",
                (incident_id,)).fetchall()
        ]
        return d

    # ---- incident log ------------------------------------------------------ #

    def add_log(self, incident_id: int, kind: str, text: str, ts: datetime | None = None) -> None:
        with self.tx() as c:
            c.execute("INSERT INTO incident_log (incident_id, ts, kind, text) VALUES (?, ?, ?, ?)",
                      (incident_id, _iso(ts or _now()), kind, text))

    def incident_log(self, incident_id: int) -> list[dict]:
        rows = self._conn.execute("SELECT ts, kind, text FROM incident_log WHERE incident_id = ? ORDER BY ts, id",
                                  (incident_id,)).fetchall()
        return [dict(r) for r in rows]

    # ---- releases ---------------------------------------------------------- #

    def _release(self, c: sqlite3.Connection, where: str, args: tuple, event: str = "returned") -> list[dict]:
        """Release allocations (and their crews) matching `where`; write ledger rows. Returns released units."""
        now = _iso(_now())
        units = c.execute(
            f"""SELECT a.id, a.unit_id, a.incident_id, a.response_id, u.callsign, u.resource_type
                FROM allocations a JOIN units u ON u.unit_id = a.unit_id
                WHERE a.released_at IS NULL AND {where}""", args).fetchall()
        for u in units:
            c.execute("UPDATE allocations SET released_at = ? WHERE id = ?", (now, u["id"]))
            c.execute("INSERT INTO inventory_ledger (ts, event, item_kind, item_id, item_type, incident_id, response_id, detail) "
                      "VALUES (?, ?, 'unit', ?, ?, ?, ?, ?)",
                      (now, event, u["unit_id"], u["resource_type"], u["incident_id"], u["response_id"], u["callsign"]))
            crew = c.execute(
                "SELECT c.id, c.person_id, p.speciality FROM crew_assignments c JOIN personnel p ON p.person_id = c.person_id "
                "WHERE c.unit_id = ? AND c.incident_id = ? AND c.released_at IS NULL", (u["unit_id"], u["incident_id"])).fetchall()
            for m in crew:
                c.execute("UPDATE crew_assignments SET released_at = ? WHERE id = ?", (now, m["id"]))
                c.execute("INSERT INTO inventory_ledger (ts, event, item_kind, item_id, item_type, incident_id, response_id, detail) "
                          "VALUES (?, ?, 'person', ?, ?, ?, ?, ?)",
                          (now, event, m["person_id"], m["speciality"], u["incident_id"], u["response_id"], u["callsign"]))
        return [dict(u) for u in units]

    def _close_if_clear(self, c: sqlite3.Connection, incident_id: int) -> bool:
        left = c.execute("SELECT COUNT(*) FROM allocations WHERE incident_id = ? AND released_at IS NULL", (incident_id,)).fetchone()[0]
        status = c.execute("SELECT status FROM incidents WHERE incident_id = ?", (incident_id,)).fetchone()
        if left == 0 and status and status[0] == "backlog":
            c.execute("UPDATE incidents SET status = 'closed', closed_at = ? WHERE incident_id = ?", (_iso(_now()), incident_id))
            return True
        return False

    def release_due(self) -> list[dict]:
        """Return units whose expected release time has passed; close backlog incidents left with no units."""
        with self.tx() as c:
            released = self._release(c, "a.expected_release_at <= ?", (_iso(_now()),))
            closed = {u["incident_id"] for u in released if self._close_if_clear(c, u["incident_id"])}
        for u in released:
            self.add_log(u["incident_id"], "returned", f"{u['callsign']} back in service.")
        for iid in closed:
            self.add_log(iid, "closed", "All units back in service. Incident closed.")
        return released

    def close_incident(self, incident_id: int) -> int:
        """Release every unit and crew member of an incident and close it. Returns units released."""
        now = _iso(_now())
        with self.tx() as c:
            released = self._release(c, "a.incident_id = ?", (incident_id,))
            c.execute("UPDATE incidents SET status = 'closed', closed_at = ? WHERE incident_id = ?", (now, incident_id))
        return len(released)

    def release_unit(self, unit_id: str) -> bool:
        with self.tx() as c:
            released = self._release(c, "a.unit_id = ?", (unit_id,))
            for u in released:
                self._close_if_clear(c, u["incident_id"])
        return bool(released)

    def fleet(self) -> list[dict]:
        """Every vehicle out on a job: which stage of its cycle it is in and when it is back in service."""
        from . import timing

        rows = self._conn.execute(
            """SELECT a.unit_id, a.incident_id, a.start_at, a.expected_release_at, a.timeline_json,
                      u.callsign, u.resource_type, u.agency, u.station
               FROM allocations a JOIN units u ON u.unit_id = a.unit_id
               WHERE a.released_at IS NULL ORDER BY a.expected_release_at"""
        ).fetchall()
        scale, out = time_scale(), []
        for r in rows:
            stages = json.loads(r["timeline_json"]) if r["timeline_json"] else []
            state = timing.current_stage(stages, datetime.fromisoformat(r["start_at"]), scale) if stages else \
                {"stage": "committed", "detail": "", "stage_left_min": None, "back_in_service_min": None}
            out.append({k: r[k] for k in ("unit_id", "callsign", "resource_type", "agency", "station", "incident_id",
                                          "expected_release_at")} | state)
        return out

    def ledger(self, limit: int = 200, incident_id: int | None = None) -> list[dict]:
        q, args = "SELECT * FROM inventory_ledger", []
        if incident_id:
            q += " WHERE incident_id = ?"
            args.append(incident_id)
        rows = self._conn.execute(q + " ORDER BY id DESC LIMIT ?", (*args, limit)).fetchall()
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------ #
    # Response DB
    # ------------------------------------------------------------------ #

    def record_response(self, incident: Case, resp: IncidentResponse) -> None:
        """Write CR's final response atomically: units, crews, ledger, conflicts; incident -> dispatched.

        A 'failed' response (nothing dispatched) is stored but leaves the incident pending.
        """
        if resp.incident_id != incident.incident_id:
            raise ValueError(f"response {resp.response_id} belongs to {resp.incident_id}, not {incident.incident_id}")
        now = _now()
        if resp.response_id is None:
            resp.response_id = self.next_id("responses", "response_id")
        rid, iid = resp.response_id, resp.incident_id
        durations = {}
        for r in resp.agent_responses:
            for req in r.resource_requests:
                durations[req.resource_type] = max(durations.get(req.resource_type, 0), req.expected_duration_min)
        scale = time_scale()

        def ledger(c, event, kind, item_id, item_type, detail, inc=iid):
            c.execute("INSERT INTO inventory_ledger (ts, event, item_kind, item_id, item_type, incident_id, response_id, detail) "
                      "VALUES (?, ?, ?, ?, ?, ?, ?, ?)", (_iso(now), event, kind, item_id, item_type, inc, rid, detail))

        with self.tx() as c:
            c.execute(
                """
                INSERT INTO responses (response_id, incident_id, created_at, status, decided_by, cr_executed_by, response_json)
                VALUES (?, ?, ?, ?, ?, ?, ?)
                """,
                (rid, iid, _iso(resp.created_at), resp.status, resp.decided_by, resp.cr_executed_by.value,
                 resp.model_dump_json()),
            )
            for ar in resp.agent_responses:
                c.execute(
                    "INSERT INTO agent_responses (response_id, agent_id, agency, executed_by, payload_json) VALUES (?,?,?,?,?)",
                    (rid, ar.agent_id, ar.agency.value, ar.executed_by.value, ar.model_dump_json()),
                )
            for alloc in resp.allocations:
                if alloc.preempted_from:
                    old = alloc.preempted_from
                    c.execute("UPDATE allocations SET released_at = ? WHERE unit_id = ? AND incident_id = ? AND released_at IS NULL",
                              (_iso(now), alloc.unit_id, old))
                    c.execute("UPDATE crew_assignments SET released_at = ? WHERE unit_id = ? AND incident_id = ? AND released_at IS NULL",
                              (_iso(now), alloc.unit_id, old))
                    c.execute(
                        "INSERT INTO conflicts (response_id, incident_id, layer, payload_json, created_at) VALUES (?, ?, 'preempted', ?, ?)",
                        (rid, old, json.dumps({"unit_id": alloc.unit_id, "resource_type": alloc.resource_type,
                                               "reassigned_to": iid, "severity": incident.severity}), _iso(now)),
                    )
                    c.execute("INSERT INTO incident_log (incident_id, ts, kind, text) VALUES (?, ?, 'reassigned', ?)",
                              (old, _iso(now), f"{alloc.callsign} reassigned to {iid} (severity {incident.severity})."))
                    ledger(c, "reassigned", "unit", alloc.unit_id, alloc.resource_type, f"{alloc.callsign} from {old}")
                # The whole vehicle cycle (timing.py); older callers without a timeline fall back to duration + ETA.
                cycle = alloc.back_in_service_min or (durations.get(alloc.resource_type, 60) + alloc.eta_min)
                release_at = _iso(now + timedelta(minutes=cycle / scale))
                c.execute(
                    """
                    INSERT INTO allocations (unit_id, response_id, incident_id, requested_by, start_at, expected_release_at,
                                             timeline_json)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (alloc.unit_id, rid, iid, alloc.requested_by.value, _iso(now), release_at, json.dumps(alloc.timeline)),
                )
                if not alloc.preempted_from:
                    ledger(c, "dispatched", "unit", alloc.unit_id, alloc.resource_type, alloc.callsign)
                for m in alloc.crew:
                    c.execute(
                        "INSERT INTO crew_assignments (person_id, unit_id, response_id, incident_id, start_at, expected_release_at) "
                        "VALUES (?, ?, ?, ?, ?, ?)", (m.person_id, alloc.unit_id, rid, iid, _iso(now), release_at))
                    ledger(c, "reassigned" if alloc.preempted_from else "dispatched", "person", m.person_id, m.speciality, alloc.callsign)
            for layer, items in (("hard", resp.hard_conflicts), ("soft", resp.soft_conflicts)):
                for item in items:
                    c.execute(
                        "INSERT INTO conflicts (response_id, incident_id, layer, payload_json, created_at) VALUES (?, ?, ?, ?, ?)",
                        (rid, iid, layer, item.model_dump_json(), _iso(now)),
                    )
            if resp.status != "failed":
                c.execute("UPDATE incidents SET status = 'dispatched', dispatched_at = ? WHERE incident_id = ?", (_iso(now), iid))

    def update_response(self, resp: IncidentResponse) -> None:
        with self.tx() as c:
            c.execute("UPDATE responses SET response_json = ?, status = ? WHERE response_id = ?",
                      (resp.model_dump_json(), resp.status, resp.response_id))

    def responses(self, incident_id: int | None = None) -> list[dict]:
        q = "SELECT response_id, incident_id, created_at, status, decided_by, cr_executed_by FROM responses"
        args: tuple = ()
        if incident_id:
            q += " WHERE incident_id = ?"
            args = (incident_id,)
        return [dict(r) for r in self._conn.execute(q + " ORDER BY created_at", args).fetchall()]

    def get_response(self, response_id: int) -> dict | None:
        r = self._conn.execute("SELECT response_json FROM responses WHERE response_id = ?", (response_id,)).fetchone()
        return json.loads(r["response_json"]) if r else None

    def conflict_log(self, incident_id: int | None = None, response_id: int | None = None) -> list[dict]:
        q, conds, args = "SELECT * FROM conflicts", [], []
        if incident_id:
            conds.append("incident_id = ?")
            args.append(incident_id)
        if response_id:
            conds.append("response_id = ?")
            args.append(response_id)
        if conds:
            q += " WHERE " + " AND ".join(conds)
        rows = self._conn.execute(q + " ORDER BY id", args).fetchall()
        return [dict(r) | {"payload": json.loads(r["payload_json"])} for r in rows]

    def availability_forecast(self, resource_type: str, horizon_min: int = 120, step_min: int = 15) -> list[dict]:
        """How many units of a type are expected free at each step, from expected release times."""
        now = datetime.now(timezone.utc)
        free_now = sum(1 for _ in self.available_units(resource_type, 0, 0))
        committed = self.committed_units(resource_type)
        out = []
        for t in range(0, horizon_min + 1, step_min):
            at = now + timedelta(minutes=t)
            released = sum(1 for u in committed if datetime.fromisoformat(u["expected_release_at"]) <= at)
            out.append({"t_plus_min": t, "available": free_now + released})
        return out
