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


def test_live_subscription_adds_old_media_without_reopening_texts():
    """Web/PosterChanOS has no phone provider to poll. Once Texts is open, a handset's archived
    MMS must enter through the live subscription and retain its portable encrypted-media hash."""
    doc = "pcai:sms:" + "c" * 24
    media = {"address": "+15550100", "body": "older photo", "date": 1000,
             "incoming": True,
             "att": [{"ct": "image/jpeg", "name": "old.jpg", "bytes": 42,
                      "sha": "d" * 64, "thumb": "e" * 64}]}
    event = {"created_at": 20, "tags": [["d", doc]], "content": "enc:" + json.dumps(media)}
    payload = {"isPhone": False, "telephony": False,
               "steps": ["render", "liveEvent"], "rawEvents": [event]}
    run = subprocess.run(["node", str(SIM), json.dumps(payload)], cwd=ROOT, text=True,
                         capture_output=True, timeout=30)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout.strip().splitlines()[-1])
    assert result["docs"] == [doc]
    assert result["threads"][0]["parts"] == [1]
    assert result["threads"][0]["partShas"] == [["d" * 64]]
