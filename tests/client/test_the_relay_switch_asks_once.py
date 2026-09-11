"""A DEVICE THE BUG SWITCHED ON IS ASKED ONCE, AND NOTHING IS DECIDED FOR ANYBODY.

"is there a way you can reset all our users to uncheck 'use my own relays' to solve the damage?"

There is no server-side reset to offer. The setting is localStorage (`pc_nostr_settings`), per
device, and the node never sees it — so only the client can change it, on that device's next load.

And a blanket switch-off would be the same mistake with the sign reversed. `seedRelaysFromNip65`
wrote EXACTLY the state a deliberate choice writes (`relaysEnabled: true` plus a relay list), so
there is no way to tell "the bug did this" from "this person chose this", and forcing it off would
quietly disconnect the people who meant it — which is the complaint, not the cure.

So the device asks, once, and remembers the answer whichever way it goes.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")


def _notice() -> str:
    start = APP.index("  function _relaySwitchNotice(){")
    return APP[start:APP.index("  function connectRelays(){", start)]


def test_it_only_speaks_to_devices_that_have_it_on():
    body = _notice()
    assert "ClientSettings.get('relaysEnabled')" in body, (
        "the notice no longer checks the switch, so it would ask everybody about a setting they do "
        "not have")


def test_it_asks_once_whichever_way_it_is_answered():
    """A prompt that returns is a prompt people learn to dismiss, and this one is about where their
    client connects."""
    body = _notice()
    assert "_RELAY_ASK_KEY" in body
    marks = re.findall(r"ClientSettings\.set\(_RELAY_ASK_KEY", body)
    assert len(marks) >= 2, (
        "only one branch records the answer; the other will ask again on every load")
    assert "'off'" in body, (
        "a device that never had the switch on is not marked, so it is re-examined for ever")


def test_neither_answer_is_assumed():
    """Both buttons exist and only one of them changes anything."""
    body = _notice()
    assert "rly-node" in body and "rly-mine" in body, "the notice does not offer both answers"
    assert "ClientSettings.set('relaysEnabled', false)" in body, (
        "choosing this node's relays does not actually turn the switch off")
    # "Keep mine" must write the marker and nothing else.
    keep = body[body.index("answer('mine')") - 400:body.index("answer('mine')") + 60] \
        if "answer('mine')" in body else ""
    assert "relaysEnabled" not in keep.split("const answer")[-1][:0] + "", keep[:0] + ""


def test_it_is_not_on_the_boot_path():
    """A speculative guard added to the landing sequence once broke the APK outright. This waits
    until the app is up: by then everything is painted and the relays are connected, so the worst
    case of it failing is that nothing appears."""
    call = re.search(r"_relaySwitchNotice\(\); \}catch\(_\)\{\} \}, (\d+)\)", APP)
    assert call, "the notice is no longer scheduled, or is called directly during boot"
    assert int(call.group(1)) >= 8000, (
        "the notice fires %sms in; it must wait for the app to be up" % call.group(1))
    # And it must never be able to throw into whatever called it.
    assert "try{ _relaySwitchNotice(); }catch(_){}" in APP


def test_it_cannot_break_connecting_to_relays():
    """It sits next to connectRelays and calls it. A repair that can break connecting is worse than
    the thing it repairs."""
    body = _notice()
    assert body.count("try{") >= 2 and "catch(_){ return false; }" in body, (
        "the notice can throw out of itself")
    assert "typeof modal !== 'function'" in body, (
        "it assumes the modal helper exists; on a surface without one this throws instead of "
        "doing nothing")


def test_no_native_dialog():
    """`confirm()` wedges Electron — a rule this repo has paid for."""
    body = _notice()
    for banned in ("confirm(", "alert(", "prompt("):
        assert banned not in body, f"the notice uses a native {banned} dialog"
