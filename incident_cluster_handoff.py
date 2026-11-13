"""Cluster JSON emergency reports into potential underlying events (POC, not dispatch authority).

Input: JSON array, JSON object with an 'incidents' array, or JSONL.
No ground-truth event_id is required; if provided it is used only for evaluation.
"""
import argparse
import json
import math
from collections import Counter
from datetime import datetime
from pathlib import Path

import numpy as np
from sentence_transformers import SentenceTransformer
from sklearn.cluster import AgglomerativeClustering
from sklearn.metrics import adjusted_rand_score
from sklearn.metrics.pairwise import cosine_distances


def read_incidents(path):
    content = Path(path).read_text(encoding="utf-8-sig")
    if path.lower().endswith(".jsonl"):
        incidents = [json.loads(line) for line in content.splitlines() if line.strip()]
    else:
        payload = json.loads(content)
        incidents = payload.get("incidents") if isinstance(payload, dict) else payload
    if not isinstance(incidents, list) or len(incidents) < 2:
        raise ValueError("Input must contain at least two incidents in a JSON array or JSONL")
    for i, incident in enumerate(incidents):
        if not isinstance(incident, dict) or not str(incident.get("description") or "").strip():
            raise ValueError(f"Incident at index {i} has no nonempty description")
    return incidents


def normalise_place(value):
    if not value:
        return ""
    import re
    return re.sub(r"[^a-z0-9]+", " ", str(value).lower()).strip()


def coordinates(location):
    if not isinstance(location, dict):
        return None
    lat, lon = location.get("lat"), location.get("lon")
    if lat is None or lon is None:
        return None
    try:
        lat, lon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (-90 <= lat <= 90 and -180 <= lon <= 180):
        return None
    return lat, lon


def haversine_km(a, b):
    r = 6371.0088
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dp, dl = p2 - p1, math.radians(b[1] - a[1])
    x = math.sin(dp / 2)**2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2)**2
    return 2 * r * math.asin(min(1.0, math.sqrt(x)))


def location_relation(a, b, radius_km):
    """Return 'match', 'conflict' or 'unknown'. String comparisons are exact only."""
    a, b = a.get("location") or {}, b.get("location") or {}
    if not isinstance(a, dict) or not isinstance(b, dict):
        return "unknown"
    ac, bc = coordinates(a), coordinates(b)
    if ac and bc:
        return "match" if haversine_km(ac, bc) <= radius_km else "conflict"
    # Only compare the same kind of textual place; a landmark vs address is ambiguous.
    for key in ("address", "landmark"):
        av, bv = normalise_place(a.get(key)), normalise_place(b.get(key))
        if av and bv:
            return "match" if av == bv else "conflict"
    return "unknown"


def parsed_time(incident):
    value = incident.get("received_at")
    if not value:
        return None
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return result if result.tzinfo else None
    except (ValueError, AttributeError):
        return None


def registration_set(incident):
    return {normalise_place(v.get("registration")) for v in (incident.get("vehicles") or [])
            if isinstance(v, dict) and normalise_place(v.get("registration"))}


def pairwise_scores(truth, predicted):
    t, p = np.asarray(truth), np.asarray(predicted)
    upper = np.triu_indices(len(t), 1)
    st = (t[:, None] == t[None, :])[upper]
    sp = (p[:, None] == p[None, :])[upper]
    tp, fp, fn = np.sum(st & sp), np.sum(~st & sp), np.sum(st & ~sp)
    precision = float(tp / (tp + fp)) if tp + fp else 0.0
    recall = float(tp / (tp + fn)) if tp + fn else 0.0
    return precision, recall, 2 * precision * recall / (precision + recall) if precision + recall else 0.0


def build_distances(incidents, embeddings, args):
    dist = cosine_distances(embeddings).astype(float)
    np.fill_diagonal(dist, 0.0)
    n = len(incidents)
    for i in range(n):
        for j in range(i + 1, n):
            a, b = incidents[i], incidents[j]
            score = float(dist[i, j])
            relation = location_relation(a, b, args.radius_km)
            if relation == "conflict":
                # A strong penalty rather than an unconditional merge ban.
                score += args.location_penalty
            elif relation == "match":
                score = max(0.0, score - args.location_bonus)
            ta, tb = parsed_time(a), parsed_time(b)
            if ta is not None and tb is not None:
                minutes = abs((ta - tb).total_seconds()) / 60.0
                if minutes > args.time_window_minutes:
                    score += args.time_penalty
            # Matching registration is positive evidence, but distinct registrations
            # do not imply distinct events (multi-vehicle collisions).
            regs_a, regs_b = registration_set(a), registration_set(b)
            if regs_a and regs_b and regs_a.intersection(regs_b):
                score = max(0.0, score - args.registration_bonus)
            dist[i, j] = dist[j, i] = score
    return dist



def summarise_cluster(members, embeddings, max_sentences=2):
    """Extract representative original descriptions; never generate new claims."""
    indices = [index for index, _ in members]
    texts = [incident["description"].strip() for _, incident in members]
    local = embeddings[indices]
    similarities = np.asarray(local @ local.T)
    # Central report is most similar to other reports in the event.
    central = int(np.argmax(similarities.mean(axis=1)))
    selected = [central]
    while len(selected) < min(max_sentences, len(texts)):
        # Prefer representative details not redundant with selected sentences.
        candidates = [i for i in range(len(texts)) if i not in selected]
        candidate = max(candidates, key=lambda i: (
            float(similarities[i].mean()) - 0.6 * max(float(similarities[i, j]) for j in selected),
            -i
        ))
        selected.append(candidate)
    return " ".join(texts[i] for i in selected), [indices[i] for i in selected]


def event_payload(cluster_id, members, embeddings, max_sentences):
    from collections import Counter
    reports = [incident for _, incident in members]
    summary, selected_indices = summarise_cluster(members, embeddings, max_sentences)
    locations = [r.get("location") or {} for r in reports]
    location_values = [
        str(loc.get(key)).strip()
        for loc in locations if isinstance(loc, dict)
        for key in ("address", "landmark") if loc.get(key)
    ]
    most_common_location = Counter(location_values).most_common(1)
    times = [parsed_time(r) for r in reports]
    times = [t for t in times if t is not None]
    source_ids = [r["incident_id"] for r in reports]
    return {
        "cluster_id": cluster_id,
        "status": "potential_event_requires_review",
        "report_count": len(reports),
        "summary": summary,
        "summary_source_report_ids": [
            reports[[i for i, _ in members].index(index)]["incident_id"]
            for index in selected_indices
        ],
        "location_label": most_common_location[0][0] if most_common_location else None,
        "locations_reported": sorted(set(location_values)),
        "first_received_at": min(times).isoformat() if times else None,
        "last_received_at": max(times).isoformat() if times else None,
        "source_report_ids": source_ids,
    }

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", default="incidents.json", help="JSON array, {'incidents': [...]}, or JSONL")
    parser.add_argument("--output", default="clustered_incidents.json")
    parser.add_argument("--threshold", type=float, default=0.65)
    parser.add_argument("--model", default="sentence-transformers/all-MiniLM-L6-v2")
    parser.add_argument("--radius-km", type=float, default=0.5)
    parser.add_argument("--location-penalty", type=float, default=1.0)
    parser.add_argument("--location-bonus", type=float, default=0.10)
    parser.add_argument("--time-window-minutes", type=float, default=30)
    parser.add_argument("--time-penalty", type=float, default=0.35)
    parser.add_argument("--registration-bonus", type=float, default=0.10)
    parser.add_argument("--semantic-only", action="store_true")
    parser.add_argument("--summary-sentences", type=int, default=2, help="Number of verbatim report descriptions per summary")
    parser.add_argument("--reports-output", default=None, help="Optional path for individual reports with cluster IDs")
    args = parser.parse_args()
    if args.summary_sentences < 1:
        parser.error("--summary-sentences must be >= 1")
    if not 0 < args.threshold < 2.0:
        parser.error("--threshold must be between 0 and 2")
    incidents = read_incidents(args.input)
    ids = [x.get("incident_id") for x in incidents]
    if any(x is None for x in ids) or len(set(map(str, ids))) != len(ids):
        raise ValueError("Every report needs a unique incident_id")
    model = SentenceTransformer(args.model)
    vectors = model.encode([x["description"] for x in incidents], normalize_embeddings=True)
    distances = cosine_distances(vectors) if args.semantic_only else build_distances(incidents, vectors, args)
    np.fill_diagonal(distances, 0)
    # Complete linkage prevents chains of compatible pairs from bridging incompatible reports.
    labels = AgglomerativeClustering(n_clusters=None, distance_threshold=args.threshold,
                                     metric="precomputed", linkage="complete").fit_predict(distances)
    # Stable cluster identifiers based on first occurrence (sklearn labels can change).
    mapping = {}
    clusters = []
    for label in labels:
        mapping.setdefault(int(label), f"EVENT-{len(mapping) + 1:03d}")
        clusters.append(mapping[int(label)])
    results = [{**incident, "cluster_id": cluster} for incident, cluster in zip(incidents, clusters)]
    grouped = {}
    for index, (incident, cluster_id) in enumerate(zip(incidents, clusters)):
        grouped.setdefault(cluster_id, []).append((index, incident))
    events = [event_payload(cid, members, vectors, args.summary_sentences)
              for cid, members in grouped.items()]
    handoff = {
        "schema_version": "1.0",
        "simulation_only": True,
        "total_reports": len(incidents),
        "total_events": len(events),
        "clustering_threshold": args.threshold,
        "events": events,
    }
    Path(args.output).write_text(json.dumps(handoff, indent=2, ensure_ascii=False), encoding="utf-8")
    if args.reports_output:
        Path(args.reports_output).write_text(json.dumps(results, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"Reports: {len(incidents)} | Potential events: {len(mapping)} | Threshold: {args.threshold}")
    for cluster_id in dict.fromkeys(clusters):
        members = [r for r in results if r["cluster_id"] == cluster_id]
        print(f"{cluster_id}: {len(members)} reports; sample: {members[0]['description']}")
    if all("event_id" in x for x in incidents):
        truth = [x["event_id"] for x in incidents]
        precision, recall, f1 = pairwise_scores(truth, labels)
        print(f"Evaluation only: ARI={adjusted_rand_score(truth, labels):.3f}, "
              f"pair precision={precision:.3f}, recall={recall:.3f}, F1={f1:.3f}")
    print(f"Saved {args.output}")


if __name__ == "__main__":
    main()
