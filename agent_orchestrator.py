"""Track 2: selective Qwen vision triage, Nemotron board and validated arbitration."""
import argparse
import asyncio
import base64
import itertools
import json
import os
from pathlib import Path
from typing import Literal
from pydantic import BaseModel, ConfigDict, Field
from knowledge_base import get_agency_sop
from state_schema import AGENCIES, HERO_CAMERAS, get_initial_city_state, is_unit_available
from sheffield_sim import SheffieldSim

QWEN_MODEL = os.getenv("SURU_QWEN_MODEL", "Qwen3.8-27B")
NEMOTRON_MODEL = os.getenv("SURU_NEMOTRON_MODEL", "Nemotron-3.5-Bolt-E4B")
# Model names are team-specified defaults, not verified Ollama registry tags.
Agency = Literal["FIRE", "POLICE", "AMBULANCE"]


class IncidentReport(BaseModel):
    model_config = ConfigDict(extra="forbid")
    location: str
    incident_type: Literal["flood", "fire", "trauma"]
    severity: int = Field(ge=1, le=10, strict=True)
    road_blocked: bool = Field(strict=True)
    summary: str
    required_agencies: list[Agency] = Field(min_length=1)


class Allocation(BaseModel):
    model_config = ConfigDict(extra="forbid")
    proposal_text: str
    proposed_units: list[str]


def stitch_quad(frames, size=(640, 360)):
    import cv2
    import numpy as np
    tiles = [cv2.resize(frame, size) for frame in list(frames)[:4] if frame is not None]
    tiles += [np.zeros((size[1], size[0], 3), dtype=np.uint8) for _ in range(4-len(tiles))]
    return np.vstack((np.hstack(tiles[:2]), np.hstack(tiles[2:])))


def _post(state, cam, sender, text, units=None, status="PENDING"):
    entry = {"tick": state["tick"], "incident_cam": cam, "sender": sender,
             "proposal_text": text, "proposed_units": units or [], "consensus_status": status}
    state["message_board"].append(entry)
    return entry


def _context(state, cam, sim):
    return json.dumps({"incident": state["active_incidents"][cam],
                       "resource_db": state["resource_db"],
                       "depletion_estimates": sim.resource_estimates(cam),
                       "routing": state["routing"], "blocked_edges": state["blocked_edges"]})


def _frame(video_path, cam_id):
    """Use prepared local frame, otherwise extract the first frame of Cosmos MP4."""
    frame_path = Path(HERO_CAMERAS[cam_id]["frame_path"])
    if frame_path.is_file():
        return base64.b64encode(frame_path.read_bytes()).decode()
    import cv2
    capture = cv2.VideoCapture(str(video_path))
    try:
        ok, frame = capture.read()
        if not ok:
            raise ValueError(f"No readable Cosmos frame: {video_path}")
        ok, data = cv2.imencode(".jpg", frame)
        if not ok:
            raise ValueError("Could not encode CCTV frame")
        return base64.b64encode(data.tobytes()).decode()
    finally:
        capture.release()


async def _ask(client, model, schema, prompt, system, images=None, timeout=20):
    messages = [{"role": "system", "content": system}, {"role": "user", "content": prompt}]
    if images:
        messages[-1]["images"] = images
    response = await asyncio.wait_for(client.chat(model=model, format=schema.model_json_schema(),
                                                 messages=messages), timeout=timeout)
    return schema.model_validate_json(response["message"]["content"])


def _mandatory_agencies(hazard, severity, blocked):
    agencies = ["AMBULANCE" if hazard == "trauma" else "FIRE"]
    if blocked or (severity >= 6 and hazard in ("trauma", "flood")) or (severity >= 7 and hazard == "fire"):
        agencies.append("POLICE")
    return agencies


async def qwen_initial_triage(city_state, cam_id, *, sim=None, live=False, client=None, timeout=20):
    sim = sim or SheffieldSim(city_state)
    incident = city_state["active_incidents"][cam_id]
    if live:
        if client is None:
            from ollama import AsyncClient
            client = AsyncClient()
        image = _frame(incident["video_path"], cam_id)
        report = await _ask(client, QWEN_MODEL, IncidentReport, _context(city_state, cam_id, sim),
                            "Triage this CCTV frame with graph context. Select only agencies needed. "
                            "Return structured report. Location names must refer to the supplied camera. "
                            "Describe uncertainty; local resource constraints never justify inventing observations.",
                            [image], timeout)
    else:
        report = IncidentReport(location=HERO_CAMERAS[cam_id]["name"], incident_type=incident["hazard"],
                                severity=incident["severity"], road_blocked=incident["road_blocked"],
                                summary="OFFLINE synthetic triage; no vision inference performed.",
                                required_agencies=_mandatory_agencies(incident["hazard"], incident["severity"], incident["road_blocked"]))
    required = list(dict.fromkeys(report.required_agencies + _mandatory_agencies(report.incident_type, report.severity, report.road_blocked)))
    incident.update(hazard=report.incident_type, severity=report.severity, road_blocked=report.road_blocked,
                    qwen_report={**report.model_dump(), "required_agencies": required, "timestamp": city_state["tick"]},
                    status="NEMOTRON_NEGOTIATING", consensus_reached=False)
    _post(city_state, cam_id, "QWEN_TRIAGE", report.summary + " | Wake: " + ", ".join(required))
    return incident["qwen_report"]


async def nemotron_negotiate_on_board(city_state, cam_id, *, sim=None, live=False, client=None, timeout=20):
    sim = sim or SheffieldSim(city_state)
    incident = city_state["active_incidents"][cam_id]
    selected = incident["qwen_report"]["required_agencies"]
    if not selected or any(a not in AGENCIES for a in selected):
        raise ValueError("Triage must select known agencies before negotiation")
    if live and client is None:
        from ollama import AsyncClient
        client = AsyncClient()
    context = _context(city_state, cam_id, sim)
    # New round boundaries prevent old proposals from being reused for consensus.
    incident["board_round_start"] = len(city_state["message_board"])

    async def agency_call(agency):
        if live:
            result = await _ask(client, NEMOTRON_MODEL, Allocation, context,
                get_agency_sop(agency) + f" You are NEMOTRON_{agency}. "
                "Propose only your own available units. Consider graph depletion, reserve coverage and other agency requirements.",
                timeout=timeout)
        else:
            eligible = [(key, estimate) for key, estimate in sim.resource_estimates(cam_id).items()
                        if city_state["resource_db"][key]["agency"] == agency and not estimate.get("unreachable")]
            eligible.sort(key=lambda pair: (pair[1]["total_depletion_ticks_remaining"], pair[0]))
            result = Allocation(proposal_text=f"OFFLINE {agency}: nearest feasible round-trip resource; apply simulation SOP.",
                                proposed_units=[eligible[0][0]] if eligible else [])
        if len(result.proposed_units) != len(set(result.proposed_units)) or any(
            key not in city_state["resource_db"] or city_state["resource_db"][key]["agency"] != agency
            or not is_unit_available(city_state["resource_db"][key]) for key in result.proposed_units):
            raise ValueError("Agency proposed duplicate, unavailable or other-agency unit")
        return result

    results = await asyncio.gather(*(agency_call(a) for a in selected), return_exceptions=True)
    incident["negotiation_failed"] = any(isinstance(r, BaseException) for r in results)
    for agency, result in zip(selected, results):
        if isinstance(result, BaseException):
            _post(city_state, cam_id, "NEMOTRON_" + agency, "Model failed or timed out: " + type(result).__name__, status="DEADLOCK_TIMEOUT")
        else:
            _post(city_state, cam_id, "NEMOTRON_" + agency, result.proposal_text, result.proposed_units)
    return results


def _offline_arbiter(sim, cam):
    estimates = sim.resource_estimates(cam)
    candidates = [key for key, estimate in estimates.items() if not estimate.get("unreachable")]
    choices = []
    for size in range(1, len(candidates)+1):
        for combination in itertools.combinations(candidates, size):
            units = list(combination)
            if sim.validate_allocation(cam, units)[0]:
                choices.append(units)
        if choices:
            return min(choices, key=lambda ids: (sum(estimates[u]["total_depletion_ticks_remaining"] for u in ids), ids))
    return []


async def resolve_board_or_qwen_takeover(city_state, cam_id, *, sim=None, live=False, client=None, timeout=20):
    sim = sim or SheffieldSim(city_state)
    incident = city_state["active_incidents"][cam_id]
    if incident["status"] in ("UNITS_DISPATCHED", "RESOLVED"):
        return incident.get("dispatched_units", [])
    board = [entry for entry in city_state["message_board"][incident.get("board_round_start", len(city_state["message_board"])):]
             if entry["incident_cam"] == cam_id and entry["sender"].startswith("NEMOTRON_")]
    expected = {"NEMOTRON_"+a for a in incident["qwen_report"]["required_agencies"]}
    units = [unit for entry in board for unit in entry["proposed_units"]]
    valid, reason = sim.validate_allocation(cam_id, units)
    agreed = valid and not incident.get("negotiation_failed", True) and {e["sender"] for e in board} == expected and len(board) == len(expected)
    if agreed:
        sim.dispatch(cam_id, units)
        incident["consensus_reached"] = True
        for entry in board:
            entry["consensus_status"] = "AGREED"
        return units
    incident["status"] = "QWEN_ARBITRATING"
    incident["consensus_reached"] = False
    for entry in board:
        entry["consensus_status"] = "DEADLOCK_TIMEOUT"
    if live:
        if client is None:
            from ollama import AsyncClient
            client = AsyncClient()
        try:
            decision = await _ask(client, QWEN_MODEL, Allocation,
                _context(city_state, cam_id, sim) + " Shared board: " + json.dumps(board),
                "You are QWEN_GOLD_ARBITER. Issue a binding allocation satisfying all agency, reserve and availability rules. "
                + " ".join(get_agency_sop(a) for a in AGENCIES), timeout=timeout)
        except Exception as error:
            _post(city_state, cam_id, "QWEN_GOLD_ARBITER", "Arbitration failed; dispatch deferred: " + type(error).__name__, status="DEADLOCK_TIMEOUT")
            return []
    else:
        decision = Allocation(proposal_text="OFFLINE arbiter: smallest feasible allocation by total round-trip depletion.",
                              proposed_units=_offline_arbiter(sim, cam_id))
    valid, reason = sim.validate_allocation(cam_id, decision.proposed_units)
    if not valid:
        _post(city_state, cam_id, "QWEN_GOLD_ARBITER", "Dispatch deferred: " + reason, decision.proposed_units, "DEADLOCK_TIMEOUT")
        return []
    sim.dispatch(cam_id, decision.proposed_units)
    _post(city_state, cam_id, "QWEN_GOLD_ARBITER", decision.proposal_text, decision.proposed_units, "QWEN_OVERRIDDEN")
    return decision.proposed_units


async def process_pending(city_state, *, sim=None, live=False, client=None, timeout=20):
    sim = sim or SheffieldSim(city_state)
    for cam, incident in list(city_state["active_incidents"].items()):
        if incident["status"] in ("UNITS_DISPATCHED", "RESOLVED"):
            continue
        try:
            if incident["status"] == "AWAITING_QWEN_TRIAGE":
                await qwen_initial_triage(city_state, cam, sim=sim, live=live, client=client, timeout=timeout)
            await nemotron_negotiate_on_board(city_state, cam, sim=sim, live=live, client=client, timeout=timeout)
            await resolve_board_or_qwen_takeover(city_state, cam, sim=sim, live=live, client=client, timeout=timeout)
        except Exception as error:
            _post(city_state, cam, "QWEN_TRIAGE", "Pipeline deferred: " + type(error).__name__, status="DEADLOCK_TIMEOUT")
    return city_state


# Preserve the previous starter entry point while migrating its state contract.
async def run_command(state, image_path=None, model=None, live=False):
    if image_path or model:
        raise ValueError("Use camera frame paths in state_schema and SURU_QWEN_MODEL instead")
    return await process_pending(state, live=live)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--ticks", type=int, default=3)
    args = parser.parse_args()
    sim = SheffieldSim(get_initial_city_state(), demo=True)
    for _ in range(args.ticks):
        sim.tick()
        asyncio.run(process_pending(sim.state, sim=sim, live=args.live))
    print(json.dumps(sim.state, indent=2))
