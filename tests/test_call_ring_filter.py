"""Only a call INVITE may wake a phone.

The push watcher cannot read a signaling frame (NIP-44 to the callee), so the client marks the
ring-worthy one with a cleartext `t=invite` and the server filters on that. Two things make this worth
a test rather than a comment:

* Getting it wrong is INVISIBLE in development. Every frame still routes, every call still connects —
  the only symptom is a phantom "📞 Incoming call" landing on a locked phone 45 seconds after the
  caller gave up, which nobody sees unless they background a device and wait.
* The first implementation tagged only invites, and looked right. It wasn't: a new client's ICE frame
  carried no tag, which is byte-identical to an OLD client's invite, so the compatibility fallback had
  to ring for both and the bug survived untouched. The invariant is that ABSENCE of a `t` tag means
  exactly one thing — a client older than the change — which only holds while new clients tag EVERY
  frame.

So this drives the REAL `_callTags` out of app.js through node, against the REAL `_rings`, rather than
restating either one here. A reimplementation would agree with itself while production disagreed.
"""
import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

from app.services.nostr_push_service import _rings

_APP_JS = Path(__file__).resolve().parent.parent / "static" / "js" / "client" / "app.js"

# Every frame type the call code sends, and whether it should ring. Group frames included: a group
# invite rings too, and `rbye` is the group twin of the `bye` that caused the phantom ring.
_FRAMES = {
    "invite": True, "ginvite": True,
    "ice": False, "answer": False, "reanswer": False, "bye": False,
    "roffer": False, "ranswer": False, "rice": False, "rbye": False,
    "rhere": False,
}


def _client_tags(frames):
    """Run the shipped _callTags over `frames`, returning {frame: tags}."""
    src = _APP_JS.read_text(encoding="utf-8")
    m = re.search(
        r"const _RING_FRAMES = new Set\(.*?\n  function _callTags\(peerHex, obj\)\{\n.*?\n  \}",
        src, re.S)
    assert m, "could not find _RING_FRAMES/_callTags in app.js — did they move or get renamed?"
    js = m.group(0) + "\nconsole.log(JSON.stringify(JSON.parse(process.argv[1]).map(t=>_callTags('peer',{t}))));"
    out = subprocess.run(["node", "-e", js, json.dumps(list(frames))],
                         capture_output=True, text=True, timeout=30)
    assert out.returncode == 0, f"node failed: {out.stderr}"
    return dict(zip(frames, json.loads(out.stdout)))


@pytest.mark.skipif(not shutil.which("node"), reason="node is needed to run the shipped client code")
def test_only_invites_ring():
    tags = _client_tags(_FRAMES)
    for frame, should_ring in _FRAMES.items():
        assert _rings({"tags": tags[frame]}) is should_ring, (
            f"{frame!r} tagged {tags[frame]} → rings={_rings({'tags': tags[frame]})}, "
            f"expected {should_ring}. A false True re-rings a phone after the call is over; "
            f"a false False means the phone never rings at all.")


@pytest.mark.skipif(not shutil.which("node"), reason="node is needed to run the shipped client code")
def test_every_frame_is_tagged():
    """The load-bearing invariant: no new-client frame may be untagged, or `no tag` stops meaning
    `old client` and the compatibility fallback silently re-rings for ICE and hangups."""
    for frame, tags in _client_tags(_FRAMES).items():
        assert any(t[0] == "t" for t in tags), f"{frame!r} has no t tag: {tags}"


def test_untagged_frames_still_ring():
    """Clients older than this change tag nothing. Until they roll over they must keep ringing —
    tightening this before then makes an old caller silently unable to reach anyone."""
    assert _rings({"tags": [["p", "peer"]]}) is True
    assert _rings({"tags": []}) is True


def test_malformed_tags_do_not_crash_the_watcher():
    """The watcher runs on a live relay subscription, so an unparseable frame from anywhere on the
    network must not take it down — that would silence calls for every user on the node."""
    for tags in ([["t"]], [[]], [["t", "invite", "extra"]], [["p"]], "not-a-list"):
        try:
            _rings({"tags": tags})
        except Exception as e:                                    # noqa: BLE001
            pytest.fail(f"_rings raised on tags={tags!r}: {e}")
