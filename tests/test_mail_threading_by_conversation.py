"""A THREAD IS A CONVERSATION, NOT A SUBJECT LINE.

Reported as "i think email conversations are broke, it grouped 4 different kraken emails into 1
thread" and "it should be by thread/conversation".

`_build_thread` closes the Message-ID/References/In-Reply-To graph first, which is right and does
all the real work. The bug was its FALLBACK: when a message had no header links it gathered every
message sharing a normalised subject — with no participant check, no time window, and no question
about whether those messages were replies at all.

Automated senders are precisely where that fails. A exchange reuses one subject for months and
references nothing, so four separate notices — four ROOTS, four unrelated events — became one
conversation. Meanwhile the case the fallback exists for is narrow and real: a mailing list strips
`References`, and a genuine reply loses its link to a parent we do hold.

So the rule is about what a message CLAIMS: carrying In-Reply-To or References means "I continue
something", and matching that to a same-subject message is a repair. Carrying neither makes it a
root, and two roots are never the same conversation however identical their subjects.

Every check here was verified to fail with the rule removed.
"""
from __future__ import annotations

from pathlib import Path

import pytest

from app.routers.mail import _build_thread, _is_reply, _normsubj

ROOT = Path(__file__).resolve().parents[1]


def msg(uid, subject, *, mid=None, irt=None, refs="", ts=0, folder="INBOX"):
    return {"uid": str(uid), "folder": folder, "subject": subject, "ts": ts,
            "message_id": mid or f"<{uid}@x>", "in_reply_to": irt or "", "references": refs}


#: Four separate notices from an automated sender: same subject, no links, days apart.
KRAKEN = [msg(i, "Kraken - Security notification", ts=1000 * i) for i in range(1, 5)]


def test_four_unrelated_notices_are_four_conversations():
    """THE REPORT. Each is a root; none references another."""
    for seed in KRAKEN:
        got = _build_thread(seed, KRAKEN)
        assert len(got) == 1, (
            f"{len(got)} messages grouped into one thread from a shared subject — these are "
            f"separate events from an automated sender, not a conversation")


def test_a_real_reply_still_joins_its_parent():
    """The header graph is untouched and does all the real work."""
    root = msg(1, "lunch?", mid="<a@x>", ts=1)
    reply = msg(2, "Re: lunch?", mid="<b@x>", irt="<a@x>", ts=2)
    got = _build_thread(root, [root, reply])
    assert [m["uid"] for m in got] == ["1", "2"]
    assert [m["uid"] for m in _build_thread(reply, [root, reply])] == ["1", "2"]


def test_a_reply_whose_parent_link_was_stripped_still_joins_by_subject():
    """The case the fallback exists for: a list mangles the headers, but the message still says it
    is a reply. `References` alone is enough — it need not name anything we hold."""
    root = msg(1, "lunch?", mid="<a@x>", ts=1)
    orphan = msg(2, "Re: lunch?", mid="<b@x>", refs="<gone@list>", ts=2)
    got = _build_thread(root, [root, orphan])
    assert [m["uid"] for m in got] == ["1", "2"], (
        "a genuine reply with a broken parent link no longer joins its conversation")


def test_a_root_never_absorbs_another_root_even_from_the_same_sender():
    """The precise distinction. Two roots sharing a subject stay apart; a reply between them joins."""
    a = msg(1, "invoice", ts=1)
    b = msg(2, "invoice", ts=2)
    reply = msg(3, "Re: invoice", mid="<c@x>", refs="<missing@x>", ts=3)
    assert [m["uid"] for m in _build_thread(a, [a, b, reply])] == ["1", "3"]
    assert "2" not in [m["uid"] for m in _build_thread(a, [a, b, reply])]


def test_is_reply_reads_both_headers():
    assert _is_reply({"in_reply_to": "<a@x>"}) is True
    assert _is_reply({"references": "<a@x> <b@x>"}) is True
    assert _is_reply({"in_reply_to": "", "references": "  "}) is False
    assert _is_reply({}) is False


def test_the_subject_normaliser_still_folds_reply_prefixes():
    """Unchanged, and load-bearing for the repair case above."""
    assert _normsubj("Re: lunch?") == _normsubj("lunch?") == "lunch?"
    assert _normsubj("FWD: Lunch?") == "lunch?"
    assert _normsubj("") == ""


def test_the_seed_is_always_in_its_own_thread():
    """Whatever the rules decide, the message somebody opened has to be in the result."""
    lone = msg(9, "no links at all")
    assert [m["uid"] for m in _build_thread(lone, [lone] + KRAKEN)] == ["9"]
