"""@ TAGGING MUST BE USABLE ON A PHONE, AND IN CONCORD IT WAS NOT USABLE AT ALL.

Reported as "@ tagging is not working at all on mobile".

TWO SURFACES, TWO DIFFERENT FAILURES, AND NEITHER HAD A SINGLE TEST.

  * CONCORD had no picker. `drawMentions` filled a module variable and NOTHING EVER PAINTED IT —
    there is no markup, and there was no `.cc-mention*` rule in the stylesheet either. The only way
    to choose was `input.onkeydown`: ArrowUp/ArrowDown to move, Tab or Enter to accept. A phone
    keyboard has no arrows and no Tab, and Enter sends the message. So typing `@` in a room did
    nothing whatsoever, with nothing on screen to suggest a list existed.

  * THE TIMELINE/REPLY composer painted its list but bound the choice to `mousedown`, which is a
    mouse event with a compatibility story on touch rather than a guarantee. `pointerdown` is the
    one a finger, a mouse and a pen all send.

Both now hold focus on `pointerdown` (preventDefault, so the caret and the soft keyboard stay) and
commit on `pointerup` only if the pointer barely moved — so a drag meant to scroll the list does not
tag whoever was under the finger.

Keyboard selection is deliberately unchanged, and the painted highlight reads the SAME `mentionIndex`
the arrow keys move, so the two ways of choosing cannot disagree about what is selected.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CONCORD = (ROOT / "static/js/client/concord.js").read_text(encoding="utf-8")
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


def _paint():
    i = CONCORD.index("function paintMentionPicker(){")
    j = CONCORD.index("\n    }", i)
    return CONCORD[i:j]


def test_concord_actually_draws_its_mention_choices():
    """The bug, by name: choices with no picture are choices nobody can reach without a hardware
    keyboard."""
    assert "function paintMentionPicker(" in CONCORD, (
        "Concord's mention choices are module state again with nothing painting them — on a phone "
        "that is '@ tagging is not working at all'")
    body = _paint()
    assert "data-cc-mention=" in body, "the rows carry no identity, so nothing can be tapped"
    assert "mentionChoices.map" in body, "the list is not built from the choices"
    assert "document.createElement" in body and "insertBefore" in body, (
        "the list is never put into the document")


def test_concord_paints_on_every_path_that_changes_the_choices():
    """Three call sites, and missing one leaves a stale list on screen — worse than none, because
    tapping a row then tags somebody the query no longer matches."""
    draw = CONCORD[CONCORD.index("const drawMentions=()=>"):]
    draw = draw[:draw.index("\n    const acceptMention")]
    assert "paintMentionPicker()" in draw, "typing no longer repaints the list"
    close = CONCORD[CONCORD.index("const closeMentions=()=>"):]
    close = close[:close.index("\n")]
    assert "paintMentionPicker()" in close, "closing the picker leaves the list on screen"


def test_concord_commits_on_pointerup_and_holds_focus_on_pointerdown():
    body = _paint()
    assert "'pointerdown'" in body, (
        "the picker is bound to a mouse-only event again — a finger is not guaranteed to send one")
    assert "ev.preventDefault()" in body, (
        "without holding focus the textarea blurs, the phone keyboard drops and the caret is gone")
    assert "'pointerup'" in body, "the choice is made on press, so a scroll gesture tags somebody"
    assert "moved" in body, "nothing distinguishes a tap from a drag"


def test_the_concord_picker_is_bound_once_not_per_keystroke():
    """The rows are rebuilt on every keystroke. Per-row handlers would be attached hundreds of times
    and lost in between; the listeners belong on the list."""
    body = _paint()
    assert "list.addEventListener" in body
    assert not re.search(r"\.forEach\(\s*\w+\s*=>\s*\w+\.onpointer", body), (
        "per-row handlers are back, so they are rebound on every keystroke")


def test_the_keyboard_and_the_finger_agree_about_what_is_selected():
    """Two selection paths that keep separate state is how a picker highlights one person and tags
    another. The painted highlight must be `mentionIndex` — the same variable the arrows move."""
    body = _paint()
    assert "i===mentionIndex" in body, "the highlight no longer follows the keyboard's index"
    assert "aria-selected=" in body, "the selection is invisible to a screen reader"
    keys = CONCORD[CONCORD.index("input.onkeydown=e=>"):][:700]
    assert "ArrowDown" in keys and "acceptMention()" in keys, (
        "the keyboard path was removed — a desktop reader lost the picker instead of gaining one")


def test_the_concord_picker_has_a_stylesheet_and_touchable_rows():
    """A list with no rule is a list with no size. 44px is the touch-target floor this app uses
    everywhere else on a phone (`.cc-head-btn,.cc-compose-btn,.cc-mobile-back`)."""
    assert ".cc-mentions{" in CSS, "the picker has no stylesheet at all"
    rule = CSS[CSS.index(".cc-mention-opt{"):]
    rule = rule[:rule.index("}") + 1]
    assert "min-height:44px" in rule, "the rows are too small to hit with a thumb"
    box = CSS[CSS.index(".cc-mentions{"):]
    box = box[:box.index("}") + 1]
    assert "overflow:auto" in box, "a long member list cannot be scrolled"


def test_the_timeline_composer_picker_is_not_mouse_only():
    """The other surface. It painted its list but committed on `mousedown`."""
    block = APP[APP.index("function attachMentionAutocomplete(ta)"):]
    block = block[:block.index("\n  // decode an npub")]
    assert "onmousedown" not in block, (
        "the composer's mention list is bound to a mouse-only event again")
    assert "'pointerdown'" in block and "'pointerup'" in block
    assert "moved" in block, "a drag to scroll the list still picks whoever is under the finger"
    assert "ev.preventDefault()" in block, "the textarea blurs and the phone keyboard drops"


def test_picking_a_mention_tells_the_composer_its_text_changed():
    """The inline timeline composer sizes itself, saves its draft and shows its send button from the
    `input` event. Writing straight into `.value` fires nothing, so a post whose only edit was
    picking a mention was never autosaved and the box never grew."""
    block = APP[APP.index("function attachMentionAutocomplete(ta)"):]
    block = block[:block.index("\n  // decode an npub")]
    assert "new Event('input'" in block, (
        "picking a mention no longer notifies the composer, so the draft and the autosize miss it")


# ───────────────────────────── and the same picker, RUN with a thumb ─────────────────────────────

import json
import shutil
import subprocess

import pytest

NODE = shutil.which("node")

_THUMB = """
  import { picker } from './tests/client/concord_mention_pick_runtime.mjs';
  const P = picker({ choices: [{pk:'a'.repeat(64),name:'alice'},{pk:'b'.repeat(64),name:'bob'}],
                     index: 1 });
  const list = P.paint();
  const out = { rows: list ? list.children.length : 0, cls: list && list.className,
                selected: list ? list.children.map(r => r._attrs['aria-selected']) : [] };
  P.tap(list.children[0]);              // a thumb lands on the first row
  out.afterTap = P.accepted.slice();
  P.tap(list.children[1], 40);          // …and a drag down the list, which is a scroll
  out.afterDrag = P.accepted.slice();
  P.set([], 0); P.paint();              // the query stops matching anybody
  out.closed = P.list() === null;
  console.log(JSON.stringify(out));
"""


@pytest.mark.skipif(NODE is None, reason="needs node")
def test_a_thumb_can_actually_pick_somebody():
    """THE CLAIM, RUN. Everything above reads source; whether a TAP tags anybody cannot be read, and
    that is the entire report. The shipped painter draws into a stub composer and the shipped
    delegated handlers get the three events a finger really sends."""
    done = subprocess.run([NODE, "--input-type=module", "-e", _THUMB], cwd=ROOT,
                          capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-2000:]
    got = json.loads(done.stdout.strip().splitlines()[-1])

    assert got["rows"] == 2, "the picker drew nothing — this is '@ tagging is not working at all'"
    assert got["cls"] == "cc-mentions"
    assert got["selected"] == ["false", "true"], (
        "the painted highlight disagrees with the keyboard's index, so the arrows move one row and "
        "Enter tags another")
    assert got["afterTap"] == [0], "a tap on the first row tagged nobody"
    assert got["afterDrag"] == [0], (
        "dragging down the list tagged whoever was under the finger — a scroll is not a choice")
    assert got["closed"] is True, "a query that matches nobody leaves a stale list on screen"
