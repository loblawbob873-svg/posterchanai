"""The Blossom auto-clean must never delete encrypted-drive content.

Run: venv-unified/bin/python -m unittest tests.test_blossom_keep

`_cleanup_once` deletes blobs two ways: an explicit per-blob `expires_at`, and — the dangerous one —
ANY blob older than the admin's `blossom_blob_ttl_days`, applied live to blobs already stored. That
is fine for chat media (the message renders the loss as a broken image) and catastrophic for the
client-side encrypted drive: Notes attachments, music tracks and the files index are ciphertext this
node holds the only copy of, under a key the server does not have. Nobody would notice until they
opened a note and the picture was gone, and nothing could bring it back.

So those uploads set `keep`, and no rule in the sweep may touch a `keep` blob. The failure mode being
guarded is specifically "an admin sets a TTL a year after the drive was uploaded" — the setting is
read live, so it is retroactive by default.

`_cleanup_once` swallows exceptions and returns 0, so a broken query would look exactly like a clean
sweep. Every test here therefore asserts in BOTH directions: the drive survived AND the ordinary
expired blobs actually went.
"""
import time
import unittest
from unittest import mock

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, BlossomBlob
from app.services import blossom_service

DAY = 86400


def _mk(session, sha, *, age_days=0, keep=False, expires_at=None):
    session.add(BlossomBlob(
        sha256=sha * 64, pubkey="a" * 64, size=10, mime="application/octet-stream",
        created_at=int(time.time()) - age_days * DAY, expires_at=expires_at,
        storage="local", path="/tmp/" + sha, private=False, keep=keep))
    session.commit()


class TestCleanupHonoursKeep(unittest.TestCase):
    def setUp(self):
        # Only blossom_blobs is needed, but create_all is simplest and the schema is the real one —
        # a test against a hand-written table would not catch a column that never got migrated.
        self.engine = create_engine("sqlite://")
        Base.metadata.create_all(self.engine, tables=[BlossomBlob.__table__])
        self.Session = sessionmaker(bind=self.engine)
        self.session = self.Session()

    def tearDown(self):
        self.session.close()

    def _sweep(self, ttl_days):
        """Run the real _cleanup_once against this in-memory DB."""
        cfg = {"ttl_days": ttl_days, "backend": "local", "blob_dir": "/tmp",
               "storage_url": "", "cache_mb": 0}
        with mock.patch.object(blossom_service, "SessionLocal", lambda: self.session), \
             mock.patch.object(blossom_service, "_cfg", lambda db: cfg), \
             mock.patch.object(blossom_service, "delete_blob_bytes", mock.AsyncMock()), \
             mock.patch.object(self.session, "close", lambda: None):
            return blossom_service._cleanup_once()

    def _alive(self):
        return {b.sha256[0] for b in self.session.query(BlossomBlob).all()}

    def test_age_sweep_skips_keep_blobs(self):
        """The retroactive one: a drive uploaded while the TTL was off, swept when it's turned on."""
        _mk(self.session, "d", age_days=400, keep=True)     # a Notes attachment / music track
        _mk(self.session, "c", age_days=400)                # an ordinary chat image
        removed = self._sweep(ttl_days=365)
        self.assertEqual(removed, 1, "the ordinary blob must still be swept")
        self.assertEqual(self._alive(), {"d"})

    def test_an_explicit_expiry_is_honoured_on_a_keep_blob(self):
        """CHANGED DELIBERATELY, and the distinction is the whole point of the two rules.

        The age rule above is a BLANKET policy nobody set per blob, read live, and therefore
        retroactive — exempting `keep` from it is what stops an admin turning a TTL on a year later
        from eating an encrypted drive. An `expires_at` is the opposite: stamped one blob at a time
        by code that proved those exact bytes are referenced by nothing — a files-index blob that
        fell out of backup retention, a folder-sync manifest two generations stale.

        `keep` used to swallow those too, which protected nothing and meant every superseded index
        and manifest leaked for ever while the code that stamped them looked like it was reclaiming.
        Measured on this deployment: 88 keep blobs carrying an expiry that could never fire.

        What makes this safe is the upload path, not this sweep — see
        test_a_keep_upload_clears_an_expiry_stamped_earlier: if those bytes ever become referenced
        again, the reference clears the stamp before it can fire."""
        past = int(time.time()) - 60
        _mk(self.session, "d", keep=True, expires_at=past)      # proven unreferenced, stamped
        _mk(self.session, "c", expires_at=past)                 # an ordinary transient artifact
        removed = self._sweep(ttl_days=0)
        self.assertEqual(removed, 2, "both were explicitly expired and both are due")
        self.assertEqual(self._alive(), set())

    def test_a_keep_blob_with_no_expiry_is_never_touched(self):
        """The ordinary case, and the one that must not move: drive content carries no expiry, so
        neither rule can reach it however old it is."""
        _mk(self.session, "d", age_days=4000, keep=True)
        _mk(self.session, "e", age_days=1, keep=True)
        self.assertEqual(self._sweep(ttl_days=1), 0)
        self.assertEqual(self._alive(), {"d", "e"})

    def test_a_keep_blob_expiring_later_survives_this_sweep(self):
        """A stamp is a grace period, not a delete — the blob stays readable until it is due."""
        future = int(time.time()) + 7 * DAY
        _mk(self.session, "d", keep=True, expires_at=future)
        self.assertEqual(self._sweep(ttl_days=0), 0)
        self.assertEqual(self._alive(), {"d"})

    def test_ttl_off_sweeps_nothing_by_age(self):
        """Guards the default: with no TTL set (the live config on this deployment) age is not a
        deletion reason for anything, keep or not."""
        _mk(self.session, "d", age_days=4000, keep=True)
        _mk(self.session, "c", age_days=4000)
        self.assertEqual(self._sweep(ttl_days=0), 0)
        self.assertEqual(self._alive(), {"d", "c"})


class TestKeepIsOneWay(unittest.TestCase):
    """Blossom dedups by sha256, so one set of bytes can be both a throwaway and drive content.
    `keep` may only ever go False->True — the reference that must survive wins."""

    def test_save_blob_promotes_an_existing_blob_to_keep(self):
        existing = mock.Mock(expires_at=None, keep=False)
        db = mock.Mock()
        db.query.return_value.filter.return_value.first.return_value = existing
        with mock.patch.object(blossom_service, "_cfg", lambda d: {"backend": "local"}), \
             mock.patch.object(blossom_service, "compute_sha256", lambda b: "a" * 64), \
             mock.patch.object(blossom_service, "add_owner", mock.Mock()), \
             mock.patch.object(blossom_service, "_meta_put", mock.Mock()), \
             mock.patch.object(blossom_service, "_meta_from_row", mock.Mock()), \
             mock.patch.object(blossom_service, "_descriptor_fields", mock.Mock()):
            import asyncio
            asyncio.run(blossom_service.save_blob(db, "a" * 64, b"x", "text/plain", keep=True))
        self.assertTrue(existing.keep, "a keep upload must promote the deduped row")
        db.commit.assert_called()

    def test_a_keep_upload_clears_an_expiry_stamped_earlier(self):
        """THE SAFETY NET under honouring an explicit expiry on a keep blob.

        Blossom dedups, so bytes the server once proved unreferenced — and stamped with a TTL — can
        become referenced again by a later upload. If the stamp survived that, the sweep would delete
        something live. It does not: a keep upload passes no TTL of its own, and the save path clears
        any expiry it finds. So an expiry on a keep blob can only ever mean 'still unreferenced'."""
        existing = mock.Mock(expires_at=int(time.time()) + 3 * DAY, keep=True)
        db = mock.Mock()
        db.query.return_value.filter.return_value.first.return_value = existing
        with mock.patch.object(blossom_service, "_cfg", lambda d: {"backend": "local"}), \
             mock.patch.object(blossom_service, "compute_sha256", lambda b: "a" * 64), \
             mock.patch.object(blossom_service, "add_owner", mock.Mock()), \
             mock.patch.object(blossom_service, "_meta_put", mock.Mock()), \
             mock.patch.object(blossom_service, "_meta_from_row", mock.Mock()), \
             mock.patch.object(blossom_service, "_descriptor_fields", mock.Mock()):
            import asyncio
            asyncio.run(blossom_service.save_blob(db, "a" * 64, b"x", "text/plain", keep=True))
        self.assertIsNone(existing.expires_at,
                          "a fresh reference must clear the TTL, or the sweep deletes live content")

    def test_save_blob_never_clears_keep(self):
        existing = mock.Mock(expires_at=None, keep=True)
        db = mock.Mock()
        db.query.return_value.filter.return_value.first.return_value = existing
        with mock.patch.object(blossom_service, "_cfg", lambda d: {"backend": "local"}), \
             mock.patch.object(blossom_service, "compute_sha256", lambda b: "a" * 64), \
             mock.patch.object(blossom_service, "add_owner", mock.Mock()), \
             mock.patch.object(blossom_service, "_meta_put", mock.Mock()), \
             mock.patch.object(blossom_service, "_meta_from_row", mock.Mock()), \
             mock.patch.object(blossom_service, "_descriptor_fields", mock.Mock()):
            import asyncio
            asyncio.run(blossom_service.save_blob(db, "a" * 64, b"x", "text/plain", keep=False))
        self.assertTrue(existing.keep, "an ordinary re-upload must not un-keep drive content")


if __name__ == "__main__":
    unittest.main()
