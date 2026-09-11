""""CAN'T OPEN MESSAGE: attachments.map is not a function" — and it was ONE email.

`attachments` is two different things under one name:

  * the full message carries the LIST, `[{name,type,size}]`
  * `_summary()` in app/routers/mail.py carries a COUNT, because a list row only needs to know
    whether to draw a paperclip

AND THE CACHE-FIRST OPEN MIXES THEM. `openMsg` paints the conversation immediately out of
`this.msgs` + `this.convSent` — which are list rows — with the fully-fetched seed pushed on top,
then upgrades when `/thread` answers (measured at 11s on this mailbox). So every SIBLING in that
first paint carries a count, `(2||[]).map` throws, and the reader renders NOTHING.

That is why it was one email: it bites a message that is part of a CONVERSATION. "other emails open
with attachments but not that one."

`||[]` reads like a guard and is not one — it catches null and undefined, and a number goes
straight through. Storage was checked before any of this was changed: all 18,000 stored documents
hold a proper list, so nothing was wrong with the data.
"""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
NODE = shutil.which("node")
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
ROUTER = (ROOT / "app/routers/mail.py").read_text(encoding="utf-8")


def _block() -> str:
    start = APP.index("    _msgBlock(m, folder, acct, expanded){")
    depth, i, in_s = 0, APP.index("{", start), None
    j = i
    while j < len(APP):
        c = APP[j]
        if in_s:
            if c == "\\":
                j += 2
                continue
            if c == in_s:
                in_s = None
        elif c in "'\"`":
            in_s = c
        elif c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                return APP[start:j + 1]
        j += 1
    raise AssertionError("could not find the end of _msgBlock")


@pytest.mark.skipif(not NODE, reason="needs node to run the shipped renderer")
def test_a_summary_row_in_the_conversation_does_not_blank_the_reader():
    """Renders the SHIPPED _msgBlock over exactly the mix the cache-first open produces: one full
    message and one list row."""
    program = """
      const enc = s => String(s==null?'':s).replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
      const _fmtBytes = n => String(n||0);
      const _previewable = () => false;
      const _mailAttachmentUrl = () => 'https://x/att';
      const _mailDate = () => '10:31';
      // Everything _msgBlock reaches for, and nothing else — a stub that does more than the real
      // thing would hide a missing dependency rather than reveal one.
      const M = { msgs: [], convSent: [],
                  _linkify: t => enc(t), _nmailHtml: () => '',
                  %s };
      const full    = {uid:'1', from:'a@b', subject:'s', ts:1, attachments:[{name:'f.pdf',type:'application/pdf',size:3}], body_text:'hi'};
      const summary = {uid:'2', from:'c@d', subject:'s', ts:2, attachments:2, preview:'p'};
      const out = [];
      for (const m of [full, summary]) {
        try { out.push({uid:m.uid, html: (M._msgBlock(m,'INBOX','acc',true)||'').length}); }
        catch (e) { out.push({uid:m.uid, error: String(e && e.message || e)}); }
      }
      console.log(JSON.stringify(out));
    """ % _block()
    done = subprocess.run([NODE, "-e", program], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-1500:]
    got = json.loads(done.stdout.strip())
    errs = [g for g in got if "error" in g]
    assert not errs, (
        "a list row in the conversation threw, which blanks the whole reader: %r" % (errs,))
    assert all(g.get("html", 0) > 0 for g in got), got
    # The full message must still show its attachment; the summary simply has none to show.
    assert got[0]["html"] > got[1]["html"], (
        "the fully-fetched message lost its attachment markup: %r" % (got,))


def test_the_guard_asks_what_the_value_is():
    """`||[]` is the shape of the bug, not the fix: it admits every number."""
    block = _block()
    assert "Array.isArray(m.attachments)" in block, (
        "the renderer is back to a falsy check, which a count passes straight through")
    assert "(m.attachments||[]).map" not in block


def test_the_two_shapes_are_still_two_shapes():
    """If the server ever stops projecting a count, this whole hazard is gone and the comment above
    the guard is wrong. Notice when that happens rather than leaving a stale explanation."""
    summary = ROUTER[ROUTER.index("def _summary("):ROUTER.index("@router.get(\"/accounts\")")]
    assert 'len(m.get("attachments")' in summary, (
        "the list projection no longer sends a COUNT — re-read the guard in _msgBlock and its "
        "comment, which exist because these two payloads disagree about what `attachments` is")


def test_the_draft_composer_is_guarded_the_same_way():
    """Same collision, same place it would land: a draft summarised for a list carries a count."""
    at = APP.index("Array.isArray(dr.attachments)")
    assert at > 0, "the draft path can still call .forEach on a count"
