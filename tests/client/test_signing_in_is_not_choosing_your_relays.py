""""I DON'T REMEMBER CHECKING USE MY OWN RELAYS" — because nobody did.

The sign-in screen has a relay box, pre-filled with the saved list when there is one and otherwise
with the SERVER'S DEFAULTS (an empty box would ask somebody to already know which relays exist).
`_persistAuthRelays()` decides from that box whether the user chose their own relays, and its comment
states the rule exactly:

    Compare against the SEED instead: only a box that differs from what we put in it is a choice.

`_authRelaySeed` is that record — and it was written inside `if(t && !t.value)`, the branch that
FILLS an empty box. The textarea keeps its value, so the seed was recorded on the first visit and
never again. Every later sign-in found it empty, and the guard was written `if(_authRelaySeed && …)`
— so an unrecorded seed SKIPPED the comparison and the box counted as a deliberate choice.

The box holds the server's defaults. So signing in could switch "use my own relays" ON and pin those
defaults as the user's own list, after which the node's relay list no longer reaches them and
nothing on screen ever said a choice had been made.

THE PATH THAT ACTUALLY FIRES is browser form restoration. `_fillAuthConnFields()` runs when the
Connection pane is OPENED and `_persistAuthRelays()` when the INSTANCE is changed, and the seed lives
in module scope — so an ordinary second visit still matched it. But Firefox and Chrome refill a
`<textarea>` on reload and on back-navigation, and a page load starts with `_authRelaySeed = ''`. So:
reload the sign-in screen, open Connection (box already populated by the browser, `!t.value` false,
nothing recorded), change your server — and the app writes a relay list nobody chose and switches the
toggle on.

Two changes, and they are the same idea twice: record what we put there EVERY time, and treat "no
record" as "not a choice" rather than as "a choice". A question about somebody else's intent must
fail closed — the cost of failing closed here is one lost convenience (relay edits are carried across
the instance reload); the cost of failing open is rewriting where somebody's client connects.

WHAT IS DELIBERATELY NOT DONE: existing accounts already in this state are not repaired. Switching
somebody's relays back would be the same sin in the other direction. Settings -> Connection turns
"use my own relays" off and restores the node's list.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
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


_HARNESS = """
  const DEFAULTS = ['wss://poster.place/relay', 'wss://nos.lol', 'wss://offchain.pub'];
  let saved = { relays: [], relaysEnabled: false };
  const ClientSettings = { set: (k, v) => { saved[k] = v; } };
  const userRelays = () => saved.relays;
  const defaultRelays = () => DEFAULTS.slice();
  const normalizeRelay = (u) => { u = String(u || '').trim(); if (!u) return '';
    if (u.indexOf('wss://') !== 0 && u.indexOf('ws://') !== 0) u = 'wss://' + u;
    while (u.length && u[u.length - 1] === '/') u = u.slice(0, -1); return u; };
  const box = { value: '' };
  const $ = (sel) => (sel === '#conn-relays' ? box : null);
  const _instanceBase = () => '';
  let _authRelaySeed = '';
  %s
  %s
  %s
  const out = {};
"""


def _drive(body):
    """Run the SHIPPED fill/persist pair against a stub box.

    A FRESH PAGE EVERY TIME — `_authRelaySeed` starts empty, which is the state the reported bug
    lives in. Sharing one process between cases would carry a seed from the previous one and quietly
    make the interesting case untestable."""
    src = (_HARNESS % (_fn("function _fillAuthConnFields()"),
                       _fn("function _authRelayUrls()"),
                       _fn("function _persistAuthRelays()"))
           + body + "\n  console.log(JSON.stringify(out));")
    done = subprocess.run([NODE, "-e", src], cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_the_seed_is_recorded_every_time_not_only_when_the_box_was_empty():
    fill = _fn("function _fillAuthConnFields()")
    assert "if(t) _authRelaySeed=t.value;" in fill, (
        "the seed is recorded only inside the fill-an-empty-box branch again, so every later "
        "sign-in has no record of what we put there")


def _code_only(src):
    """Comments are not code — and this file's own comment quotes the old guard verbatim."""
    out, i = [], 0
    while i < len(src):
        a = src.find("/*", i)
        b = src.find("//", i)
        nxt = min(x for x in (a, b) if x >= 0) if (a >= 0 or b >= 0) else -1
        if nxt < 0:
            out.append(src[i:]); break
        out.append(src[i:nxt])
        i = (src.find("*/", nxt) + 2) if nxt == a else (src.find("\n", nxt) + 1 or len(src))
    return "".join(out)


def test_an_unrecorded_seed_is_not_treated_as_a_choice():
    persist = _code_only(_fn("function _persistAuthRelays()"))
    assert "if(_authRelaySeed &&" not in persist, (
        "the comparison is skipped when there is no recorded seed — that fails OPEN on a question "
        "about somebody else's intent, and silently switches 'use my own relays' on")
    assert "String(_authRelaySeed||'')" in persist


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_signing_in_without_touching_the_box_changes_nothing():
    """RUN the shipped functions. The box is filled with the server's defaults, nothing is typed,
    and the question is whether the app decides the user picked those relays.

    Then the same flow with a relay actually typed in — which IS a choice and must still be saved,
    or the fix would have removed the feature instead of the bug."""
    got = _drive(r"""
      _fillAuthConnFields();                       // a first visit: empty box gets the defaults
      out.filled = box.value.split('\n').length;
      out.persistedOnFirstVisit = _persistAuthRelays();
      _fillAuthConnFields();                       // …and a second, box already populated
      out.persistedOnSecondVisit = _persistAuthRelays();
      out.enabledWithoutAsking = saved.relaysEnabled;
      box.value = 'wss://relay.example.org';       // somebody actually types one
      out.persistedAfterTyping = _persistAuthRelays();
      out.enabledAfterTyping = saved.relaysEnabled;
      out.savedList = saved.relays;
    """)
    assert got["filled"] == 3, "the box was not seeded with the server's defaults"
    assert got["persistedOnFirstVisit"] is False, (
        "merely opening the sign-in screen saved the defaults as the user's own relays")
    assert got["persistedOnSecondVisit"] is False, (
        "the SECOND visit saved an untouched box as a deliberate relay list")
    assert got["enabledWithoutAsking"] is False, (
        "'use my own relays' was switched on without anybody choosing it, which pins the server's "
        "defaults and stops the node's list ever reaching this account again")
    assert got["persistedAfterTyping"] is True, "typing a relay no longer saves it"
    assert got["enabledAfterTyping"] is True
    assert got["savedList"] == ["wss://relay.example.org"]


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_a_browser_restored_textarea_is_not_a_choice():
    """THE PATH THAT PRODUCED THE REPORT. A page load starts with no recorded seed; the browser
    refills the textarea by itself; the pane is opened, so the fill branch is skipped because the
    box is not empty. Nothing here was typed by anybody."""
    got = _drive(r"""
      box.value = DEFAULTS.join('\n');      // the browser restored it, not us
      _fillAuthConnFields();                 // opening the pane: the box is not empty
      out.persisted = _persistAuthRelays();  // …and the user changes their server
      out.enabled = saved.relaysEnabled;
      out.list = saved.relays;
    """)
    assert got["persisted"] is False, (
        "a textarea the BROWSER refilled was saved as a deliberate relay list")
    assert got["enabled"] is False, "'use my own relays' was switched on by a page reload"
    assert got["list"] == []


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_editing_and_changing_your_mind_is_not_a_choice():
    """Type something, delete it, leave the box as you found it. Nothing was chosen."""
    got = _drive(r"""
      _fillAuthConnFields();
      const seeded = box.value;
      box.value = seeded + '\nwss://relay.example.org';   // typed…
      box.value = seeded;                                   // …and removed again
      out.persisted = _persistAuthRelays();
      out.enabled = saved.relaysEnabled;
    """)
    assert got["persisted"] is False, "reverting an edit still counted as choosing"
    assert got["enabled"] is False


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_trailing_whitespace_is_not_a_choice():
    """The comparison is trimmed on both sides; a stray newline is not an opinion about relays."""
    got = _drive(r"""
      _fillAuthConnFields();
      box.value = box.value + '\n\n  ';
      out.persisted = _persistAuthRelays();
    """)
    assert got["persisted"] is False, "a trailing newline counted as a deliberate relay list"


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_a_box_matching_what_is_already_saved_writes_nothing():
    """The first guard, unchanged and still needed: re-saving an identical list would churn the
    setting (and, before the reload, the pool) for nothing."""
    got = _drive(r"""
      saved.relays = ['wss://relay.example.org'];
      box.value = 'wss://relay.example.org';
      _fillAuthConnFields();
      out.persisted = _persistAuthRelays();
    """)
    assert got["persisted"] is False


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_an_empty_box_never_saves_an_empty_relay_list():
    """The worst possible write: an empty list with the toggle ON is a client that can reach no
    relay at all. Guarded before anything else, and asserted here because the fix moved the code
    around it."""
    got = _drive(r"""
      box.value = '   \n  ';
      out.persisted = _persistAuthRelays();
      out.enabled = saved.relaysEnabled;
      out.list = saved.relays;
    """)
    assert got["persisted"] is False
    assert got["enabled"] is False
    assert got["list"] == [], "an empty relay list was saved — this client could reach nothing"


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_the_deliberate_save_button_is_untouched():
    """`_saveAuthRelays` is the EXPLICIT path — somebody pressed Save. It must save unconditionally,
    seed or no seed, or the fix would have removed the feature instead of the bug."""
    save = _fn("function _saveAuthRelays()")
    assert "ClientSettings.set('relaysEnabled', true)" in save
    assert "_authRelaySeed" not in save, (
        "the explicit Save button now consults the seed — pressing Save IS the choice and must "
        "never be second-guessed")
