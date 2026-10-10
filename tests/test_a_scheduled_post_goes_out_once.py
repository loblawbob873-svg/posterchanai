"""A scheduled post must go out exactly once, or say why it did not.

Run: venv-unified/bin/python -m pytest tests/test_a_scheduled_post_goes_out_once.py

`app/services/scheduled_posts_service.py` was on a coverage audit's list of modules no test or check
mentioned anywhere. It is 10K of state machine standing between somebody pressing "schedule" and
their post appearing, and every way it can go wrong is silent — a post that never appears looks
exactly like a post that was never scheduled, and one that appears twice is a thing the author finds
out about from somebody else.

The guarantees, each asserted below by driving the real poll pass against the shipped relay (the schedules
live in the `scheduled_posts` DocTable since #161 moved them off Postgres; tests/app_tables_harness.py):

  once            a due post is claimed pending -> sending -> sent, and a second pass does nothing.
  cancel wins     a cancelled post is never published, even if the poll is mid-flight. The claim
                  re-reads the row under the lock a cancel takes and only takes a `pending` one.
  yours only      cancel is scoped by `user_id`. Without that filter any signed-in account could
                  cancel anybody's schedule by guessing a row id.
  deleted owner   deleting an account mid-publish removes its queued rows without aborting
                  another user's due posts or recreating deleted rows after the send completes.
  could not ask   a relay that cannot be read publishes nothing and changes nothing; a claim the
                  relay did not confirm is never published.
  not yet         a post scheduled for later is left alone.
  retried         a relay that says no keeps the post PENDING and counts an attempt; it is only
                  marked `failed` after _MAX_ATTEMPTS. Fast-failing on one rejection would kill a
                  note the relay would have accepted moments later (its WoT load race after a
                  deploy legitimately answers OK-false).
  recovered       a row left `sending` by a crash is reset to pending by the NEXT pass, not left
                  wedged until someone restarts the process.
  kept            terminal rows are pruned on `sent_at` (when they resolved), never `created_at` —
                  a post scheduled 300 days out must keep its failure notice for the full window
                  after it fails, not be pruned the instant it fails for having been made long ago.
  UTC             `scheduled_at` is a NAIVE UTC datetime, and a naive `.timestamp()` reads it as
                  LOCAL time. On any node not on UTC that silently shifts every schedule the client
                  displays by the offset.
"""
import asyncio
import json
from datetime import datetime, timedelta
from types import SimpleNamespace

import pytest

from app.services import app_tables
from app.services import scheduled_posts_service as sched
from tests.app_tables_harness import tables, shared_relay, no_relay, fresh_process_view  # noqa: F401

USER, OTHER = SimpleNamespace(id=1), SimpleNamespace(id=2)


def _event(text="hello"):
    return {"id": "a" * 64, "pubkey": "b" * 64, "kind": 1, "content": text,
            "created_at": 1, "tags": [], "sig": "c" * 128}


class World:
    def __init__(self, monkeypatch):
        self.published = []
        self.answer = (True, "")

        async def publish_event(port, event):
            self.published.append(event)
            return self.answer
        monkeypatch.setattr(sched.store, "publish_event", publish_event)
        monkeypatch.setattr(sched._ss, "_port", lambda: 3052)
        self.monkeypatch = monkeypatch

    def on_publish(self, fn):
        async def publish_event(port, event):
            self.published.append(event)
            return await fn(event)
        self.monkeypatch.setattr(sched.store, "publish_event", publish_event)

    def poll(self):
        asyncio.run(sched._publish_due_once())

    def schedule(self, when, user=None, text="hello"):
        return asyncio.run(sched.create(None, user or USER, _event(text), when))

    def cancel(self, user, rid):
        return asyncio.run(sched.cancel(None, user, rid))

    def listing(self, user):
        return asyncio.run(sched.list_for_user(None, user))

    def status(self, row_id):
        """The row as the RELAY holds it (a fresh process's strict load), or None."""
        row = fresh_process_view("scheduled_posts").get(str(row_id))
        return sched.as_post(row_id, row) if row else None

    def update(self, row_id, **fields):
        t = fresh_process_view("scheduled_posts")
        row = t.get(str(row_id))
        row.update({k: (v.isoformat() if isinstance(v, datetime) else v) for k, v in fields.items()})
        t.put(str(row_id), row)


@pytest.fixture
def w(tables, monkeypatch):
    return World(monkeypatch)


# ------------------------------------------------------------------ the happy path, exactly once

def test_a_due_post_is_published_once_and_marked_sent(w):
    row = w.schedule(datetime.utcnow() - timedelta(minutes=1))
    w.poll()
    assert len(w.published) == 1, "a due post was not published exactly once"
    assert w.status(row.id).status == "sent"
    w.poll()
    assert len(w.published) == 1, ("a second poll published the same post again — the author finds out about "
                                   "a double post from somebody else")


def test_a_post_that_is_not_due_is_left_alone(w):
    row = w.schedule(datetime.utcnow() + timedelta(hours=2))
    w.poll()
    assert w.published == [], "a post scheduled for later went out early"
    assert w.status(row.id).status == "pending"


# ------------------------------------------------------------------ cancelling

def test_a_cancelled_post_is_never_published(w):
    row = w.schedule(datetime.utcnow() - timedelta(minutes=1))
    assert w.cancel(USER, row.id)
    w.poll()
    assert w.published == [], "a cancelled post was published anyway"
    assert w.status(row.id).status == "cancelled"


def test_you_cannot_cancel_somebody_else_s_post(w):
    """The `user_id` check. Without it, any signed-in account cancels anybody's schedule by
    guessing a row id — and the owner is never told."""
    row = w.schedule(datetime.utcnow() + timedelta(hours=2))
    assert not w.cancel(OTHER, row.id), "another account cancelled this user's scheduled post"
    assert w.status(row.id).status == "pending"


def test_a_cancel_that_lands_mid_pass_still_stops_the_post(w):
    """THE RACE THE CLAIM EXISTS FOR, and the only way to reach it.

    A poll pass selects every due row first, then claims and publishes them one at a time. A
    cancel pressed in that window has already passed the `due` selection — so the row IS in the
    pass's list — and the only thing standing between it and going out is the claim re-reading the
    row and taking only a `pending` one.

    Cancelling BEFORE the poll (as the test above it does) never reaches that line at all: the
    row is already `cancelled` and the `due` selection does not pick it."""
    first = w.schedule(datetime.utcnow() - timedelta(minutes=2), text="goes out")
    second = w.schedule(datetime.utcnow() - timedelta(minutes=1), text="cancelled mid-pass")

    async def cancel_second(event):
        if event["content"] == "goes out":        # the user presses cancel while this is in flight
            assert await sched.cancel(None, USER, second.id)
        return (True, "")
    w.on_publish(cancel_second)

    w.poll()
    assert [e["content"] for e in w.published] == ["goes out"], \
        "a post cancelled while the pass was in flight went out anyway — the claim is not atomic"
    assert w.status(second.id).status == "cancelled"
    assert w.status(first.id).status == "sent"


def test_account_deletion_mid_publish_skips_its_queue_and_keeps_other_users_moving(w):
    """Deleting an account removes its rows (the old FK cascade is `app_tables.purge_user` now), possibly
    while this poll already holds them in its due list. The pass must skip them — not publish them, not
    abort the batch, and not recreate a deleted row when the in-flight send completes."""
    now = datetime.utcnow()
    first = w.schedule(now - timedelta(minutes=3), text="already in flight")
    queued = w.schedule(now - timedelta(minutes=2), text="deleted account's queued post")
    survivor = w.schedule(now - timedelta(minutes=1), user=OTHER, text="other user's post")

    async def delete_owner(event):
        if event["content"] == "already in flight":
            await app_tables.apurge_user(USER.id)
            # Prove the rows are gone ON THE RELAY, not merely hidden from this process.
            view = fresh_process_view("scheduled_posts")
            assert str(queued.id) not in await view.aall()
        return True, ""
    w.on_publish(delete_owner)

    w.poll()
    assert [e["content"] for e in w.published] == ["already in flight", "other user's post"], \
        "deleted-account rows must not publish or abort the remaining due batch"
    assert w.status(first.id) is None, "a completed send recreated a deleted account's row"
    assert w.status(queued.id) is None
    assert w.status(survivor.id).status == "sent"
    w.poll()
    assert len(w.published) == 2, "the surviving post was retried after success"


def test_cancelling_loses_to_a_claim_that_already_happened(w):
    """Mid-flight the row is 'sending', and cancel must report that it did NOT take — otherwise
    the UI says cancelled about a post that is going out."""
    row = w.schedule(datetime.utcnow() - timedelta(minutes=1))
    w.update(row.id, status="sending")
    assert not w.cancel(USER, row.id)


# ------------------------------------------------------------------ failure, retry, recovery

def test_a_refused_publish_is_retried_rather_than_lost(w):
    row = w.schedule(datetime.utcnow() - timedelta(minutes=1))
    w.answer = (False, "not stored, retry")
    w.poll()
    fresh = w.status(row.id)
    assert fresh.status == "pending", ("one refusal killed the post — the local relay answers OK-false for "
                                       "recoverable reasons, including its WoT load race after a deploy")
    assert fresh.attempts == 1, "the attempt was not counted, so it can never give up"
    w.answer = (True, "")
    w.poll()
    assert w.status(row.id).status == "sent", "the retry never happened"


def test_it_gives_up_visibly_rather_than_retrying_for_ever(w):
    row = w.schedule(datetime.utcnow() - timedelta(minutes=1))
    w.answer = (False, "nope")
    w.update(row.id, attempts=sched._MAX_ATTEMPTS - 1)
    w.poll()
    fresh = w.status(row.id)
    assert fresh.status == "failed", "it retries for ever, so the author is never told it did not go out"
    assert fresh.sent_at is not None, "a failed row with no resolve time is never pruned"
    assert "failed" in [r["status"] for r in w.listing(USER)], \
        "a failure is not shown to its author, so it vanishes silently"


def test_a_crash_mid_send_is_recovered_by_the_next_pass(w):
    """A row left 'sending' is orphaned — nothing else owns it on a single instance. Without the
    reset it wedges as a permanent 'sending…' until somebody restarts the process."""
    row = w.schedule(datetime.utcnow() - timedelta(minutes=1))
    w.update(row.id, status="sending")
    w.poll()
    assert w.status(row.id).status == "sent", "a post orphaned mid-publish was never re-attempted"


# ------------------------------------------------------------------ pruning and time

def test_a_failure_is_pruned_on_when_it_RESOLVED_not_when_it_was_made(w):
    """A post scheduled 300 days out and made long ago must keep its ⚠ notice for the full
    window after it actually fails."""
    row = w.schedule(datetime.utcnow() + timedelta(days=200))
    w.update(row.id, status="failed", sent_at=datetime.utcnow(),
             created_at=datetime.utcnow() - timedelta(days=300))
    prune = lambda: asyncio.run(sched._prune_terminal(fresh_process_view("scheduled_posts")))  # noqa: E731
    assert prune() == 0, ("a failure was pruned for being OLD rather than long-resolved, so its "
                          "author never sees that it did not go out")
    w.update(row.id, sent_at=datetime.utcnow() - sched._PRUNE_AGE - timedelta(days=1))
    assert prune() == 1, "resolved rows are never cleaned up"
    assert w.status(row.id) is None


def test_the_time_the_client_is_given_is_utc(w):
    """`scheduled_at` is naive UTC. A naive `.timestamp()` reads it as LOCAL time, which shifts
    every schedule shown in the client by the node's offset — silently, and only off UTC."""
    import calendar
    import os
    import time
    from unittest.mock import patch
    if not hasattr(time, "tzset"):
        pytest.skip("time.tzset is required to exercise a non-UTC host")
    # An ambient UTC timezone would let naive .timestamp() pass. Force an offset even in CI,
    # then restore the process timezone so later tests keep their own environment.
    when = datetime(2030, 1, 15, 12, 30)
    try:
        with patch.dict(os.environ, {"TZ": "EST5"}):
            time.tzset()
            row = w.schedule(when)
            shown = [r for r in w.listing(USER) if r["id"] == row.id][0]
    finally:
        time.tzset()
    # `calendar.timegm` is the canonical "read this naive datetime as UTC", and it comes from a
    # different module than the code under test — restating the implementation's own
    # `.replace(tzinfo=utc).timestamp()` here would assert that the line equals itself.
    expected = calendar.timegm(when.utctimetuple())
    assert shown["scheduled_at"] == expected, (
        "the schedule the client is shown is not the UTC instant it was set for; "
        "on a node off UTC this shifts every schedule by the node's offset")


def test_sent_and_cancelled_are_not_shown_but_pending_and_failed_are(w):
    keep = w.schedule(datetime.utcnow() + timedelta(hours=1), text="pending one")
    gone = w.schedule(datetime.utcnow() + timedelta(hours=1), text="cancelled one")
    w.cancel(USER, gone.id)
    shown = {r["id"] for r in w.listing(USER)}
    assert keep.id in shown
    assert gone.id not in shown, "a cancelled schedule is still listed as upcoming"


def test_one_user_never_sees_another_user_s_schedules(w):
    mine = w.schedule(datetime.utcnow() + timedelta(hours=1))
    theirs = w.schedule(datetime.utcnow() + timedelta(hours=1), user=OTHER)
    assert {r["id"] for r in w.listing(USER)} == {mine.id}
    assert {r["id"] for r in w.listing(OTHER)} == {theirs.id}


def test_what_is_published_is_the_event_that_was_stored(w):
    """Not a rebuilt one: the event was signed by the author's key before it got here, and
    anything re-serialised differently would not verify."""
    original = _event("exactly these bytes")
    asyncio.run(sched.create(None, USER, original, datetime.utcnow() - timedelta(minutes=1)))
    w.poll()
    assert w.published == [original]


# ------------------------------------------------------------------ could not ask (#161)

def test_listed_soonest_first(w):
    late = w.schedule(datetime.utcnow() + timedelta(hours=3))
    soon = w.schedule(datetime.utcnow() + timedelta(hours=1))
    assert [r["id"] for r in w.listing(USER)] == [soon.id, late.id]


def test_an_unreachable_relay_publishes_nothing_and_answers_unavailable(no_relay, monkeypatch):
    from app.services.relay_reader import Unavailable
    w = World(monkeypatch)
    w.poll()                                       # must not raise out of the pass either
    assert w.published == []
    for call in (lambda: w.listing(USER), lambda: w.cancel(USER, 1),
                 lambda: w.schedule(datetime.utcnow())):
        with pytest.raises(Unavailable):
            call()


def test_a_claim_the_relay_did_not_confirm_is_never_published(w, monkeypatch):
    """The claim (pending → sending) is written to the relay BEFORE the broadcast. If that write is not
    confirmed nobody knows whether it landed, so nothing goes out this pass; the post is still there for the
    next one, and goes out once."""
    row = w.schedule(datetime.utcnow() - timedelta(minutes=1))
    from app.services import doc_table
    real = doc_table.nostr_store.put_doc

    async def refuse(*a, **k):
        return False
    monkeypatch.setattr(doc_table.nostr_store, "put_doc", refuse)
    w.poll()
    assert w.published == [], "a post was broadcast on the strength of a claim the relay never confirmed"
    monkeypatch.setattr(doc_table.nostr_store, "put_doc", real)
    w.poll()
    assert len(w.published) == 1 and w.status(row.id).status == "sent"


def test_rows_round_trip_every_field(w):
    when = datetime(2030, 1, 1, 9, 30)
    ev = _event("fields")
    row = asyncio.run(sched.create(None, USER, ev, when))
    got = w.status(row.id)
    assert (got.user_id, got.event_id, got.scheduled_at, got.status, got.attempts) == (1, ev["id"], when, "pending", 0)
    assert json.loads(got.event_json) == ev and got.content_preview == "fields" and got.created_at is not None
