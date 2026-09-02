"""A CONVERSATION HAS TO SHOW THE USER'S OWN REPLIES, INCLUDING THE ONES WITH NO IDENTITY.

Reported four times, ending with "you didn't fix emails conversations mode showing sent items in
thread" and "do you not understand what I want?". Each previous round fixed something real — sent
folders were not being recognised (`_looks_sent`), and the mailbox scan was reading only the newest
5000 documents (`list_all_messages`) — and the symptom survived both, because neither was the cause.

The cause is in the DATA. Measured on the reporting mailbox:

    17,921 messages · 907 sent
    sent carrying a Message-ID     503
    sent carrying reply headers    366

`_build_thread` closes a graph over Message-ID / In-Reply-To / References. A message with none of
them is not weakly connected to the conversation, it is INVISIBLE to the graph: nothing references
it and it references nothing. 404 of this user's own replies are in that state, because the app did
not set a Message-ID on outgoing mail until that was fixed — and the fix only helps mail sent from
now on. The subject fallback could not save them either: it ran only when the graph found NOTHING,
so any thread that already had two messages never consulted it.

The repair is additive and narrow: pull in a message only when it is the user's OWN SENT mail, has
NO Message-ID (so it is unthreadable by any other means and no evidence is being overridden), and
shares the seed's normalized subject. Measured over 60 threads from the folders that hold the mail:
threads containing a sent message went from 24 to 29, and 5 threads gained replies that no previous
version could reach.

Also here: `_normsubj` stripped only ONE reply prefix, so "Re: Re: Fwd: x" normalised to
"re: fwd: x" and matched nothing. Mail accretes those.
"""
from __future__ import annotations

from app.routers.mail import _build_thread, _normsubj


def msg(uid, folder="INBOX", subject="Quote", mid="", irt="", refs="", ts=0):
    return {"uid": str(uid), "folder": folder, "subject": subject, "message_id": mid,
            "in_reply_to": irt, "references": refs, "ts": ts}


def sent(uid, subject="Quote", mid="", ts=0):
    """A message in a Sent folder — `_is_own_sent` keys on the folder name."""
    return msg(uid, folder="INBOX.Sent", subject=subject, mid=mid, ts=ts)


def uids(thread):
    return {m["uid"] for m in thread}


def test_a_sent_reply_with_no_message_id_joins_the_conversation():
    """THE BUG, with the shape the mailbox actually has: an inbound message with a real ID, and the
    user's reply carrying no identity whatsoever."""
    seed = msg(1, mid="<a@x>", subject="Quote", ts=10)
    mine = sent(2, subject="Re: Quote", ts=20)          # no Message-ID at all
    other = msg(3, mid="<c@x>", subject="Something else", ts=30)
    got = _build_thread(seed, [seed, mine, other])
    assert "2" in uids(got), "the user's own reply is still missing from the thread"
    assert "3" not in uids(got), "an unrelated message was pulled in"


def test_it_still_works_when_the_thread_already_has_two_messages():
    """Why the old subject fallback could not have fixed this: it only ran for a thread of one, and
    a conversation with a reply already in it never reached the fallback at all."""
    seed = msg(1, mid="<a@x>", subject="Quote", ts=10)
    theirs = msg(2, mid="<b@x>", irt="<a@x>", subject="Re: Quote", ts=20)
    mine = sent(3, subject="Re: Quote", ts=30)
    got = _build_thread(seed, [seed, theirs, mine])
    assert uids(got) == {"1", "2", "3"}


def test_a_sent_message_that_has_an_id_is_left_to_the_headers():
    """A subject match is a guess; headers are evidence. Anything threadable must not be threaded by
    guesswork — otherwise two unrelated conversations that share a subject merge."""
    # The thread must already hold two messages, or this exercises the OLDER headerless fallback
    # (thread-of-one → group by subject) instead, which is deliberate and predates this repair.
    seed = msg(1, mid="<a@x>", subject="Invoice", ts=10)
    theirs = msg(9, mid="<b@x>", irt="<a@x>", subject="Re: Invoice", ts=20)
    unrelated_sent = sent(2, subject="Invoice", mid="<z@x>", ts=99)   # has an ID, links to nothing
    got = _build_thread(seed, [seed, theirs, unrelated_sent])
    assert "2" not in uids(got), (
        "a sent message with its own Message-ID was merged on subject alone — that is a guess "
        "overriding evidence, and it merges unrelated conversations that share a subject")


def test_an_empty_subject_pulls_in_nothing():
    """Otherwise every subject-less sent message joins every subject-less thread."""
    seed = msg(1, mid="<a@x>", subject="", ts=10)
    mine = sent(2, subject="", ts=20)
    got = _build_thread(seed, [seed, mine])
    assert uids(got) == {"1"}


def test_a_different_subject_is_not_pulled_in():
    seed = msg(1, mid="<a@x>", subject="Quote", ts=10)
    mine = sent(2, subject="Holiday", ts=20)
    assert "2" not in uids(_build_thread(seed, [seed, mine]))


def test_the_thread_stays_in_time_order():
    seed = msg(1, mid="<a@x>", subject="Quote", ts=50)
    mine = sent(2, subject="Re: Quote", ts=10)
    got = _build_thread(seed, [seed, mine])
    assert [m["uid"] for m in got] == ["2", "1"]


def test_no_duplicates_when_a_message_is_reachable_both_ways():
    seed = msg(1, mid="<a@x>", subject="Quote", ts=10)
    mine = msg(2, folder="INBOX.Sent", subject="Re: Quote", irt="<a@x>", ts=20)  # linked AND matching
    got = _build_thread(seed, [seed, mine])
    assert len(got) == 2 and uids(got) == {"1", "2"}


# ── the subject normaliser ───────────────────────────────────────────────────────────────────────

def test_repeated_reply_prefixes_are_all_stripped():
    """"Re: Re: Fwd: quote" is an ordinary subject after a few round trips, and one strip left
    "re: fwd: quote" — which matches nothing, so the repair above would never fire on real mail."""
    assert _normsubj("Re: Re: Fwd: Quote") == "quote"
    assert _normsubj("RE: FW: re: Quote") == "quote"
    assert _normsubj("Fwd: Quote") == _normsubj("Quote") == "quote"


def test_a_subject_that_is_only_prefixes_does_not_hang_or_match_everything():
    assert _normsubj("Re: Re: Re:") == ""
    assert _normsubj("Re: " * 500) == ""      # any depth, not a capped number of them


def test_a_subject_containing_re_is_not_mangled():
    """Only a LEADING prefix is a prefix — "Regarding" and a mid-subject "re:" are content."""
    assert _normsubj("Regarding the quote") == "regarding the quote"
    assert _normsubj("Quote re: the roof") == "quote re: the roof"
