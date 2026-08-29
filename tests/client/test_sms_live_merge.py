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


def test_cold_web_load_drains_full_history_and_keeps_old_conversation_media():
    """The first cache paint is intentionally bounded.  Media older than that first page must
    still be merged into the open conversation by the background drain, without a second visit."""
    cached = []
    for i in range(70):
        doc = "pcai:sms:cold-%019d" % i
        body = {"address": "+15550100", "body": "message %d" % i,
                "date": 70_000 - i * 1000, "incoming": True}
        if i == 69:
            body["att"] = [{"ct": "image/jpeg", "name": "oldest.jpg", "bytes": 42,
                            "sha": "f" * 64, "thumb": "e" * 64}]
        cached.append({"kind": 30078, "created_at": 1000 - i,
                       "tags": [["d", doc]], "content": "enc:" + json.dumps(body)})

    payload = {"isPhone": False, "telephony": False, "cached": cached,
               "relayEmpty": True, "steps": ["render", "settle"]}
    run = subprocess.run(["node", str(SIM), json.dumps(payload)], cwd=ROOT, text=True,
                         capture_output=True, timeout=30)
    assert run.returncode == 0, run.stderr
    result = json.loads(run.stdout.strip().splitlines()[-1])
    assert result["threads"][0]["n"] == 70
    assert result["threads"][0]["parts"][0] == 1
    assert result["threads"][0]["partShas"][0] == ["f" * 64]
