"""A ROOM WENT DEAF FOR EVER WHEN ITS RELAY DROPPED THE SOCKET.

Reported as: "i had to change concord rooms and click on my room again to see new message in there.
I thought this was fixed" -- a different bug from the membership vault, which was about the room
LIST.

A Concord room's messages arrive on the ROOM's own relays, which the managed pool deliberately does
not own, so they are read through `Relay.subscribeFrom` -- raw WebSockets with no `onclose` and no
redial. When the far end went away (a relay restart, an idle timeout, a laptop sleep, any blip) the
socket was simply gone. Nothing above it could tell: `closed` stayed false and the caller's handle,
its `ready` promise and its `hasTargets` flag were all exactly as they are on a healthy
subscription. `startChatLive` then short-circuits on `chatSubKey===key` and never rebuilds it, so
the open room stopped receiving messages permanently while every other part of the app kept working.

Switching rooms changes that key, which is why it was the only thing that ever fixed it -- and why
the report is phrased as a workaround rather than as a failure.

`live` is OPT-IN because most callers of subscribeFrom are one-shot reads with a `timeout` that is
supposed to end them. The shipped function runs here against a fake socket that can be dropped: the
question is "did it dial again", which no assertion about the handle can answer.
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "tests/client/subscribe_from_redial_sim.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")


def _run(plan):
    out = subprocess.run(["node", str(SIM), json.dumps(plan)],
                         cwd=ROOT, text=True, capture_output=True, timeout=120)
    assert out.returncode == 0, out.stderr[:3000]
    return json.loads(out.stdout)


def test_a_dropped_live_socket_is_dialled_again():
    r = _run({"live": True, "drop": True, "advanceMs": 5000})
    assert r["dials"] == 2, f"the room stayed deaf after the relay went away ({r['dials']} dial(s))"
    assert r["openSockets"] == 1, r


def test_the_new_socket_re_sends_the_req():
    """A reconnected socket that never asks for anything is a connection, not a subscription."""
    r = _run({"live": True, "drop": True, "advanceMs": 5000})
    assert r["reqs"] == 2, r


def test_a_one_shot_read_is_left_alone():
    """Every other caller is a bounded read whose `timeout` is meant to end it. Redialling those
    would turn a finished query into a permanent socket."""
    r = _run({"live": False, "drop": True, "advanceMs": 60000})
    assert r["dials"] == 1, r


def test_stopping_it_stops_the_redial_too():
    """`stop()` closes the socket, and closing a live socket is exactly what schedules a redial --
    so the cancel has to happen first or the handle outlives the caller."""
    r = _run({"live": True, "stopFirst": True, "drop": True, "advanceMs": 60000})
    assert r["dials"] == 1, f"it kept reconnecting after being stopped ({r['dials']} dials)"
    r2 = _run({"live": True, "drop": True, "advanceMs": 5000, "stopAfter": True})
    assert r2["openSockets"] == 0, "a socket survived stop()"


def test_it_backs_off_rather_than_hammering_a_relay_that_is_gone():
    """A relay that is genuinely down must cost one socket every half minute, not a storm."""
    r = _run({"live": True, "deadRelay": True, "drop": True, "advanceMs": 120000})
    assert r["dials"] <= 10, f"{r['dials']} dials in two minutes is a reconnect storm"
    assert r["dials"] >= 4, f"it gave up on a relay that was only down ({r['dials']} dial(s))"


def test_it_never_gives_up_on_a_relay_that_stays_down():
    """The outage this was reported for is a laptop that slept. Whatever the length, the room has to
    come back on its own -- a subscription that exhausts its retries is the same bug again, later."""
    r = _run({"live": True, "deadRelay": True, "drop": True, "advanceMs": 600000})
    assert r["pendingTimers"] >= 1, "it stopped trying to reconnect"
