"""THE ɱ TIP MARK IS A PROFILE FACT, AND A CARD IS DRAWN ONCE.

Reported as "on mobile i could not see monero zap, but i zapped fine on desktop", with a note id.

Nothing about it is platform-specific. `noteCard` computes

    hasNoteXmr = isXmrAddr(xmrForNote(ev))

and `xmrForNote` falls back to the AUTHOR'S kind-0 when the note carries no `monero_address` tag of
its own. Cards render once — `needProfile` fetches the profile lazily as the card nears view — so on
a session where that author's profile was not already cached, the card is painted with no ɱ mark, no
`data-xmr` attribute and a tip title that says "Lightning" only, and nothing ever revisits it. On a
desktop that had been running for hours the profile was warm and the mark appeared; on a freshly
opened phone it did not. Same code, same note, different cache.

`decorateProfiles()` is the pass that exists precisely for this — it fills the avatar, the handle,
the display name and, since its own earlier bug, inline @mentions. The tip button was the one it did
not touch, which is the same defect the comment about "@profile @profile" describes two elements
away.

These drive the SHIPPED `_tipMarks` against a stub card.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
XMR = "4" + "A" * 94


def _helper() -> str:
    start = APP.index("  function _tipMarks(n, p){")
    return APP[start:APP.index("  function decorateProfiles(){", start)]


def run(profile: dict, note_has_xmr_tag=False, already_marked=False) -> dict:
    program = """
      const isXmrAddr = a => /^[48][1-9A-HJ-NP-Za-km-z]{94}$/.test(String(a||'').trim());
      const isBchAddr = a => /^(bitcoincash:)?[qp][a-z0-9]{41}$/.test(String(a||'').trim());
      const xmrOf = p => (p && p.monero_address) || '';
      const bchOf = p => (p && p.bch) || '';
      /* the smallest DOM these functions actually touch */
      const mk = (cls) => ({ className: cls, children: [], dataset:{}, title:'',
        appendChild(c){ this.children.push(c); },
        querySelector(sel){
          const want = sel.replace('.','');
          return this.children.find(c => (c.className||'').split(' ').includes(want)) || null; },
        closest(){ return btn; } });
      const bolt = mk('tipbolt');
      const btn = mk('act actz'); btn.children.push(bolt);
      const note = { dataset: %(dataset)s,
        querySelector(sel){ return sel === '.actz .tipbolt' ? bolt : null; } };
      globalThis.document = { createElement: () => mk('') };
      %(helper)s
      _tipMarks(note, %(profile)s);
      console.log(JSON.stringify({ xmr: note.dataset.xmr || '',
        marks: bolt.children.map(c => c.className), title: btn.title }));
    """ % {
        "helper": _helper(),
        "profile": json.dumps(profile),
        "dataset": json.dumps({"xmr": XMR} if note_has_xmr_tag else {}),
    }
    if already_marked:
        program = program.replace("const btn = mk('act actz');",
                                  "bolt.children.push(mk('xmr-mark'));\n      const btn = mk('act actz');")
    done = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-1000:]
    return json.loads(done.stdout.strip())


def test_the_mark_is_added_when_the_profile_arrives_after_the_card():
    """THE BUG. This is the whole report: the address was on the author's profile all along and the
    card had already been painted."""
    got = run({"monero_address": XMR})
    assert "xmr-mark" in got["marks"], "the ɱ mark is still never added once the profile lands"
    assert got["xmr"] == XMR, "the tip handler has no address to use"
    assert "Monero" in got["title"]


def test_a_profile_with_no_monero_address_changes_nothing():
    got = run({"name": "someone"})
    assert got["marks"] == [] and got["xmr"] == ""


def test_a_per_note_address_is_never_replaced_by_the_profiles():
    """A note carrying its own `monero_address` tag won at render, and it must keep winning — the
    profile's address is the author's default wallet, so overwriting would misroute the payment."""
    other = "8" + "B" * 94
    got = run({"monero_address": other}, note_has_xmr_tag=True)
    assert got["xmr"] == XMR, "the profile's address overwrote the note's own"


def test_it_does_not_add_a_second_mark_on_a_later_pass():
    """`decorateProfiles` runs on every batch of profiles, so this must be idempotent or a card
    accumulates a row of ɱs."""
    got = run({"monero_address": XMR}, already_marked=True)
    assert got["marks"].count("xmr-mark") == 1


def test_bitcoin_cash_is_marked_the_same_way():
    got = run({"bch": "q" + "a" * 41})
    assert "bch-mark" in got["marks"] and "Bitcoin Cash" in got["title"]


def test_both_marks_can_appear_together():
    got = run({"monero_address": XMR, "bch": "q" + "a" * 41})
    assert {"xmr-mark", "bch-mark"} <= set(got["marks"])
    assert "Monero" in got["title"] and "Bitcoin Cash" in got["title"]


def test_decorate_profiles_actually_calls_it():
    """The helper is worth nothing if the pass that runs on profile arrival does not invoke it."""
    block = APP[APP.index("  function decorateProfiles(){"):]
    block = block[:block.index("$$('.name[data-prof]').forEach(_decorName);")]
    assert "_tipMarks(n, p)" in block, "decorateProfiles no longer patches the tip affordance"


def test_a_thrown_card_does_not_kill_the_decorate_pass():
    """One bad card must not cost every avatar and name on screen — the guard this codebase has
    paid for repeatedly."""
    assert "catch(_){" in _helper()
