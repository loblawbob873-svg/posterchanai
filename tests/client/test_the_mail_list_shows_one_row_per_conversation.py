"""THE POINT OF A THREAD IS THAT IT IS ONE ROW.

Reported as "threading is showing multiple messages in Inbox, the point of threads is to
consolidate". The READER has grouped a conversation for a while; the LIST never did, so a
back-and-forth with one person filled the screen with near-identical rows and the newest was
wherever it happened to fall.

Grouped on the normalised subject — every reply/forward prefix stripped, not just the first, because
mail accretes them ("Re: Re: Fwd: quote" is ordinary after a few round trips). It is deliberately
the SAME rule the server threads with (`_normsubj` in app/routers/mail.py): the list and the reader
disagreeing about what one conversation is would be worse than not grouping at all.

Two things that are not cosmetic:
  * a message with no usable subject is its own row, or every subject-less message in the mailbox
    collapses under one heading;
  * the checkbox selects the WHOLE conversation. Acting on only the newest would delete or move half
    a thread and leave the rest, which is not something anybody means to do.

These run the SHIPPED `_convKey` / `_conversations` against message lists.
"""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

APP = (Path(__file__).resolve().parents[2] / "static/js/client/app.js").read_text(encoding="utf-8")


def _lift(name: str) -> str:
    """Lift a method by its FULL signature.

    `_key(` alone matched the Signer's `_key()` forty thousand lines earlier, and only the one test
    that reaches that branch noticed — the others never call it. A lift by first occurrence is a
    trap in a 36,000-line file."""
    sig = {"_key": "    _key(m){", "_convKey": "    _convKey(m){",
           "_conversations": "    _conversations(){"}[name]
    start = APP.index(sig)
    depth, i = 0, APP.index("{", start)
    for j in range(i, len(APP)):
        if APP[j] == "{":
            depth += 1
        elif APP[j] == "}":
            depth -= 1
            if depth == 0:
                return APP[start:j + 1]
    raise AssertionError(name)


def group(msgs: list) -> list:
    program = """
      const Mail = {
        acct:'me@x', folder:'INBOX',
        %s,
        %s,
        %s,
        msgs: %s,
      };
      process.stdout.write(JSON.stringify(Mail._conversations().map(c => ({
        key:c.key, n:c.all.length, head:c.head.uid, unread:!!c.unread,
        uids:c.all.map(m=>m.uid) }))));
    """ % (_lift("_key"), _lift("_convKey"), _lift("_conversations"), json.dumps(msgs))
    done = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-800:]
    return json.loads(done.stdout)


def m(uid, subject, read=True, ts=0):
    return {"uid": str(uid), "subject": subject, "read": read, "ts": ts,
            "account": "me@x", "folder": "INBOX"}


def test_a_back_and_forth_is_one_row():
    """THE REPORT."""
    got = group([m(3, "Re: Re: Quote"), m(2, "Re: Quote"), m(1, "Quote")])
    assert len(got) == 1, f"still {len(got)} rows for one conversation"
    assert got[0]["n"] == 3


def test_the_newest_message_heads_the_row():
    """`msgs` is newest-first, so the row must show the newest — not whichever started the thread."""
    got = group([m(3, "Re: Quote"), m(2, "Re: Quote"), m(1, "Quote")])
    assert got[0]["head"] == "3"


def test_every_reply_prefix_is_stripped_not_just_the_first():
    """Mail accretes them; one strip leaves "re: fwd: quote", which groups with nothing."""
    got = group([m(2, "RE: FW: re: Quote"), m(1, "Quote")])
    assert len(got) == 1


def test_different_conversations_stay_apart():
    got = group([m(2, "Invoice"), m(1, "Quote")])
    assert len(got) == 2


def test_a_subjectless_message_is_its_own_row():
    """Otherwise every subject-less message in the mailbox lands under one heading."""
    got = group([m(2, ""), m(1, "")])
    assert len(got) == 2, "subject-less messages were collapsed together"


def test_a_conversation_is_unread_if_any_message_in_it_is():
    got = group([m(2, "Re: Quote", read=True), m(1, "Quote", read=False)])
    assert got[0]["unread"] is True


def test_a_fully_read_conversation_is_not_marked_unread():
    got = group([m(2, "Re: Quote"), m(1, "Quote")])
    assert got[0]["unread"] is False


def test_the_row_carries_every_message_it_stands_for():
    """The checkbox needs them all — selecting half a thread and deleting it is not a thing anybody
    means to do."""
    got = group([m(3, "Re: Quote"), m(2, "Re: Quote"), m(1, "Quote")])
    assert sorted(got[0]["uids"]) == ["1", "2", "3"]


def test_the_checkbox_selects_the_whole_conversation():
    draw = APP[APP.index("const cb=el.querySelector('.mi-chk')"):]
    draw = draw[:draw.index("this.updateBulk(); };") + 21]
    assert "dataset.keys" in draw and "for(const k of keys)" in draw


def test_the_row_renders_every_message_key():
    assert 'data-keys="${enc(keys.join(\',\'))}"' in APP


def test_the_count_is_only_shown_when_there_is_more_than_one():
    assert "${n>1?`<span class=\"mi-count\"" in APP


def test_the_list_and_the_server_agree_on_what_a_conversation_is():
    """`_normsubj` in the router strips the same prefixes. If these two ever diverge, the list will
    group messages the reader then refuses to show together."""
    router = (Path(__file__).resolve().parents[2]
              / "app/routers/mail.py").read_text(encoding="utf-8")
    assert "(?:\\s*(?:re|fwd|fw)\\s*:\\s*)+" in router
    assert "(?:\\s*(?:re|fwd|fw)\\s*:\\s*)+" in APP
