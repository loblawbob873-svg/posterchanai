"""MAIL WE SEND MUST CARRY AN ID WE KEEP, OR NOTHING CAN EVER THREAD IT.

Reported as "threads is better but it's missing the sent items. I imagine it like gmail you just
scroll down".

The copy IS filed — `send_email` appends to the account's Sent folder, resolving it through the
RFC 6154 \\Sent flag — and `sync_all` syncs every folder, and `/thread` reads across all of them.
Every part of the path was right except one header: nothing ever set `Message-ID`.

So the MTA assigned one on the way out. The recipient replies quoting an id this mailbox has never
seen, and the copy appended to Sent carries none at all — the reference graph cannot link either
direction. A conversation the user STARTED showed the reply and not their original; their own reply
to an inbound mail did thread, because a reply at least carries In-Reply-To. That is exactly "the
sent items are missing".

Two halves, because one of them cannot fix the past:

  * `Message-ID` is generated before sending, so the id that goes out is the id stored in Sent.
    Everything sent from now on threads on headers, which is what Gmail does.
  * Everything sent BEFORE has no id and never will. The subject fallback was deliberately bounded
    to replies (two roots are never one conversation — that is what stopped four separate Kraken
    notices becoming one thread), and that bound also excluded the one root that does belong: your
    own outgoing message. `_is_own_sent` re-admits exactly that, and nothing else.
"""
from __future__ import annotations

import re
from pathlib import Path

from app.routers.mail import _build_thread, _is_own_sent, _is_reply

ROOT = Path(__file__).resolve().parents[1]
SEND = (ROOT / "app/services/mail_service.py").read_text(encoding="utf-8")


def msg(uid, folder="INBOX", logical="", mid="", irt="", refs="", subject="Project kickoff", ts=0):
    return {"uid": uid, "folder": folder, "logical": logical, "message_id": mid,
            "in_reply_to": irt, "references": refs, "subject": subject, "ts": ts}


def test_outgoing_mail_is_given_a_message_id():
    """THE ROOT CAUSE. Without this the id is the MTA's and this mailbox never learns it."""
    assert 'msg["Message-ID"]' in SEND, (
        "outgoing mail carries no Message-ID again — the copy in Sent cannot be threaded to the "
        "reply it produces, in either direction")
    assert "make_msgid" in SEND


def test_the_id_is_set_before_the_message_is_sent_and_appended():
    """Both halves must see the SAME id: the one that goes out and the one stored in Sent. Set
    after either, they would differ and this would fix nothing while looking fixed."""
    at_id = SEND.index('msg["Message-ID"]')
    assert at_id < SEND.index("smtp.sendmail("), "the id is set after the message has gone out"
    assert at_id < SEND.index("imap.append("), "the id is set after the copy has been filed"


def test_a_sent_root_and_its_reply_are_one_conversation():
    """The Gmail shape: I write, they reply, it is ONE thread you scroll down."""
    sent = msg("1", folder="Sent", logical="Sent", mid="<mine@ours>", ts=10)
    back = msg("2", mid="<theirs@them>", irt="<mine@ours>", subject="Re: Project kickoff", ts=20)
    got = _build_thread(sent, [sent, back])
    assert [m["uid"] for m in got] == ["1", "2"], "a reply to our own mail is not in its thread"


def test_opening_the_reply_finds_our_sent_original_too():
    """It must group the same way from either end — that asymmetry was a bug once already."""
    sent = msg("1", folder="Sent", logical="Sent", mid="<mine@ours>", ts=10)
    back = msg("2", mid="<theirs@them>", irt="<mine@ours>", subject="Re: Project kickoff", ts=20)
    got = _build_thread(back, [sent, back])
    assert [m["uid"] for m in got] == ["1", "2"]


def test_history_with_no_ids_still_groups_through_the_subject():
    """Everything sent before the fix has no Message-ID and never will. This is what rescues it —
    and it has to be checked from the REPLY end, which is the end the user opens from.

    Opening from the sent side proves nothing: the reply carries In-Reply-To/References of its own,
    so `_is_reply` admits it and the thread looks complete whether or not our own sent mail is
    exempt. Only the other direction isolates the rule — the idless sent root is admitted by
    `_is_own_sent` or not at all. (Written the wrong way round first, and the mutation that removed
    the exemption passed it.)"""
    old_sent = msg("1", folder="Sent", logical="Sent", ts=10)
    reply = msg("2", mid="<theirs@them>", refs="<gone@mta>", subject="Re: Project kickoff", ts=20)
    got = _build_thread(reply, [old_sent, reply])
    assert [m["uid"] for m in got] == ["1", "2"], (
        "an idless sent message is stranded — opening the reply shows their side of the "
        "conversation and not yours, which is 'the sent items are missing'")


def test_the_same_history_groups_from_either_end():
    """It must not depend on which message you clicked."""
    old_sent = msg("1", folder="Sent", logical="Sent", ts=10)
    reply = msg("2", mid="<theirs@them>", refs="<gone@mta>", subject="Re: Project kickoff", ts=20)
    assert [m["uid"] for m in _build_thread(old_sent, [old_sent, reply])] == ["1", "2"]


def test_two_inbound_roots_are_still_not_one_thread():
    """THE REGRESSION THIS MUST NOT REINTRODUCE. Four separate Kraken notices, each a root, each
    its own event, days apart, arrived as one thread. Widening the fallback for our own sent mail
    must not widen it for theirs."""
    a = msg("1", mid="<k1@kraken>", subject="Your Kraken account", ts=10)
    b = msg("2", mid="<k2@kraken>", subject="Your Kraken account", ts=99999)
    got = _build_thread(a, [a, b])
    assert [m["uid"] for m in got] == ["1"], "two inbound roots are being merged again"


def test_only_the_sent_folder_gets_the_exemption():
    """Narrow on purpose: it is 'this is my side of the conversation', not 'subjects match'."""
    assert _is_own_sent(msg("1", folder="Sent", logical="Sent")) is True
    for logical in ("", "INBOX", "Archive", "Drafts", "Trash", "Spam"):
        assert _is_own_sent(msg("1", logical=logical)) is False, f"{logical} counts as sent"


def test_a_reply_is_still_a_reply_however_it_is_filed():
    """The original bound is untouched — this adds a case, it does not replace one."""
    assert _is_reply(msg("1", irt="<x@y>")) is True
    assert _is_reply(msg("1", refs="<x@y>")) is True
    assert _is_reply(msg("1")) is False
