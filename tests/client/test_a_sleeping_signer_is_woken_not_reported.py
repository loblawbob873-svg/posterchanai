"""AN MV3 WORKER THAT IS ASLEEP IS NOT AN EXTENSION THAT IS GONE.

Reported as "why is the signer extension so shit, I can't fucking send any posts from webui now",
with the console showing:

    theme sync skipped: could not establish your app session:
      signEvent failed (0 bytes): Could not establish connection. Receiving end does not exist.
    [monero wallet] no app session — the Nostr signer extension is not responding

and resolved by the person having to log out and log back in.

The browser evicts an idle background service worker, and the FIRST message after that eviction
fails with exactly that error -- while being the very thing that wakes it. Everything authenticated
goes through one call, so a single failed attempt took the whole app session down: theme, wallet,
posting. `extension/content.js` already retries this, but an INSTALLED extension is whatever the
store last handed out; the client half is ours to fix today and works against every version.

Only that error is retried, and it is safe precisely because of what it means: the message never
arrived, so nothing was signed and nothing can be signed twice. A refusal, a timeout, and an
orphaned content script are different answers and none of them is retried.

The shipped `_extGate` is RUN here, because the question is how many times it calls the signer.
"""
import json
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _run():
    out = subprocess.run(["node", str(ROOT / "tests/client/ext_gate_sim.mjs")],
                         cwd=ROOT, text=True, capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr
    return json.loads(out.stdout)


def test_a_worker_that_wakes_on_the_second_try_is_not_a_failure():
    r = _run()
    assert r["wokeUp"] == "signed"
    assert r["wokeUpAttempts"] == 2


def test_it_gives_up_rather_than_looping():
    r = _run()
    assert r["deadResult"] == "threw"
    assert r["deadAttempts"] == 3


def test_a_refusal_is_an_answer_and_is_not_retried():
    """Retrying a denial would re-prompt the person for something they just declined."""
    r = _run()
    assert r["refusedAttempts"] == 1
    assert r["refusedMsg"] == "User rejected", r


def test_an_orphaned_content_script_says_to_reload():
    """Nothing it sends will ever arrive; only a page reload fixes it, so say so."""
    r = _run()
    assert r["orphanAttempts"] == 1
    assert "reload" in r["orphanMsg"].lower(), r


def test_the_ordinary_path_still_costs_one_call():
    r = _run()
    assert r["plain"] == "ok" and r["plainAttempts"] == 1


def test_identical_concurrent_asks_still_collapse_to_one():
    """The de-duplication is what stops a burst of decrypts opening a window each."""
    r = _run()
    assert r["dedupe"] is True and r["dedupeRuns"] == 1
