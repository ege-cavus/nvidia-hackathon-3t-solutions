"""Duplicate detection: MiniLM clustering via incident_cluster_handoff.py.

incident_cluster_handoff.py (repo root, vendored unchanged from origin/main, commit 1fc4340)
is the de-duplication engine. For every new report we take ONE canonical report per live
incident from the last hour, its representative: the earliest report with caller coordinates,
else the earliest report. (Clustering every caller of an incident lets two callers'
differently-worded landmarks veto a merge through complete linkage, and makes the outcome depend
on arrival order.) We add the new report and run exactly what that script does:

  1. embed each description with sentence-transformers/all-MiniLM-L6-v2 (normalised)
  2. build_distances(): cosine distance, + location penalty / - location bonus,
     + time penalty beyond 30 min, - registration bonus for a shared vehicle plate
  3. AgglomerativeClustering, complete linkage, distance_threshold 0.85 (as the commit requires)

MiniLM PROPOSES; the orchestrator agent (Qwen) DECIDES. propose() returns MiniLM's verdict and
the candidate incidents (those it clustered the report with, plus active incidents within 500 m
it did not), and the agent makes the final call, told to dispatch separately whenever the
reports are probably different incidents. Without the agent (mock mode, or the call fails),
MiniLM's verdict stands: a report in a cluster with an existing incident is merged into it;
otherwise it is new, or related if an active incident is within 500 m.

The script's code and parameters are not modified here; this module only feeds it.

Safety guard (UR_DEDUP_CATEGORY_GUARD, on by default): a merge means nothing is dispatched for
the new report, so if MiniLM clusters two reports whose triaged incident types are clearly
different (e.g. a road collision and a building fire 350 m apart that both mention a fire
engine), the merge is refused and the report is kept as a separate, related incident. The
guard can only prevent merges, never create them, and every override is stated in the reason.
"""

from __future__ import annotations

import os
import threading
from argparse import Namespace

import numpy as np
from sklearn.cluster import AgglomerativeClustering

import incident_cluster_handoff as ich  # noqa: E402  (repo root; do not edit that file)

from .db import haversine_km
from .schemas import DedupDecision, DedupVerdict, ExecutedBy, Incident, Triage

THRESHOLD = 0.85  # commit 1fc4340: "must be threshold at 0.85"
# The script's own argparse defaults, unchanged.
PARAMS = Namespace(threshold=THRESHOLD, radius_km=0.5, location_penalty=1.0, location_bonus=0.10,
                   time_window_minutes=30, time_penalty=0.35, registration_bonus=0.10)
MODEL = os.getenv("UR_MINILM_MODEL", "sentence-transformers/all-MiniLM-L6-v2")  # name or local path
WINDOW_MIN = 60
RELATED_RADIUS_M = 500
CATEGORY_GUARD = os.getenv("UR_DEDUP_CATEGORY_GUARD", "1") == "1"
CATEGORY_FAMILIES = [
    {"structure_fire", "high_rise_fire", "vehicle_fire"},
    {"rtc", "vehicle_fire"},
    {"assault", "stabbing", "firearms", "robbery", "public_disorder"},
    {"hazmat", "gas_leak"},
    {"medical", "cardiac_arrest", "overdose", "fall"},
]


def compatible(a: str, b: str) -> bool:
    return a == b or "other" in (a, b) or any(a in f and b in f for f in CATEGORY_FAMILIES)

_model = None
_lock = threading.Lock()
_embed_override = None  # tests inject a deterministic embedder


def set_embedder(fn) -> None:
    """Replace the MiniLM encoder (tests). fn(list[str]) -> np.ndarray of unit vectors."""
    global _embed_override
    _embed_override = fn


def load_model():
    """Load MiniLM once (honours HF_HUB_OFFLINE=1 when the model is cached or MODEL is a path)."""
    global _model
    with _lock:
        if _model is None:
            from sentence_transformers import SentenceTransformer

            _model = SentenceTransformer(MODEL)
    return _model


def embed(texts: list[str]) -> np.ndarray:
    if _embed_override:
        return np.asarray(_embed_override(texts), dtype=float)
    return load_model().encode(texts, normalize_embeddings=True)


def cluster(reports: list[dict]) -> tuple[np.ndarray, np.ndarray]:
    """Run the script's pipeline on canonical incident dicts. Returns (labels, distances)."""
    vectors = embed([r["description"] for r in reports])
    distances = ich.build_distances(reports, vectors, PARAMS)
    np.fill_diagonal(distances, 0)
    labels = AgglomerativeClustering(n_clusters=None, distance_threshold=THRESHOLD,
                                     metric="precomputed", linkage="complete").fit_predict(distances)
    return labels, distances


def decide(new: Incident, triage: Triage, recent: list[dict]) -> DedupDecision:
    """MiniLM's verdict alone (used in mock mode, as the agent's fallback, and in tests)."""
    return propose(new, triage, recent)[0]


def propose(new: Incident, triage: Triage, recent: list[dict]) -> tuple[DedupDecision, list[dict]]:
    """Run the clustering and return (MiniLM's proposed decision, candidate incidents for the agent).

    recent: [{incident_id, status, duplicate_of, incident: Incident, triage: Triage | None}]
    Candidates are incidents MiniLM clustered the report with, plus active incidents within 500 m
    that it did not cluster, so the deciding agent sees both what MiniLM matched and what it missed.
    """
    groups: dict[int, list[dict]] = {}
    for r in recent:
        if r["incident_id"] != new.incident_id and r["status"] != "closed":
            groups.setdefault(r["duplicate_of"] or r["incident_id"], []).append(r)

    def representative(members: list[dict]) -> dict:
        located = [m for m in members if m["incident"].location.lat is not None and m["incident"].location.lon is not None]
        rep = min(located or members, key=lambda m: m["incident"].received_at)
        root = next((m for m in members if m["duplicate_of"] is None), rep)
        # Cluster on the representative's report, but act on the incident (root id, status, triage).
        return {**root, "incident": rep["incident"], "reports": len(members)}

    pool = [representative(ms) for ms in groups.values() if any(m["duplicate_of"] is None for m in ms)]
    if not pool:
        return DedupDecision(incident_id=new.incident_id, verdict=DedupVerdict.new, executed_by=ExecutedBy.minilm,
                             reason="No other report in the last hour to compare with."), []
    reports = [r["incident"].canonical_json() for r in pool] + [new.canonical_json()]
    labels, dist = cluster(reports)
    me = len(reports) - 1
    mates = [i for i in range(me) if labels[i] == labels[me]]

    def gap_m(r) -> float | None:
        t = r.get("triage")
        return haversine_km(triage.lat, triage.lon, t.lat, t.lon) * 1000 if t else None

    def minutes(r) -> float:
        return round(abs((new.received_at - r["incident"].received_at).total_seconds()) / 60, 1)

    candidates = []
    for i, r in enumerate(pool):
        d = gap_m(r)
        clustered = i in mates
        if not clustered and (r["status"] in ("merged", "closed") or d is None or d > RELATED_RADIUS_M):
            continue
        t, loc = r.get("triage"), r["incident"].location
        candidates.append({
            "incident_id": r["duplicate_of"] or r["incident_id"], "minilm_clustered": clustered,
            "adjusted_distance": round(float(dist[me, i]), 3), "distance_m": round(d) if d is not None else None,
            "minutes_apart": minutes(r), "status": r["status"], "reports": r.get("reports", 1),
            "category": t.category if t else None, "severity": t.severity if t else None,
            "description": r["incident"].description, "place": loc.address or loc.landmark or (t.place if t else ""),
        })
    candidates.sort(key=lambda c: (not c["minilm_clustered"], c["adjusted_distance"]))
    candidates = candidates[:4]

    return _rule_decision(new, triage, pool, mates, dist, me, gap_m, minutes), candidates


def _rule_decision(new, triage, pool, mates, dist, me, gap_m, minutes) -> DedupDecision:
    if mates:
        # Merge into the root of the earliest matching report.
        roots = {}
        for i in mates:
            r = pool[i]
            root = r["duplicate_of"] or r["incident_id"]
            roots.setdefault(root, []).append(i)

        def cat(rid):
            t = pool[roots[rid][0]].get("triage")
            return t.category if t else "other"

        # Prefer an incident of a compatible type within the cluster (a fire report that clusters with
        # both a fire and a nearby crash belongs to the fire), then the earliest.
        candidates = [rid for rid in roots if not CATEGORY_GUARD or compatible(triage.category, cat(rid))] or list(roots)
        root = min(candidates, key=lambda rid: min(pool[i]["incident"].received_at for i in roots[rid]))
        closest = min(roots[root], key=lambda i: dist[me, i])
        r = pool[closest]
        d = gap_m(r)
        theirs = cat(root)
        if CATEGORY_GUARD and not compatible(triage.category, theirs):
            return DedupDecision(
                incident_id=new.incident_id, verdict=DedupVerdict.related, match_incident_id=root,
                score=round(float(dist[me, closest]), 3), distance_m=round(d) if d is not None else None,
                minutes_apart=minutes(r), cluster_size=len(mates) + 1, executed_by=ExecutedBy.minilm,
                reason=f"MiniLM clustered it with incident {root} (adjusted distance {float(dist[me, closest]):.2f}), "
                       f"but triage reads {triage.category.replace('_', ' ')} vs {theirs.replace('_', ' ')}: kept as a separate "
                       f"incident so it is still dispatched (category guard).",
            )
        return DedupDecision(
            incident_id=new.incident_id, verdict=DedupVerdict.duplicate, match_incident_id=root,
            score=round(float(dist[me, closest]), 3), distance_m=round(d) if d is not None else None,
            minutes_apart=minutes(r), cluster_size=len(mates) + 1, executed_by=ExecutedBy.minilm,
            reason=f"MiniLM clusters it with {len(mates)} earlier report(s) of incident {root} "
                   f"(adjusted distance {float(dist[me, closest]):.2f} ≤ {THRESHOLD}).",
        )

    # Not the same incident. Is a different active incident close enough to share scene context?
    near = []
    for i, r in enumerate(pool):
        if r["status"] in ("merged", "closed") or r.get("triage") is None:
            continue
        d = gap_m(r)
        if d is not None and d <= RELATED_RADIUS_M:
            near.append((d, i))
    if near:
        d, i = min(near)
        r = pool[i]
        return DedupDecision(
            incident_id=new.incident_id, verdict=DedupVerdict.related, match_incident_id=r["incident_id"],
            score=round(float(dist[me, i]), 3), distance_m=round(d), minutes_apart=minutes(r),
            executed_by=ExecutedBy.minilm,
            reason=f"MiniLM keeps it separate (adjusted distance {float(dist[me, i]):.2f} > {THRESHOLD}); "
                   f"active incident {r['incident_id']} is {d:.0f} m away.",
        )
    best = int(np.argmin(dist[me, :me]))
    return DedupDecision(incident_id=new.incident_id, verdict=DedupVerdict.new, executed_by=ExecutedBy.minilm,
                         score=round(float(dist[me, best]), 3),
                         reason=f"No cluster match among {len(pool)} recent report(s) (closest {float(dist[me, best]):.2f}).")
