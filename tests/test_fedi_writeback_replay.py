"""Regression tests: a write-back INTERACTION must be performed exactly once, ever.

Run: venv-unified/bin/python -m unittest tests.test_fedi_writeback_replay

The incident (2026-07-28): several fediverse instances reported us for reacting to the same post
~100 times over 8 hours. Nothing was retrying and nothing was looping — the service subscribes with a
6h lookback, `_seen_events` is an in-process set, and every service RESTART therefore replayed the
last 6 hours of the user's reactions and re-performed each one. A day of ordinary deploys was enough.

The write-back had a durable guard for kind-1 replies (FediBridgeDelivered) and none for kind-7/6,
because the fediverse calls were believed to be "server-idempotent". They are idempotent about the
instance's own STATE and not about FEDERATION: each call re-emits the Like/EmojiReact/Announce to the
target's instance, so the author gets a fresh notification every single time.

Pinned here:
  - an interaction already recorded is NOT re-sent on a replay, for both kind-7 and kind-6
  - an UNDONE interaction is not resurrected by a later replay of the still-live kind-7
  - undoing TOMBSTONES the row instead of deleting it (deleting it is what would allow the above)
  - a genuinely new interaction is still performed, and recorded so the next replay is a no-op
These use a real SQLAlchemy session, not a mocked query, so the guard is exercised as it runs.
"""
import asyncio
import unittest
from datetime import datetime
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, FediBridgeAction, FediBridgeDelivered
from app.services import fedi_nostr_writeback_service as wb

PK = "b" * 64
EID = "a" * 64
INST = "https://detroitriotcity.com"
TARGET = "B8nx2nyVCPAdqoO89I"


def _ev(kind=7, content="\U0001f525", eid=EID):
    return {"id": eid, "pubkey": PK, "kind": kind, "content": content,
            "tags": [["e", "c" * 64], ["p", "d" * 64]]}


class _Base(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine, tables=[FediBridgeAction.__table__])
        self.db = sessionmaker(bind=self.engine)()
        wb._seen_events.clear()

        self.user = mock.Mock(pleroma_instance_url=INST, pleroma_access_token="tok",
                              username="verita84@poster.place", fedi_crosspost_enabled=False)
        self.fav = mock.AsyncMock()
        self.react = mock.AsyncMock()
        self.reblog = mock.AsyncMock()
        self.unreact = mock.AsyncMock()
        self.unreblog = mock.AsyncMock()
        self.unfav = mock.AsyncMock()
        self.p = [
            mock.patch.object(wb, "_bridge_allowed_pubkeys", lambda: frozenset({PK})),
            mock.patch.object(wb, "_user_for_pubkey", lambda db, pk: self.user),
            mock.patch.object(wb, "_target_row", lambda db, ev: mock.Mock(author_acct="x@y")),
            mock.patch.object(wb, "_resolve_target_id", mock.AsyncMock(return_value=TARGET)),
            mock.patch.object(wb.pleroma_service, "favourite_status", self.fav),
            mock.patch.object(wb.pleroma_service, "emoji_react", self.react),
            mock.patch.object(wb.pleroma_service, "reblog_status", self.reblog),
            mock.patch.object(wb.pleroma_service, "emoji_unreact", self.unreact),
            mock.patch.object(wb.pleroma_service, "unreblog_status", self.unreblog),
            mock.patch.object(wb.pleroma_service, "unfavourite_status", self.unfav),
        ]
        for p in self.p:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.p])
        self.addCleanup(self.db.close)

    def handle(self, ev):
        asyncio.run(wb._handle(self.db, ev))

    def seed(self, action="react", emoji="\U0001f525", undone=False, eid=EID):
        row = FediBridgeAction(nostr_event_id=eid, nostr_pubkey=PK, platform="pleroma",
                               instance_url=INST, target_id=TARGET, action=action, emoji=emoji,
                               undone_at=datetime.utcnow() if undone else None)
        self.db.add(row)
        self.db.commit()
        return row

    @property
    def sent(self):
        return self.fav.call_count + self.react.call_count + self.reblog.call_count


class TestReplayDoesNotRepeat(_Base):
    def test_a_recorded_reaction_is_not_sent_again(self):
        self.seed()
        self.handle(_ev())
        self.assertEqual(self.sent, 0, "a replay re-sent a reaction that was already performed")

    def test_a_recorded_repeat_is_not_sent_again(self):
        self.seed(action="reblog", emoji=None)
        self.handle(_ev(kind=6, content=""))
        self.assertEqual(self.sent, 0, "a replay re-sent a repeat that was already performed")

    def test_a_recorded_favourite_is_not_sent_again(self):
        # The plain NIP-25 like ('+') takes the favourite path, which was equally affected.
        self.seed(action="favourite", emoji=None)
        self.handle(_ev(content="+"))
        self.assertEqual(self.sent, 0)

    def test_many_replays_still_send_nothing(self):
        # The shape of the incident: one interaction, a day of restarts.
        self.seed()
        for _ in range(20):
            wb._seen_events.clear()      # what a restart does
            self.handle(_ev())
        self.assertEqual(self.sent, 0)

    def test_an_undone_reaction_is_not_resurrected(self):
        # The kind-7 is still live and still inside the lookback window, so it WILL be replayed after
        # the user removes the reaction. It must stay removed.
        self.seed(undone=True)
        self.handle(_ev())
        self.assertEqual(self.sent, 0, "a replay put back a reaction the user had deleted")


class TestFirstTimeStillWorks(_Base):
    def test_a_new_reaction_is_performed_and_recorded(self):
        self.handle(_ev())
        self.react.assert_awaited_once()
        row = self.db.query(FediBridgeAction).filter(FediBridgeAction.nostr_event_id == EID).first()
        self.assertIsNotNone(row, "the action was performed but not recorded — the next replay repeats it")
        self.assertEqual(row.action, "react")

    def test_a_new_repeat_is_performed(self):
        self.handle(_ev(kind=6, content=""))
        self.reblog.assert_awaited_once()

    def test_a_different_event_on_the_same_target_still_goes(self):
        # Re-reacting later is a NEW kind-7 with its own id; the guard is per event, not per target.
        self.seed()
        self.handle(_ev(eid="f" * 64))
        self.assertEqual(self.sent, 1)

    def test_performed_once_then_never_again(self):
        self.handle(_ev())
        self.assertEqual(self.sent, 1)
        for _ in range(5):
            wb._seen_events.clear()
            self.handle(_ev())
        self.assertEqual(self.sent, 1, "the recorded row did not suppress the replay")


class TestUndoTombstones(_Base):
    def _undo(self):
        return asyncio.run(wb._undo_actions(self.db, self.user, {"pubkey": PK}, EID))

    def test_undo_keeps_the_row_as_a_marker(self):
        self.seed()
        found, ok = self._undo()
        self.assertTrue(found and ok)
        self.unreact.assert_awaited_once()
        row = self.db.query(FediBridgeAction).filter(FediBridgeAction.nostr_event_id == EID).first()
        self.assertIsNotNone(row, "the row was deleted — a replay can now resurrect the reaction")
        self.assertIsNotNone(row.undone_at)

    def test_undo_is_not_repeated_for_an_already_undone_row(self):
        self.seed(undone=True)
        found, ok = self._undo()
        self.assertFalse(found)
        self.unreact.assert_not_awaited()


class TestDeleteHappensOnce(_Base):
    """A NIP-09 delete must delete the fediverse status once, not once per restart.

    Same replay shape as the reactions, one floor down: the FediBridgeDelivered row deliberately
    SURVIVES the delete (it is what keeps the mirror from re-importing the post as a puppet note), so
    before `deleted_at` the replay found a live note_id and re-issued the delete every time. Harmless
    to other users — a delete notifies nobody — but it ran on every restart forever.
    """
    def setUp(self):
        super().setUp()
        Base.metadata.create_all(self.engine, tables=[FediBridgeDelivered.__table__])
        self.delete = mock.AsyncMock()
        p = mock.patch.object(wb.pleroma_service, "delete_status", self.delete)
        p.start(); self.addCleanup(p.stop)

    def _delivered(self, note_id="B8nrZR6odSculTSANs", deleted=False):
        self.db.add(FediBridgeDelivered(
            platform="pleroma", instance_url=INST, note_id=note_id, note_uri=None, author_acct=None,
            nostr_event_id=EID, nostr_pubkey=PK,
            deleted_at=datetime.utcnow() if deleted else None))
        self.db.commit()

    def _kind5(self):
        return {"id": "e" * 64, "pubkey": PK, "kind": 5, "content": "", "tags": [["e", EID]]}

    def _run(self):
        return asyncio.run(wb._delete_federated(self.db, self.user, self._kind5()))

    def test_first_delete_goes_and_marks_the_row(self):
        self._delivered()
        self.assertTrue(self._run())
        self.delete.assert_awaited_once()
        row = self.db.query(FediBridgeDelivered).filter(
            FediBridgeDelivered.nostr_event_id == EID).first()
        self.assertIsNotNone(row, "the row must survive — it keeps the mirror off the post")
        self.assertIsNotNone(row.deleted_at)

    def test_replays_do_not_delete_again(self):
        self._delivered()
        self._run()
        for _ in range(10):
            wb._seen_events.clear()      # what a restart does
            self._run()
        self.assertEqual(self.delete.await_count, 1, "a replay re-deleted an already-deleted status")

    def test_an_already_deleted_row_is_skipped(self):
        self._delivered(deleted=True)
        self._run()
        self.delete.assert_not_awaited()

    def test_a_tombstone_row_is_still_skipped(self):
        self._delivered(note_id="")      # deleted before it ever federated
        self._run()
        self.delete.assert_not_awaited()


if __name__ == "__main__":
    unittest.main()
