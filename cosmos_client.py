"""Generate pre-made report images through a running Cosmos3 Generator NIM."""
import argparse
import base64
from io import BytesIO
import json
from pathlib import Path
import urllib.request
from PIL import Image

ROOT=Path(__file__).resolve().parent
DEFAULT_MANIFEST=ROOT/"assets/scenarios/sheffield_reports_demo/media_manifest.json"


def image_request(item, seed=42, resolution="480_16_9"):
    if type(item["incident_id"]) is not int or item["incident_id"]<1:
        raise ValueError("Incident ID must be a positive integer")
    if not isinstance(item["prompt"],str) or not item["prompt"].strip():
        raise ValueError("Image prompt is required")
    if type(seed) is not int or seed<0:
        raise ValueError("Seed must be a nonnegative integer")
    return {"model_mode":"text2image","prompt":item["prompt"],"resolution":resolution,
            "num_frames":1,"seed":seed}


def generate_image(item, endpoint="http://127.0.0.1:8000", output_dir=None, overwrite=False,
                   seed=42, resolution="480_16_9", timeout=1800):
    payload=image_request(item,seed,resolution)
    directory=Path(output_dir) if output_dir is not None else ROOT/"assets/incident_media"
    directory.mkdir(parents=True,exist_ok=True)
    destination=directory/f"{item['incident_id']}.jpg"
    if destination.exists() and not overwrite:
        return {"incident_id":item["incident_id"],"path":str(destination),"status":"SKIPPED_EXISTING"}
    ready=endpoint.rstrip('/')+"/v1/health/ready"
    with urllib.request.urlopen(ready,timeout=10) as response:
        if response.status!=200:raise RuntimeError("Cosmos Generator is not ready")
    request=urllib.request.Request(endpoint.rstrip('/')+"/v1/infer",json.dumps(payload).encode(),
                                   {"Content-Type":"application/json"})
    # No automatic retry: a timed-out synchronous request may still be generating.
    with urllib.request.urlopen(request,timeout=timeout) as response:
        result=json.load(response)
    encoded=result.get("b64_image")
    if not isinstance(encoded,str):raise ValueError("Generator did not return b64_image")
    data=base64.b64decode(encoded,validate=True)
    with Image.open(BytesIO(data)) as image:
        if image.format!="JPEG":raise ValueError("Expected a JPEG from Cosmos")
        size=image.size
        image.verify()
    temporary=destination.with_suffix('.jpg.tmp')
    temporary.write_bytes(data)
    temporary.replace(destination)
    return {"incident_id":item["incident_id"],"path":str(destination),"status":"GENERATED",
            "width":size[0],"height":size[1],"bytes":len(data)}


if __name__=="__main__":
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--manifest",type=Path,default=DEFAULT_MANIFEST)
    parser.add_argument("--endpoint",default="http://127.0.0.1:8000")
    parser.add_argument("--incident-id",type=int)
    parser.add_argument("--output-dir",type=Path)
    parser.add_argument("--overwrite",action="store_true")
    parser.add_argument("--seed",type=int,default=42)
    parser.add_argument("--resolution",default="480_16_9")
    parser.add_argument("--dry-run",action="store_true")
    args=parser.parse_args()
    items=json.loads(args.manifest.read_text())["items"]
    if args.incident_id is not None:items=[i for i in items if i["incident_id"]==args.incident_id]
    if not items:parser.error("No matching incident in manifest")
    for item in items:
        if args.dry_run:
            print(json.dumps({"incident_id":item["incident_id"],"request":image_request(item,args.seed,args.resolution)}))
        else:
            print(json.dumps(generate_image(item,args.endpoint,args.output_dir,args.overwrite,args.seed,args.resolution)),flush=True)
