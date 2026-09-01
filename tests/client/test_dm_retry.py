"""A WRAP WHOSE FIRST UNWRAP FAILED MUST BE TRIED AGAIN.

Reported as "on desktop, Messages -> DM, it did not load the new message from the user".

`ingestWrap` was built to allow the retry, and does it correctly: `_wrapTried` returns immediately
for a wrap already in flight or done — so nothing is ever decrypted twice — and it DELETES that
entry when the unwrap throws, precisely so a redelivery can try again. A remote signer that times
out or a crypto worker busy verifying the feed is a normal, transient failure.

The live subscription overrode that:

    onEvent: async ev => { if(!Store.saveEvent(ev)) return; await ingestWrap(ev, _dmLive); }

`Store.saveEvent` answers false for an event the Store already holds — and it holds it from the
FIRST delivery, the one that failed. So every redelivery was dropped before the retry could happen
and the message stayed invisible for the rest of the session. Only a reload recovered it, because
`ensureDMs` re-walks the cached wraps.

The Store's dedup answers "have I SEEN this", which is not the question. `ingestWrap` owns "have I
READ this", and it is the one that knows the difference between done and failed.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP_JS = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
RUNTIME = ROOT / "tests/client/dm_retry_runtime.mjs"
NODE = shutil.which("node")


@pytest.mark.skipif(not NODE, reason="needs node")
def test_a_failed_unwrap_is_retried_and_a_good_one_is_not_repeated():
    """Drives the shipped ingestWrap: fail once, redeliver, and the message must arrive — while a
    wrap that succeeded is never decrypted a second time however often the relay replays it."""
    done = subprocess.run([NODE, str(RUNTIME)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ok" in done.stdout


def test_the_live_subscription_does_not_gate_the_unwrap_on_the_store():
    """THE BUG, in one line. The Store's dedup is about having seen the event, not having read it."""
    sub = APP_JS[APP_JS.index("Relay.subscribe([{ kinds:[1059], '#p':[ME.pubkey] }]"):]
    sub = sub[:sub.index("onEose")]
    assert "if(!Store.saveEvent(ev)) return;" not in sub, (
        "the live DM subscription skips the unwrap for any wrap the Store already holds — which "
        "includes every wrap whose first unwrap failed")
    assert "Store.saveEvent(ev); await ingestWrap(ev, _dmLive);" in sub, (
        "the wrap is no longer stored, or no longer ingested")


def test_ingest_still_refuses_a_wrap_it_is_already_working_on():
    """The cost argument for the old guard was real — an unwrap is two round trips to a phone on a
    remote signer. `_wrapTried` is what actually prevents that, and it must stay."""
    fn = APP_JS[APP_JS.index("async function ingestWrap(ev, live){"):]
    fn = fn[:fn.index("\n  }") + 4]
    assert "_wrapTried.has(ev.id)" in fn and "_wrapTried.add(ev.id)" in fn


def test_a_throwing_unwrap_clears_the_retry_latch():
    """Without this the retry cannot happen no matter what the subscription does — a latch set
    BEFORE the attempt it describes, which this codebase has paid for more than once."""
    fn = APP_JS[APP_JS.index("async function ingestWrap(ev, live){"):]
    fn = fn[:fn.index("\n  }") + 4]
    assert "catch(_){ _wrapTried.delete(ev.id);" in fn
