"""A SIGNER REQUEST THAT WAS IN FLIGHT WHEN THE RELAY DIED MUST BE RELEASED, NOT WAITED OUT.

Reported as four symptoms of one bug, all after a node restart — which is what every deploy does to
every logged-in desktop:

    "every time you restart, desktop takes a long time to recover and post"
    "waiting for signer, queue builds up"
    "i just seen signer request timed out"
    "i have a bunch of drafts now"

Kind 24133 is EPHEMERAL. This relay stores nothing and fans a request out only to whoever is
listening at that instant, so once the last socket is gone a request already sent and unanswered
CANNOT be answered — there is no copy anywhere and the reply has nowhere to land. `ws.onclose`
redialled the socket and did nothing about `_pending`, so those requests sat there holding a slot
until their ceiling: 120s for a signature, 45s for a decrypt.

The interactive lane is TWO slots wide (`_capP`). Two dead requests therefore block every signature
for two minutes — and a publish gives the relay 8s before the post is filed, which is where the
drafts came from. Only `reset()` failed pending requests, and that is a full session teardown; the
user was reaching it by reloading the page, which is the workaround this replaces.

The three cases below are the whole rule, and the middle two are why this is not simply "clear
pending on close": requests are fanned out to EVERY relay the session holds, so while any socket
survives the reply may still arrive on it.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
RUNTIME = Path(__file__).with_name("nip46_pending_runtime.mjs")
NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(not NODE, reason="node unavailable")

OPEN, CONNECTING, CLOSED = 1, 0, 3


def _node(script: str):
    done = subprocess.run([NODE, "--input-type=module", "-e", script], cwd=ROOT,
                          capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def close_with(*others):
    """Close one socket while `others` remain, and report what happened to the pending requests."""
    socks = [{"readyState": CLOSED}] + [{"readyState": r} for r in others]
    return _node(f"""
        import {{ session }} from './tests/client/nip46_pending_runtime.mjs';
        console.log(JSON.stringify(session({{ sockets: {json.dumps(socks)} }})));
    """)


def test_the_last_socket_dying_releases_every_outstanding_request():
    """THE BUG. Nothing can answer them, so holding the lane for two minutes buys nothing and costs
    the user every signature in that window."""
    got = close_with()
    assert got["pending"] == 0, (
        "requests outstanding when the last socket died are still held — this is the two minutes of "
        "a dead Post button after a restart")
    assert len(got["rejected"]) == 2


def test_a_surviving_open_socket_keeps_them():
    """Requests go to EVERY relay the session holds. While one is up the reply may still land on
    it, and failing would turn a working signer into a spurious retry."""
    assert close_with(OPEN)["pending"] == 2


def test_a_socket_still_dialling_counts_as_a_socket():
    """The race this nearly introduced: `_openAll` opens every relay it knows, and a dead one
    closing while a good one is still CONNECTING would read as 'all gone' and kill the connect
    request that pairing itself is waiting on — breaking login to fix posting."""
    assert close_with(CONNECTING)["pending"] == 2, (
        "a socket in the middle of dialling is being treated as no socket at all")


def test_the_socket_is_still_redialled():
    """Releasing the requests must not replace the reconnect — the session still wants that relay."""
    assert close_with()["reopened"] == 1


def test_the_failure_is_one_the_sender_will_retry():
    """The whole point is that the retry re-sends: `_send` treats only a USER REFUSAL as final, and
    reconnects through `_ensure` for anything else. A wording that matched a refusal phrase here
    would release the slot and then throw the user's post away — worse than the bug."""
    final = _node("""
        import { finalPhrases } from './tests/client/nip46_pending_runtime.mjs';
        console.log(JSON.stringify(finalPhrases()));
    """)
    assert final, "the refusal list could not be read out of _send — re-read this test"
    message = close_with()["rejected"][0][1].lower()
    for phrase in final:
        assert phrase not in message, (
            f"the release message contains {phrase!r}, which _send treats as a final user refusal — "
            f"the request would be dropped instead of retried")
