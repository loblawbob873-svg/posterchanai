"""The SQL→relay account sweep must not rewrite what the relay already holds.

`_last_synced_hash` lives in memory, so every restart used to make the first sweep consider every
account changed: on this deployment 176 accounts x (record + kv) = ~352 replaceable docs republished
with byte-identical content, each then re-broadcast to every upstream relay. The outbox is paced at
about one event every three seconds, so a deploy buried the outbound queue for ~20 minutes with
writes that changed nothing — and nothing said so until Server Stats grew a queue-depth reading.

The fix asks the relay what it already has before the first sweep. What these tests pin down is the
direction it is allowed to be wrong in: seeding may only ever REMOVE a redundant write. Anything
short of a confirmed byte-for-byte match — a missing doc, a stale one, an unreadable relay — must
leave the account unseeded so the sweep still writes it.
"""
import asyncio
import datetime
import json
import unittest

from app.services import users_store as US


class FakeQuery:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *a, **k):
        return self

    def all(self):
        return list(self._rows)

    def count(self):
        return len(self._rows)


class FakeDb:
    """Enough session for the sweep: users by model, and each user's kv rows."""
    def __init__(self, users, kv_rows):
        self.users, self.kv_rows = users, kv_rows
        self._last_user = None

    def query(self, model):
        from app.models import User, UserSetting
        if model is User:
            return FakeQuery(self.users)
        if model is UserSetting:
            # The sweep queries kv per user inside the loop; every user here has the same rows,
            # which is all these tests need.
            return FakeQuery(self.kv_rows)
        return FakeQuery([])


class Row:
    def __init__(self, key, value):
        self.key, self.value = key, value


def _user(npub, **extra):
    """A stand-in with the columns _record reads, including a DATETIME — the field that makes a
    naive comparison against a stored (JSON) doc fail."""
    u = type("U", (), {})()
    for f in US._SYNCED:
        setattr(u, f, None)
    u.nostr_npub = npub
    u.id = 1
    u.username = "someone"
    u.created_at = datetime.datetime(2026, 1, 2, 3, 4, 5)
    for k, v in extra.items():
        setattr(u, k, v)
    return u


def _stored(user, kv):
    """What the relay would hand back for this user: the record + kv, through JSON."""
    return json.loads(json.dumps([US._record(user), kv], sort_keys=True, default=str))


class SeedFromRelay(unittest.TestCase):
    def setUp(self):
        US._last_synced_hash.clear()
        self.npub = "npub1" + "a" * 58
        self.kv_rows = [Row("timezone", "America/Denver"), Row("storage_nsec", "SECRET")]
        self.kv = {"timezone": "America/Denver"}          # storage_nsec is exempt
        self.user = _user(self.npub)
        self.db = FakeDb([self.user], self.kv_rows)
        self._patch()

    def tearDown(self):
        US._ss._operator_seckey = self._real_key
        US.store.get_docs = self._real_get
        US._last_synced_hash.clear()

    def _patch(self, rec=None, kvd=None, raises=False):
        self._real_key = US._ss._operator_seckey
        self._real_get = US.store.get_docs
        US._ss._operator_seckey = lambda db: b"\x01" * 32
        US._ss._port = lambda db: 3052
        self.asked = []

        async def get_docs(port, d_tags, **kw):
            tags = list(d_tags)
            self.asked.append(tags)
            if raises:
                raise RuntimeError("relay unreachable")
            # Named docs only — a caller that asked for everything would be relying on the 5000-doc
            # client-side filter this deliberately moved away from.
            if tags and tags[0].startswith(US.store.NS_USER):
                return {} if rec is None else {US.store.NS_USER + self.npub: rec}
            return {} if kvd is None else {US.store.NS_USERCFG + self.npub: kvd}

        US.store.get_docs = get_docs

    def _seed(self, **kw):
        self._patch(**kw)
        return asyncio.run(US._seed_hashes(self.db))

    def _count_writes(self, seeded_first: bool):
        """Run one sweep against a relay that already holds this account, counting doc writes.

        `seeded_first=False` is the case that matters and the one an earlier version of this test
        missed: it seeded by hand and then checked the sweep skipped, which passes whether or not
        the sweep ever ASKS the relay. Removing the seed call from reconcile_all broke nothing —
        the cache was tested, the wiring was not.
        """
        rec, kvd = _stored(self.user, self.kv)
        self._patch(rec=rec, kvd=kvd)
        if seeded_first:
            asyncio.run(US._seed_hashes(self.db))
        written = []
        real_u, real_kv = US.sync_user, US.sync_user_kv

        async def spy_u(db, user, **kw):
            written.append("record"); return True

        async def spy_kv(db, user, **kw):
            written.append("kv"); return True

        US.sync_user, US.sync_user_kv = spy_u, spy_kv
        try:
            n = asyncio.run(US.reconcile_all(self.db))
        finally:
            US.sync_user, US.sync_user_kv = real_u, real_kv
        return n, written

    def test_the_first_sweep_after_a_restart_asks_the_relay_before_writing(self):
        """The wiring, with an EMPTY cache — exactly the state a restart leaves behind. reconcile_all
        must consult the relay itself; nothing else will do it for it at startup."""
        self.assertEqual(US._last_synced_hash, {})
        n, written = self._count_writes(seeded_first=False)
        self.assertEqual(written, [], "a restart must not republish content the relay already has")
        self.assertEqual(n, 0)

    def test_an_account_that_matches_the_relay_is_seeded_and_then_skipped(self):
        """The whole point: identical content must not be republished after a restart."""
        n, written = self._count_writes(seeded_first=True)
        self.assertEqual(written, [], "an unchanged account must not be rewritten")
        self.assertEqual(n, 0)

    def test_a_non_json_column_does_not_make_every_account_look_changed(self):
        """No column in _SYNCED is a datetime TODAY, and that is the trap: it is a hand-edited tuple
        of column names, so the day someone adds one, a live datetime stops being equal to the string
        the stored doc came back as. Every account would look changed, the seed would quietly match
        nothing, and the republish-everything-on-restart storm would return with no signal at all.
        The comparison round-trips the local side through JSON precisely so that cannot happen."""
        real = US._record
        US._record = lambda u: dict(real(u), last_seen=datetime.datetime(2026, 1, 2, 3, 4, 5))
        try:
            rec, kvd = _stored(self.user, self.kv)
            self.assertIsInstance(US._record(self.user)["last_seen"], datetime.datetime)
            self.assertIsInstance(rec["last_seen"], str)      # …and a string once stored
            self.assertEqual(self._seed(rec=rec, kvd=kvd), 1)
        finally:
            US._record = real

    def test_a_stale_stored_doc_is_not_seeded(self):
        rec, kvd = _stored(self.user, self.kv)
        kvd = dict(kvd, timezone="Europe/Berlin")          # relay is behind SQL
        self.assertEqual(self._seed(rec=rec, kvd=kvd), 0)
        self.assertEqual(US._last_synced_hash, {})

    def test_a_never_stored_account_is_not_seeded(self):
        rec, _ = _stored(self.user, self.kv)
        self.assertEqual(self._seed(rec=rec, kvd=None), 0)   # record exists, kv doc missing
        self.assertEqual(self._seed(rec=None, kvd=None), 0)

    def test_the_seed_asks_for_the_docs_it_wants_by_name(self):
        """Not "give me everything you have, I'll filter". The operator key is at 4028 docs against a
        5000 cap (2972 of them bookmarks, a set that only grows), so a whole-key read would one day
        return a partial answer with no signal and the accounts it omitted would silently go back to
        being republished on every restart."""
        rec, kvd = _stored(self.user, self.kv)
        self._seed(rec=rec, kvd=kvd)
        self.assertEqual(self.asked, [[US.store.NS_USER + self.npub],
                                      [US.store.NS_USERCFG + self.npub]])

    def test_an_unreadable_relay_seeds_nothing_and_does_not_raise(self):
        """Fail toward writing. A read failure must never be mistaken for "everything matches" —
        that would skip a real change instead of a redundant one."""
        self.assertEqual(self._seed(raises=True), 0)
        self.assertEqual(US._last_synced_hash, {})

    def test_an_exempt_kv_key_is_not_part_of_the_comparison(self):
        """storage_nsec never leaves the keyfile, so it is not in the doc — including it in the local
        side would make every account mismatch forever."""
        rec, kvd = _stored(self.user, self.kv)
        self.assertNotIn("storage_nsec", kvd)
        self.assertEqual(self._seed(rec=rec, kvd=kvd), 1)

    def test_seeding_runs_once_and_never_under_force(self):
        """`force=True` is the migrate/repair path — it must still write everything."""
        rec, kvd = _stored(self.user, self.kv)
        calls = []
        real = US._seed_hashes

        async def spy(db):
            calls.append(1); return await real(db)

        US._seed_hashes = spy
        written = []
        real_u, real_kv = US.sync_user, US.sync_user_kv

        async def spy_u(db, user, **kw):
            written.append(1); return True

        US.sync_user, US.sync_user_kv = spy_u, spy_u
        try:
            self._patch(rec=rec, kvd=kvd)
            asyncio.run(US.reconcile_all(self.db, force=True))
            self.assertEqual(calls, [], "force must not consult the relay")
            self.assertTrue(written, "force must rewrite every account")
        finally:
            US._seed_hashes = real
            US.sync_user, US.sync_user_kv = real_u, real_kv


if __name__ == "__main__":
    unittest.main()
