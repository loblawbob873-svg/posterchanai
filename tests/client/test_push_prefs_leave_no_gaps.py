"""A TYPE NOBODY SWITCHED IS STILL A TYPE THE FILTERS HAVE TO BE TOLD ABOUT.

Reported as mentions arriving on Android with only DMs, Zaps, Concord mentions and Texts selected —
the same shape as "i am getting push notifications for likes when I only have DM's and concord
mentions selected" before it.

BOTH PUSH FILTERS FAIL OPEN, deliberately and at length (`app/services/push_prefs.py`): unset,
unparseable or unrecognised all mean SEND, because a silenced alert is invisible by construction and
a missing DM is a bug nobody can report. That is the right default and it is not what changed here.

What changed is what the filters are TOLD. `_pushPrefState` is the STORED shape and is deliberately
sparse — a key appears only once it has been switched, so the default can move later without
rewriting everybody's document. Correct for storage, wrong for the wire: a type with no stored
boolean arrives at a fail-open filter as "no opinion", and is sent. The gap and the fail-open
default multiply.

So the wire carries EVERY known type resolved to its effective value. The stored shape is untouched.
"""
from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
PREFS_PY = (ROOT / "app/services/push_prefs.py").read_text(encoding="utf-8")
NODE = shutil.which("node")


def _fn(head):
    i = APP.index(head)
    j = APP.index("{", i)
    depth = 0
    for k in range(j, len(APP)):
        if APP[k] == "{":
            depth += 1
        elif APP[k] == "}":
            depth -= 1
            if depth == 0:
                return APP[i:k + 1]
    raise AssertionError("unterminated " + head)


def test_the_stored_shape_is_still_sparse():
    """Not a detail: keeping storage sparse is what lets a default change without rewriting every
    account's document. The fix must not have turned the fix into a migration."""
    fn = _fn("function _pushPrefState(owner=_notificationOwner())")
    assert "typeof raw[key]==='boolean'" in fn, (
        "_pushPrefState now writes every key, so changing a default silently keeps the old value "
        "for everybody who ever opened the screen")


def test_the_wire_shape_carries_every_type():
    fn = _fn("function _pushPrefsWire(owner=_notificationOwner())")
    assert "_NOTIFICATION_TYPES" in fn and "_PUSH_PREF_DEFAULT" in fn, (
        "the mirrored preferences are a subset again, so any type nobody toggled is 'no opinion' "
        "to a filter that fails open — and is therefore sent")


def test_both_filters_are_given_the_complete_picture():
    """Two filters, two call sites: the server row (`/api/push/prefs`) and the phone-local copy
    (`DirectPushStore`). A backstop that shares the gap it backs up is not a backstop."""
    mirror = _fn("async function mirrorPushPrefs(owner=_notificationOwner())")
    assert "_pushPrefsWire(owner)" in mirror, "the server row still gets the sparse subset"
    setter = _fn("function setPushPreference(key,value)")
    assert "_pushPrefsToDevice();" in setter, (
        "the phone-local copy is handed the sparse delta, so it keeps the gap")


def test_the_screen_and_the_wire_share_one_default():
    """`pushPreference` draws the switch; `_pushPrefsWire` tells the filters. If those disagree the
    screen shows one thing and the phone does another, which is unfalsifiable from the outside."""
    assert "const _PUSH_PREF_DEFAULT=true;" in APP
    assert APP.index("const _PUSH_PREF_DEFAULT=true;") < APP.index("out[key]= typeof stored[key]"), (
        "the default is declared after the function that reads it — a temporal dead zone read, "
        "which throws rather than merely defaulting")
    assert _fn("function pushPreference(key)").count("_PUSH_PREF_DEFAULT") == 1


def test_the_vocabulary_matches_the_server():
    """One list of names. A toggle the server has never heard of silences nothing at all, and the
    server's own comment says so."""
    js = set(re.findall(r"\['([a-z]+)',", APP.split("_NOTIFICATION_TYPES = [", 1)[1].split("];", 1)[0]))
    py = set(re.findall(r"['\"]([a-z]+)['\"]",
                        PREFS_PY.split("PUSH_TYPES = (", 1)[1].split(")", 1)[0]))
    assert js, "re-point this test: _NOTIFICATION_TYPES no longer parses"
    missing = sorted(js - py)
    assert not missing, ("the client offers toggles the server cannot act on: %s" % missing)


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_a_never_touched_type_is_sent_as_an_answer_not_a_gap():
    """RUN both shapes. Somebody switches OFF likes and leaves mentions alone; the stored document
    then mentions only `likes`. The old wire shape passed exactly that on, and the server's
    fail-open rule turned the silence about mentions into a send."""
    wire = _fn("function _pushPrefsWire(owner=_notificationOwner())")
    state = _fn("function _pushPrefState(owner=_notificationOwner())")
    script = r"""
      const _PUSH_PREF_DEFAULT = true;
      const _NOTIFICATION_TYPES = [['dm','DMs'],['likes','Likes'],['mentions','Mentions'],
                                   ['zaps','Zaps'],['concord','Communities']];
      const store = { 'pc_push_prefs:me': JSON.stringify({ likes: false }) };
      const localStorage = { getItem: (k) => store[k] ?? null };
      const _notificationOwner = () => 'me';
      const _pushPrefKey = (o) => 'pc_push_prefs:' + o;
      %s
      %s
      console.log(JSON.stringify({ stored: _pushPrefState(), wire: _pushPrefsWire() }));
    """ % (state, wire)
    done = subprocess.run([NODE, "-e", script], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-800:]
    got = json.loads(done.stdout.strip().splitlines()[-1])

    assert got["stored"] == {"likes": False}, "the stored shape stopped being sparse"
    assert set(got["wire"]) == {"dm", "likes", "mentions", "zaps", "concord"}, (
        "the wire shape is missing types, so a fail-open filter sends them: %r" % (got["wire"],))
    assert got["wire"]["likes"] is False, "an explicit choice was lost on the way to the wire"
    assert got["wire"]["mentions"] is True, (
        "a type nobody touched must travel as its effective value, not as an absence")
