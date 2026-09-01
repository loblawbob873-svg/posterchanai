"""@-MENTION CANNOT COMPLETE SOMEBODY THE ROOM WILL NOT NAME.

Reported as "Concord: user tagging still not working, I want to @ tab autocomplete and it notifies
the user properly".

Both halves of that were already built and both were correct: Tab is bound in the composer's
keydown (`if(e.key==='Tab'||...) acceptMention()`), and sending publishes `['P',pk]` and `['p',pk]`
for everyone tagged. What was missing sat one layer under them — `roomParticipants` read MESSAGE
AUTHORS and nothing else, so the autocomplete's candidate list held only people who had already
spoken in history this client had loaded. A member who had not posted did not exist to it, and Tab
completes nothing when nothing was offered.

The same function feeds the Members pane (whose empty state reads "No members have appeared yet")
and the call picker, so all three were wrong in the same way.

The room's own control document is the answer: `controlPubkeys` are its admins and each channel's
`streamPubkeys` are the keys allowed to write to it. Message authors STAY in the set — a room whose
control view has not decrypted yet must not lose the people visibly talking in it.

The runtime file drives the shipped function under node.
"""
from __future__ import annotations

import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
CONCORD = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")
RUNTIME = ROOT / "tests/client/concord_members_runtime.mjs"
NODE = shutil.which("node")


@pytest.mark.skipif(not NODE, reason="needs node")
def test_the_room_names_its_members_not_just_its_talkers():
    done = subprocess.run([NODE, str(RUNTIME)], capture_output=True, text=True, timeout=120)
    assert done.returncode == 0, done.stdout + done.stderr
    assert "ok" in done.stdout


def test_tab_accepts_the_completion():
    """Already true, asserted so the fix above cannot be undone by losing the key that uses it."""
    keys = CONCORD[CONCORD.index("input.onkeydown="):]
    keys = keys[:keys.index("\n")]
    assert "e.key==='Tab'" in keys and "acceptMention()" in keys
    assert "ArrowDown" in keys and "Escape" in keys, "the list lost its navigation or its escape"


def test_a_mention_tags_the_person_so_they_are_notified():
    """The other half of the report. `P` and `p` are what every other client watches for."""
    send = CONCORD[CONCORD.index("send.onclick=async()=>{"):]
    send = send[:send.index("\n")]
    assert "mentionTags.push(['P',pk],['p',pk])" in send
    assert "typedMentionRecipients(" in send, (
        "a name typed by hand rather than picked from the list no longer tags anybody")


def test_the_candidate_list_is_cached_per_control_generation():
    """`drawMentions` runs on every keystroke and `inspectControl` re-walks the wraps each time."""
    fn = CONCORD[CONCORD.index("function roomParticipants(room,viewerPubkey=''){"):]
    fn = fn[:fn.index("\n  function ")]
    assert "_partsCache" in fn
    assert "wraps&&wraps.length" in fn, "the cache key ignores new control wraps, so it goes stale"


def test_message_authors_are_never_lost():
    """A room whose control view has not decrypted yet must still name whoever is visibly talking —
    losing them would be a worse bug than the one being fixed."""
    fn = CONCORD[CONCORD.index("function roomParticipants(room,viewerPubkey=''){"):]
    fn = fn[:fn.index("\n  function ")]
    assert "fromMessages" in fn
    assert "catch(_){ known=[]; }" in fn, "a throwing control view is not contained"
