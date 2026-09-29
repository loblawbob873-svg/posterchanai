"""A git issue a relay STORED is never reported as a failure, and a retry never files a second copy.

Reported twice on the PosterChan repo: "At publish some issue on git I get the message 'timeout' but
the issue was published already" (the reporter pressed again and four identical issues landed seconds
apart), and "I get it for close issues too" -- Close/Resolve showed "timeout" AND "relay: timeout"
while the issue closed. The relay answers OK in 1-4 ms and stored every one of those events; what the
phone lacked was the OK within Relay.publish's eight seconds. Silence was reported as failure, and the
issue was never even offered to the relays the repo's own announcement names.

Drives the SHIPPED relay.js + git.js under node (tests/client/git_issue_publish_runtime.cjs) with
stub sockets per relay. Against the pre-fix pair: `repo_stores`, `late_ok` and `relay_unconfirmed`
fail, and `silence` fails on the message (the same-id retry itself was already fixed on 2026-09-27
and is kept here as a guard). `double_tap` and `refusal` are guards that pass on both.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CLIENT = ROOT / "static" / "js" / "client"
DRIVER = Path(__file__).with_name("git_issue_publish_runtime.cjs")


@pytest.fixture(scope="module")
def out():
    node = shutil.which("node")
    if not node:
        pytest.skip("node not installed")
    r = subprocess.run([node, str(DRIVER), str(CLIENT / "relay.js"), str(CLIENT / "git.js")],
                       capture_output=True, text=True, timeout=120)
    assert r.returncode == 0, r.stderr[-3000:]
    return json.loads(r.stdout.strip().splitlines()[-1])


def _no_false_failure(text):
    assert "timeout" not in text.lower(), f"a stored or unknown outcome was reported as 'timeout': {text!r}"
    assert "relay:" not in text, f"an unanswered publish was reported as a relay error: {text!r}"


def test_repo_stores_while_the_pool_is_silent_is_a_published_issue(out):
    a = out["repoStores"]
    assert a["reachedRepo"], "the issue was never sent to the relays the repo announcement names"
    assert a["published"] and a["closed"] == 1, f"stored on the repo's relay and still not published: {a}"
    _no_false_failure(a["said"])


def test_late_ok_turns_not_confirmed_into_published(out):
    b = out["late"]
    assert "not confirmed" in b["first"].lower(), b
    _no_false_failure(b["first"])
    assert b["closedAtFirst"] == 0
    assert b["closedLater"] == 1 and b["published"], f"the relay's late OK never reached the form: {b}"
    assert b["savedLocally"], "the confirmed issue was left out of the local store"


def test_silence_says_not_confirmed_and_a_retry_resends_the_same_event(out):
    c = out["silent"]
    assert "not confirmed" in c["first"].lower(), c
    _no_false_failure(c["first"])
    assert c["enabled"], "Publish stayed disabled after an unconfirmed send"
    assert c["closed"] == 0
    assert c["ids"] == 1, f"a retry signed a NEW issue -- a duplicate once the first lands: {c}"
    assert c["frames"] >= 4, f"the retry did not reach both the pool and the repo relay again: {c}"


def test_double_tap_is_one_event(out):
    d = out["doubleTap"]
    assert d["ids"] == 1 and d["poolFrames"] == 1, d
    assert d["closed"] == 1


def test_a_real_refusal_is_still_reported_with_its_reason(out):
    e = out["refused"]
    assert e["said"] == "relay: blocked: not in web of trust", e
    assert e["closed"] == 0


def test_relay_publish_reports_silence_as_unconfirmed_with_a_late_answer(out):
    r = out["relay"]
    assert r["ok"] is False, "an unknown outcome must never read as ok (callers gate destructive steps on it)"
    assert r["unconfirmed"] and r["hasLate"], r
    assert r["late"] is True, "a late OK did not settle the unconfirmed publish"
    assert r["refused"] == [False, "blocked: nope", False], "a refusal is an answer, not 'unconfirmed'"
