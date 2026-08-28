import json
import pathlib
import subprocess


ROOT = pathlib.Path(__file__).resolve().parents[2]
SIM = ROOT / "tests/client/sms_sim.js"


def test_slow_historical_version_cannot_overwrite_new_live_mms():
    doc = "pcai:sms:" + "a" * 24
    old = {"address": "+15550100", "body": "old", "date": 1000, "incoming": True}
    new = {"address": "+15550100", "body": "new", "date": 2000, "incoming": True,
           "att": [{"ct": "image/jpeg", "name": "photo.jpg", "bytes": 42,
                    "sha": "b" * 64, "nt": 1}]}
    events = [
        {"created_at": 10, "tags": [["d", doc]], "content": "enc:" + json.dumps(old)},
        {"created_at": 11, "tags": [["d", doc]], "content": "enc:" + json.dumps(new)},
    ]
    # Make the historical body finish after the live MMS, reproducing the subscription race.
    payload = {"steps": ["absorbConcurrent"], "rawEvents": events,
               "decryptDelays": {"\"body\": \"old\"": 30}}
    run = subprocess.run(["node", str(SIM), json.dumps(payload)], cwd=ROOT, text=True,
                         capture_output=True, timeout=30)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout.strip().splitlines()[-1])
    assert result["docs"] == [doc]
    assert result["threads"][0]["bodies"] == ["new"]
    assert result["threads"][0]["parts"] == [1]
