"""Track 2: quad-view and async Gold/Bronze scaffolding. Default CLI is offline."""
import argparse
import asyncio
import json
from state_schema import get_initial_city_state


def summarize_units(state: dict) -> str:
    """Expose geography and lockouts to every command role."""
    return json.dumps({key: {field: unit[field] for field in
        ("home_hub", "lat", "lon", "status", "total_busy_ticks")}
        for key, unit in state["units"].items()}, sort_keys=True)


def stitch_quad(frames, size=(640, 360)):
    """Resize up to four BGR frames into a 2x2 monitor; pad missing feeds black."""
    import cv2
    import numpy as np
    tiles = [cv2.resize(frame, size) for frame in list(frames)[:4] if frame is not None]
    tiles += [np.zeros((size[1], size[0], 3), dtype=np.uint8) for _ in range(4 - len(tiles))]
    return np.vstack((np.hstack(tiles[:2]), np.hstack(tiles[2:])))


async def run_command(state: dict, image_path=None, model="llama3.2-vision", live=False):
    """Gold triages a supplied quad image; three Bronze agents advise in parallel.

    Advice is logged only. TODO: validate structured triage before changing incidents.
    """
    coverage = summarize_units(state)
    instruction = (" Debate geographic coverage gaps and turnaround lockouts. "
                   "Dispatch only IDLE_AT_BASE units with total_busy_ticks == 0. Units: " + coverage)
    if live:
        from ollama import AsyncClient
        client = AsyncClient()
        content = {"role": "user", "content": "Triage this Sheffield CCTV quad. Describe visible flood, fire or trauma; acknowledge uncertainty." + instruction}
        if image_path:
            content["images"] = [str(image_path)]
        else:
            raise ValueError("Live vision requires --image with a prepared CCTV quad")
        gold = (await client.chat(model=model, messages=[content]))["message"]["content"]
    else:
        gold = "DEMO: synthetic storm; review flood access and emergency coverage. " + instruction
    state["comms_log"].append({"timestamp": str(state["tick"]), "sender": "GOLD_COMMAND", "message": gold})

    async def bronze(role):
        if live:
            response = await client.chat(model=model, messages=[{
                "role": "user", "content": f"You are {role}. Give concise tactical advice from Gold triage: {gold}. {instruction}"}])
            message = response["message"]["content"]
        else:
            message = f"DEMO {role}: assess available units and safe access. {instruction}"
        return {"timestamp": str(state["tick"]), "sender": role, "message": message}

    state["comms_log"].extend(await asyncio.gather(*(bronze(r) for r in ("BRONZE_POLICE", "BRONZE_FIRE", "BRONZE_NHS"))))
    return state


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--live", action="store_true")
    parser.add_argument("--image")
    parser.add_argument("--model", default="llama3.2-vision")
    args = parser.parse_args()
    print(json.dumps(asyncio.run(run_command(get_initial_city_state(), args.image, args.model, args.live)), indent=2))
