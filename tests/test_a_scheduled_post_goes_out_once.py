"""A scheduled post must go out exactly once, or say why it did not.

Run: venv-unified/bin/python -m unittest tests.test_a_scheduled_post_goes_out_once

`app/services/scheduled_posts_service.py` was on a coverage audit's list of modules no test or check
mentioned anywhere. It is 10K of state machine standing between somebody pressing "schedule" and
their post appearing, and every way it can go wrong is silent — a post that never appears looks
exactly like a post that was never scheduled, and one that appears twice is a thing the author finds
out about from somebody else.

The guarantees, each asserted below by driving the real poll pass against a real (sqlite) database:

  once            a due post is claimed pending -> sending -> sent, and a second pass does nothing.
  cancel wins     a cancelled post is never published, even if the poll is mid-flight. The claim
                  filters on `status == "pending"`; cancel flips it first and the claim's rowcount
                  is 0.
  yours only      cancel is scoped by `user_id`. Without that filter any signed-in account could
                  cancel anybody's schedule by guessing a row id.
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
import unittest
from datetime import datetime, timedelta

from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.models import ScheduledPost, User
from app.services import scheduled_posts_service as sched


def _event(text="hello"):
    return {"id": "a" * 64, "pubkey": "b" * 64, "kind": 1, "content": text,
            "created_at": 1, "tags": [], "sig": "c" * 128}


class AScheduledPostGoesOutOnce(unittest.TestCase):

    def setUp(self):
        self.engine = create_engine("sqlite://")            # one in-memory database per test
        User.__table__.create(self.engine)
        ScheduledPost.__table__.create(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.user = User(username="me", password_hash="x", nostr_npub="b" * 64)
        self.other = User(username="them", password_hash="x", nostr_npub="d" * 64)
        self.db.add_all([self.user, self.other])
        self.db.commit()

        # The poll pass opens its OWN session and talks to the relay. Point both at this test.
        self.published = []
        self.answer = (True, "")
        self._real = (sched.SessionLocal, sched.store.publish_event, sched._ss._port)
        sched.SessionLocal = self.Session

        async def publish_event(port, event):
            self.published.append(event)
            return self.answer
        sched.store.publish_event = publish_event
        sched._ss._port = lambda: 3052

    def tearDown(self):
        sched.SessionLocal, sched.store.publish_event, sched._ss._port = self._real
        self.db.close()

    def poll(self):
        asyncio.run(sched._publish_due_once())
        self.db.expire_all()

    def schedule(self, when, user=None, text="hello"):
        return sched.create(self.db, user or self.user, _event(text), when)

    def status(self, row_id):
        return self.db.query(ScheduledPost).filter(ScheduledPost.id == row_id).first()

    # ------------------------------------------------------------------ the happy path, exactly once

    def test_a_due_post_is_published_once_and_marked_sent(self):
        row = self.schedule(datetime.utcnow() - timedelta(minutes=1))
        self.poll()
        self.assertEqual(len(self.published), 1, "a due post was not published exactly once")
        self.assertEqual(self.status(row.id).status, "sent")
        self.poll()
        self.assertEqual(len(self.published), 1,
                         "a second poll published the same post again — the author finds out about "
                         "a double post from somebody else")

    def test_a_post_that_is_not_due_is_left_alone(self):
        row = self.schedule(datetime.utcnow() + timedelta(hours=2))
        self.poll()
        self.assertEqual(self.published, [], "a post scheduled for later went out early")
        self.assertEqual(self.status(row.id).status, "pending")

    # ------------------------------------------------------------------ cancelling

    def test_a_cancelled_post_is_never_published(self):
        row = self.schedule(datetime.utcnow() - timedelta(minutes=1))
        self.assertTrue(sched.cancel(self.db, self.user, row.id))
        self.poll()
        self.assertEqual(self.published, [], "a cancelled post was published anyway")
        self.assertEqual(self.status(row.id).status, "cancelled")

    def test_you_cannot_cancel_somebody_else_s_post(self):
        """The `user_id` filter. Without it, any signed-in account cancels anybody's schedule by
        guessing a row id — and the owner is never told."""
        row = self.schedule(datetime.utcnow() + timedelta(hours=2))
        self.assertFalse(sched.cancel(self.db, self.other, row.id),
                         "another account cancelled this user's scheduled post")
        self.assertEqual(self.status(row.id).status, "pending")

    def test_a_cancel_that_lands_mid_pass_still_stops_the_post(self):
        """THE RACE THE ATOMIC CLAIM EXISTS FOR, and the only way to reach it.

        A poll pass selects every due row first, then claims and publishes them one at a time. A
        cancel pressed in that window has already passed the `due` query — so the row IS in the
        pass's list — and the only thing standing between it and going out is the claim filtering on
        `status == "pending"`, whose rowcount is then 0.

        Cancelling BEFORE the poll (as the test above it does) never reaches that line at all: the
        row is already `cancelled` and the `due` query does not select it. Measured — with the
        status filter deleted from the claim, every other test in this file still passed."""
        first = self.schedule(datetime.utcnow() - timedelta(minutes=2), text="goes out")
        second = self.schedule(datetime.utcnow() - timedelta(minutes=1), text="cancelled mid-pass")

        async def publish_event(port, event):
            self.published.append(event)
            if event["content"] == "goes out":        # the user presses cancel while this is in flight
                sched.cancel(self.db, self.user, second.id)
            return (True, "")
        sched.store.publish_event = publish_event

        self.poll()
        self.assertEqual([e["content"] for e in self.published], ["goes out"],
                         "a post cancelled while the pass was in flight went out anyway — the "
                         "claim is not atomic")
        self.assertEqual(self.status(second.id).status, "cancelled")
        self.assertEqual(self.status(first.id).status, "sent")

    def test_cancelling_loses_to_a_claim_that_already_happened(self):
        """Mid-flight the row is 'sending', and cancel must report that it did NOT take — otherwise
        the UI says cancelled about a post that is going out."""
        row = self.schedule(datetime.utcnow() - timedelta(minutes=1))
        self.db.query(ScheduledPost).filter(ScheduledPost.id == row.id).update({"status": "sending"})
        self.db.commit()
        self.assertFalse(sched.cancel(self.db, self.user, row.id))

    # ------------------------------------------------------------------ failure, retry, recovery

    def test_a_refused_publish_is_retried_rather_than_lost(self):
        row = self.schedule(datetime.utcnow() - timedelta(minutes=1))
        self.answer = (False, "not stored, retry")
        self.poll()
        fresh = self.status(row.id)
        self.assertEqual(fresh.status, "pending",
                         "one refusal killed the post — the local relay answers OK-false for "
                         "recoverable reasons, including its WoT load race after a deploy")
        self.assertEqual(fresh.attempts, 1, "the attempt was not counted, so it can never give up")
        self.answer = (True, "")
        self.poll()
        self.assertEqual(self.status(row.id).status, "sent", "the retry never happened")

    def test_it_gives_up_visibly_rather_than_retrying_for_ever(self):
        row = self.schedule(datetime.utcnow() - timedelta(minutes=1))
        self.answer = (False, "nope")
        self.db.query(ScheduledPost).filter(ScheduledPost.id == row.id).update(
            {"attempts": sched._MAX_ATTEMPTS - 1})
        self.db.commit()
        self.poll()
        fresh = self.status(row.id)
        self.assertEqual(fresh.status, "failed",
                         "it retries for ever, so the author is never told it did not go out")
        self.assertIsNotNone(fresh.sent_at, "a failed row with no resolve time is never pruned")
        self.assertIn("failed", [r["status"] for r in sched.list_for_user(self.db, self.user)],
                      "a failure is not shown to its author, so it vanishes silently")

    def test_a_crash_mid_send_is_recovered_by_the_next_pass(self):
        """A row left 'sending' is orphaned — nothing else owns it on a single instance. Without the
        reset it wedges as a permanent 'sending…' until somebody restarts the process."""
        row = self.schedule(datetime.utcnow() - timedelta(minutes=1))
        self.db.query(ScheduledPost).filter(ScheduledPost.id == row.id).update({"status": "sending"})
        self.db.commit()
        self.poll()
        self.assertEqual(self.status(row.id).status, "sent",
                         "a post orphaned mid-publish was never re-attempted")

    # ------------------------------------------------------------------ pruning and time

    def test_a_failure_is_pruned_on_when_it_RESOLVED_not_when_it_was_made(self):
        """A post scheduled 300 days out and made long ago must keep its ⚠ notice for the full
        window after it actually fails."""
        row = self.schedule(datetime.utcnow() + timedelta(days=200))
        self.db.query(ScheduledPost).filter(ScheduledPost.id == row.id).update(
            {"status": "failed", "sent_at": datetime.utcnow(),
             "created_at": datetime.utcnow() - timedelta(days=300)})
        self.db.commit()
        self.assertEqual(sched._prune_terminal(self.db), 0,
                         "a failure was pruned for being OLD rather than long-resolved, so its "
                         "author never sees that it did not go out")
        self.db.query(ScheduledPost).filter(ScheduledPost.id == row.id).update(
            {"sent_at": datetime.utcnow() - sched._PRUNE_AGE - timedelta(days=1)})
        self.db.commit()
        self.assertEqual(sched._prune_terminal(self.db), 1, "resolved rows are never cleaned up")

    def test_the_time_the_client_is_given_is_utc(self):
        """`scheduled_at` is naive UTC. A naive `.timestamp()` reads it as LOCAL time, which shifts
        every schedule shown in the client by the node's offset — silently, and only off UTC."""
        import calendar
        import os
        import time
        from unittest.mock import patch
        if not hasattr(time, "tzset"):
            self.skipTest("time.tzset is required to exercise a non-UTC host")
        # An ambient UTC timezone would let naive .timestamp() pass. Force an offset even in CI,
        # then restore the process timezone so later tests keep their own environment.
        when = datetime(2030, 1, 15, 12, 30)
        try:
            with patch.dict(os.environ, {"TZ": "EST5"}):
                time.tzset()
                row = self.schedule(when)
                shown = [r for r in sched.list_for_user(self.db, self.user) if r["id"] == row.id][0]
        finally:
            time.tzset()
        # `calendar.timegm` is the canonical "read this naive datetime as UTC", and it comes from a
        # different module than the code under test — restating the implementation's own
        # `.replace(tzinfo=utc).timestamp()` here would assert that the line equals itself.
        expected = calendar.timegm(when.utctimetuple())
        self.assertEqual(shown["scheduled_at"], expected,
                         "the schedule the client is shown is not the UTC instant it was set for; "
                         "on a node off UTC this shifts every schedule by the node's offset")

    def test_sent_and_cancelled_are_not_shown_but_pending_and_failed_are(self):
        keep = self.schedule(datetime.utcnow() + timedelta(hours=1), text="pending one")
        gone = self.schedule(datetime.utcnow() + timedelta(hours=1), text="cancelled one")
        sched.cancel(self.db, self.user, gone.id)
        shown = {r["id"] for r in sched.list_for_user(self.db, self.user)}
        self.assertIn(keep.id, shown)
        self.assertNotIn(gone.id, shown, "a cancelled schedule is still listed as upcoming")

    def test_one_user_never_sees_another_user_s_schedules(self):
        mine = self.schedule(datetime.utcnow() + timedelta(hours=1))
        theirs = self.schedule(datetime.utcnow() + timedelta(hours=1), user=self.other)
        self.assertEqual({r["id"] for r in sched.list_for_user(self.db, self.user)}, {mine.id})
        self.assertEqual({r["id"] for r in sched.list_for_user(self.db, self.other)}, {theirs.id})

    def test_what_is_published_is_the_event_that_was_stored(self):
        """Not a rebuilt one: the event was signed by the author's key before it got here, and
        anything re-serialised differently would not verify."""
        original = _event("exactly these bytes")
        sched.create(self.db, self.user, original, datetime.utcnow() - timedelta(minutes=1))
        self.poll()
        self.assertEqual(self.published, [original])


if __name__ == "__main__":
    unittest.main()
