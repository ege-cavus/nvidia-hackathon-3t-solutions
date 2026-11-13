"""Verify the console submits caller facts in the shared JSON contract."""
import json
from pathlib import Path
import shutil
import subprocess

import pytest
from jsonschema import validate
from urban_response.schemas import Incident

ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.skipif(shutil.which("node") is None, reason="Node is needed for the console serializer check")
def test_console_form_preserves_unknowns_and_emits_canonical_report():
    html=(ROOT/"urban_response/static/index.html").read_text(encoding="utf-8")
    serializer=html[html.index("function nullableNumber"):html.index("function initCallForm")]
    values={"channel":"999", "description":"Smoke in the stairwell", "address":"", "landmark":"Park Hill flats",
            "lat":"", "lon":"", "caller_reported_severity":"", "casualties_reported":"",
            "people_trapped_reported":"", "hazards_reported":"smoke, blocked exit", "weapons_mentioned":""}
    script=serializer+"\nconst values="+json.dumps(values)+";\n"+'''
const form={elements:{namedItem:(name)=>({value:values[name]})}};
console.log(JSON.stringify(canonicalCall(form)));
values.lat="53.38";
try {canonicalCall(form);throw new Error("unpaired coordinate accepted");}
catch(error) {if (!error.message.includes("both coordinates")) throw error;}
'''
    result=subprocess.run([shutil.which("node"),"-e",script],capture_output=True,text=True,check=True)
    report=json.loads(result.stdout)
    schema=json.loads((ROOT/"schemas/incident.schema.json").read_text(encoding="utf-8"))
    validate({**report,"incident_id":20261113000001,"received_at":"2026-11-13T09:00:00Z"},schema)
    incident=Incident.model_validate(report)
    assert incident.location.landmark=="Park Hill flats"
    assert incident.casualties_reported is None and incident.people_trapped_reported is None
    assert incident.weapons_mentioned is None and incident.location.lat is None
    assert "category" not in report and "severity" not in report
    assert report["hazards_reported"]==["smoke","blocked exit"]
