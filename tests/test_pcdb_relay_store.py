"""PosterChanDB, the relay's store (nostr_relay/pcdb_store.py, #161 -- the Postgres store it replaced is deleted).

The relay suites (test_relay_prune, test_paid_retention, test_relay_git_state_tie_break, test_relay_payment_targets)
run against it through tests/relay_backends.py. This file holds what is particular to the store:

  * an id-prefix collision can no longer make an event unfindable;
  * the sidecar (relay_kv / wot / bridge tables) survives a SIGKILL at any instant, and refuses a corrupt file;
  * kind 30078 -- the app's datastore -- survives every cleaner;
  * the relay opens it the way production does, every public method works, and a directory that is not a
    relay store (events but no marker, or a marker in another state) is refused rather than adopted;
  * no operation holds the store lock for more than a few milliseconds (measured, printed).
"""
from __future__ import annotations

import asyncio
import os
import random
import signal
import subprocess
import sys
import textwrap
import threading
import time

import pytest

from app.services.nostr_relay import pcdb_store as P
from app.services.posterchandb.store import Store
from tests.relay_backends import pcdb_factory
from tests.test_posterchandb import ev_id

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable
DAY = 86400


def _run(fn):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(fn(loop))
    finally:
        loop.close()


def _mk(pk, kind=1, age=0, tags=None, content="hello", created=None):
    e = {"pubkey": pk, "created_at": created if created is not None else int(time.time()) - age,
         "kind": kind, "tags": tags or [], "content": content, "sig": "0" * 128}
    e["id"] = ev_id(e)
    return e


# ------------------------------------------------------------------ an id prefix shared by two events
def test_two_events_sharing_an_id_prefix_are_both_found(tmp_path):
    """The id map is keyed by the first 8 bytes of the id. Two events with the same prefix in one flush window
    used to OVERWRITE each other in the delta dict, and the merge then dropped the first for good: it could
    never be found by id again -- not by a query, not by has_event, not by a NIP-09 deletion, and a re-send was
    stored as new. Grinding a colliding prefix is 2^32 work, so this is reachable on purpose. Found by running
    the Postgres store's suite (synthetic ids share prefixes) against the primary store."""
    st = Store(str(tmp_path / "s"), flush_interval=3600, direct_durable=False)
    a = {"id": "00" * 8 + "aa" * 24, "pubkey": "1" * 64, "created_at": int(time.time()) - 5, "kind": 1, "tags": [],
         "content": "a", "sig": "0" * 128}
    b = dict(a, id="00" * 8 + "bb" * 24, content="b")
    assert st.put(a) == "stored" and st.put(b) == "stored"
    for when in ("before the merge", "after the merge"):
        assert st.seq_of(a["id"]) is not None and st.seq_of(b["id"]) is not None, when
        assert [e["id"] for e in st.query({"ids": [a["id"]]})] == [a["id"]], when
        assert st.put(dict(a)) == "duplicate", "a re-send of the first was stored again (%s)" % when
        st.flush()
    st.close()
    st2 = Store(str(tmp_path / "s"))
    try:
        assert st2.seq_of(a["id"]) is not None, "lost after a restart"
    finally:
        st2.close()


# ------------------------------------------------------------------ the sidecar
_WRITER = textwrap.dedent("""
    import sys
    sys.path.insert(0, %(root)r)
    from app.services.nostr_relay.pcdb_store import Sidecar
    sc = Sidecar(%(path)r)
    i = int(sc.kv.get("n") or 0)
    while True:
        i += 1
        sc.kv["n"] = str(i)
        sc.kv["blob"] = ("%%d:" %% i) * (1 + (i * 7919) %% 20000)    # sizes vary: some writes take many blocks
        sc.wot = {("%%064x" %% j): [1, i] for j in range(i %% 300)}
        sc.save("kv")
        sc.save("wot")
        print(i, flush=True)
""")


def test_the_sidecar_survives_sigkill_at_any_instant(tmp_path):
    """SIGKILL a writer mid-loop, many times: every reopen reads a COMPLETE state, and never one older than the
    last write that returned (an acknowledged kv_set is durable). kv and wot are separate files, so each is
    checked on its own: a kill between the two saves leaves wot one write behind kv, which is fine."""
    path = str(tmp_path / "sc")
    os.makedirs(path)
    rng = random.Random(7)
    for round_ in range(12):
        p = subprocess.Popen([PY, "-c", _WRITER % {"root": ROOT, "path": path}], stdout=subprocess.PIPE, text=True)
        acked = 0
        deadline = time.monotonic() + rng.uniform(0.05, 0.6)
        while time.monotonic() < deadline:
            line = p.stdout.readline()
            if not line:
                break
            acked = int(line)
        os.kill(p.pid, signal.SIGKILL)
        p.wait()
        for line in p.stdout.read().split():       # everything it acknowledged before it died
            acked = int(line)
        sc = P.Sidecar(path)                       # must not raise: no torn file is ever in place
        n = int(sc.kv["n"])
        assert acked <= n <= acked + 1, "round %d: acked %d, found %d" % (round_, acked, n)
        assert sc.kv["blob"] == ("%d:" % n) * (1 + (n * 7919) % 20000), "kv is torn"
        w = {v[1] for v in sc.wot.values()}
        assert len(w) <= 1 and (not w or w.pop() in (n - 1, n)), "wot is torn"
        assert not [f for f in os.listdir(path) if ".tmp." in f], "a temp file was left for the next open"


def test_a_corrupt_sidecar_is_refused_never_read_as_empty(tmp_path):
    """An unreadable WoT or kv read as EMPTY would rebuild the trust set and forget the pinned (never-pruned)
    authors -- the next prune deletes their history. Refuse instead."""
    path = str(tmp_path / "sc")
    sc = P.Sidecar(path)
    sc.kv["pinned_pubkeys"] = "a" * 64
    sc.save("kv")
    with open(sc.file("kv"), "r+b") as f:
        f.truncate(10)
    with pytest.raises(ValueError):
        P.Sidecar(path)


# ------------------------------------------------------------------ kind 30078 survives every cleaner
def test_the_datastore_kind_survives_every_cleaner():
    """Kind 30078 is settings, Notes, calendars, contacts, the vault, the desktop -- somebody's only copy. Every
    cleaner at once, at its harshest: a 1-day retention, a count cap of 1, pay-to-stay on with a 1-day free
    window and the author neither subscribed nor preserved, a stray NIP-40 expiration, a blocked word, a
    blocked language, a hidden payload, a proxy tag. Not one document may go (an admin deleting the AUTHOR is
    the only thing that removes them, and that is not a cleaner)."""
    async def go(loop, make):
        st = make(loop, retention_days=1, max_events=1)
        st.free_retention_days, st.paid_retention_days = 1, 2
        st.set_subscribers([], ledger_ok=True)
        stranger = "c" * 64
        docs = []
        for i, origin in enumerate(("direct", "wot", "ancestor", "bridge") * 3):
            tags = [["d", "pcai:note:%d" % i], ["expiration", str(int(time.time()) + 2)],
                    ["proxy", "https://mostr.pub/x", "activitypub"]]
            ev = _mk(stranger, kind=30078, age=400 * DAY + i, tags=tags,
                     content="​" * 50 + " spamword das ist ein deutscher Satz mit vielen Woertern " + "QUJD" * 20)
            assert await st.add_event(ev, origin=origin), "the document was not stored at all"
            docs.append(ev["id"])
        feed = [_mk(stranger, kind=1, age=400 * DAY + i, content="spamword %d" % i) for i in range(20)]
        await st.add_events_bulk(feed, origin="wot")
        time.sleep(3)                                     # the stray expiration tag is now in the past
        await st.prune(chunk=3)
        await st.delete_by_words(["spamword"])
        await st.delete_by_langs({"de", "en"})
        await st.delete_hidden_payload()
        await st.delete_by_proxy()
        left = {e["id"] for e in await st.query([{"kinds": [30078], "authors": [stranger], "limit": 500}])}
        assert left == set(docs), "%d of %d datastore documents were deleted by a cleaner" % (
            len(set(docs) - left), len(docs))
        assert not await st.query([{"kinds": [1], "authors": [stranger]}]), "the cleaners did nothing at all"

    with pcdb_factory() as make:
        _run(lambda loop: go(loop, make))


# ------------------------------------------------------------------ the relay opens it
def _cfg(**kw):
    cfg = {"max_events": 0, "retention_days": 30, "pcdb_flush_s": 300, "pcdb_cache_mb": 0}
    cfg.update(kw)
    return cfg


def test_the_relay_opens_the_store_and_every_public_method_works(tmp_path, monkeypatch):
    """Every public method of the store, through the same constructor the relay uses (thread._open_store) --
    on an EMPTY directory, which is a fresh install's first start: it becomes a new store."""
    from app.services.nostr_relay import thread
    monkeypatch.setenv("POSTERCHANDB_DIR", str(tmp_path))

    async def go(loop):
        st = thread._open_store(_cfg(), loop)
        try:
            assert isinstance(st, P.PcdbRelayStore)
            assert P.read_marker(str(tmp_path / "relay"))["state"] == "primary"
            pk = "a" * 64
            ev = _mk(pk, content="hello there", tags=[["t", "x"]])
            assert await st.add_event(ev, origin="direct")
            assert await st.add_events_bulk([_mk(pk, age=i + 1) for i in range(5)]) == 5
            assert await st.has_event(ev["id"]) and await st.filter_existing([ev["id"]]) == {ev["id"]}
            assert (await st.query([{"ids": [ev["id"]]}]))[0]["id"] == ev["id"]
            await st.query([{"search": "hello"}])
            assert await st.count() == 6 and await st.count_filtered([{"kinds": [1]}], True) == 6
            assert len(await st.neg_items([{"kinds": [1]}])) == 6
            await st.kv_set("k", "v")
            assert await st.kv_get("k") == "v"
            await st.bridge_nip05_set("n", pk)
            assert await st.bridge_nip05_all() == {"n": pk}
            await st.bridge_puppet_add(pk)
            assert await st.bridge_puppets_all() == {pk}
            assert await st.wot_replace([pk], ["b" * 64]) == 2
            assert await st.wot_add(["c" * 64]) == 1
            assert len(await st.wot_members()) == 3
            await st.wot_missing_metadata()
            assert not await st.is_repo_announced("d" * 64, "repo")
            await st.bridged_pubkeys({"mostr.pub"})
            await st.social_mirror_pubkeys()
            await st.bridge_identity_pubkeys({"mostr.pub"})
            await st.nip05_domains()
            await st.delete_by_words(["zzz"])
            await st.delete_by_langs({"de"})
            await st.delete_hidden_payload()
            await st.delete_by_proxy()
            await st.prune_preview()
            await st.prune()
            assert await st.delete_pubkeys([pk], spare_preserved=False) == 6
            await st.checkpoint()
            assert thread.mirror_stats()["mode"] == "primary"
        finally:
            st.close()
            thread._store_ref.pop("primary", None)
    _run(go)


async def _async(fn, *a):
    return fn(*a)


def test_a_directory_holding_events_with_no_marker_is_refused_never_adopted(tmp_path, monkeypatch):
    """Events and no PRIMARY marker: nothing records whose data that is. Adopting it would make it the truth, so
    the relay refuses to start -- loudly -- and changes nothing."""
    from app.services.nostr_relay import thread
    monkeypatch.setenv("POSTERCHANDB_DIR", str(tmp_path))
    st = Store(str(tmp_path / "relay"), flush_interval=3600)
    st.put(_mk("a" * 64))
    st.close()
    with pytest.raises(P.PrimaryNotReady):
        _run(lambda loop: _async(thread._open_store, _cfg(), loop))
    assert P.read_marker(str(tmp_path / "relay")) is None, "a refused start must change nothing"


def test_a_marker_in_any_other_state_is_refused(tmp_path):
    path = str(tmp_path / "relay")
    os.makedirs(path)
    P.Sidecar(path).replace_all({}, {}, {}, ())
    P.write_marker(path, {"state": "promoted"})
    with pytest.raises(P.PrimaryNotReady):
        P.claim_directory(path)
    P.write_marker(path, {"state": "primary"})
    P.claim_directory(path)
    P.claim_directory(path)                          # a restart (or a crash) of the store itself


# ------------------------------------------------------------------ lock holds
class _TimedRLock:
    """An RLock that records the longest OUTERMOST hold."""

    def __init__(self):
        self._l = threading.RLock()
        self._depth = 0
        self._t0 = 0.0
        self.max_ms = 0.0
        self.where = None

    def acquire(self, *a, **k):
        ok = self._l.acquire(*a, **k)
        if ok:
            self._depth += 1
            if self._depth == 1:
                self._t0 = time.perf_counter()
        return ok

    def release(self):
        self._depth -= 1
        if self._depth == 0:
            ms = (time.perf_counter() - self._t0) * 1000
            if ms > self.max_ms:
                self.max_ms = ms
                self.where = _phase[0]
        self._l.release()

    __enter__ = acquire

    def __exit__(self, *a):
        self.release()


_phase = ["setup"]


def test_no_cleaner_or_scan_holds_the_store_lock_for_long(tmp_path):
    """The relay's readers share ONE store lock; a 465 ms hold starved the live relay (docs/POSTERCHANDB.md).
    200k events, then every prune rule, the preview, a content purge, COUNT/negentropy with nothing selective,
    wot_missing_metadata and an admin purge of a big author -- the longest single hold is measured and must stay
    small. (The bound is generous for a loaded CI box; the measured value is printed.)"""
    d = str(tmp_path / "p")
    P.init_new(d)
    loop = asyncio.new_event_loop()
    st = P.PcdbRelayStore(d, maintenance=False, retention_days=30, max_events=150000)
    st.open(loop)
    try:
        now = int(time.time())
        rng = random.Random(3)
        authors = ["%064x" % rng.getrandbits(256) for _ in range(400)]
        big = authors[0]
        for i in range(200000):
            pk = big if i % 10 == 0 else rng.choice(authors)
            ev = {"pubkey": pk, "created_at": now - rng.randint(0, 90 * DAY), "kind": rng.choice([1, 1, 6, 7, 9735, 0]),
                  "tags": [["e", "%064x" % rng.getrandbits(256)]], "content": rng.choice(["hi", "spam word", "x"]),
                  "sig": "0" * 128, "id": "%064x" % rng.getrandbits(256)}
            st.store.copy_put(ev, origin=rng.choice(["wot", "wot", "direct"]))
        st.store.flush()
        st.store.index_pending_words()
        lock = _TimedRLock()
        st.store._lock = lock
        st.set_paid_tier_enabled(True)
        st.free_retention_days = 20
        st.set_subscribers([authors[3]], ledger_ok=True)
        r = loop.run_until_complete
        results = {}
        for name, call in (("prune_preview", lambda: st.prune_preview()),
                           ("prune", lambda: st.prune()),
                           ("count_filtered", lambda: st.count_filtered([{"kinds": [1, 6]}])),
                           ("neg_items", lambda: st.neg_items([{"kinds": [1], "since": now - 40 * DAY}])),
                           ("wot_missing_metadata", lambda: st.wot_missing_metadata()),
                           ("delete_by_words", lambda: st.delete_by_words(["spam"])),
                           ("delete_pubkeys", lambda: st.delete_pubkeys([big])),
                           ("count", lambda: st.count())):
            _phase[0] = name
            before = lock.max_ms
            t0 = time.perf_counter()
            results[name] = r(call())
            print("%-22s %8.0f ms total, longest lock hold so far %.2f ms" % (
                name, (time.perf_counter() - t0) * 1000, max(before, lock.max_ms)))
        print("LONGEST LOCK HOLD: %.2f ms (%s)" % (lock.max_ms, lock.where))
        assert results["prune"] > 10000 and results["delete_pubkeys"] > 1000, results
        assert lock.max_ms < 50, "a %s held the store lock %.1f ms" % (lock.where, lock.max_ms)
    finally:
        st.close()
        loop.close()


def test_the_relay_process_opens_its_store_before_its_settings_and_reads_them_from_it(tmp_path, monkeypatch):
    """The relay opens its store BEFORE it can read a setting (it answers its own settings reads,
    open_store_for_settings): that early open is the store, answering with no socket."""
    from app.services.nostr_relay import thread
    from app.services import settings_store
    monkeypatch.setenv("POSTERCHANDB_DIR", str(tmp_path))
    P.init_new(str(tmp_path / "relay"))
    seen = {}
    monkeypatch.setattr(settings_store, "set_event_source", lambda fn: seen.setdefault("src", fn))

    async def go(loop):
        st = thread.open_store_for_settings(loop)
        try:
            assert isinstance(st, P.PcdbRelayStore), type(st)
            assert seen["src"]([{"kinds": [30078], "limit": 5}]) == []      # answered by the store, no socket
        finally:
            st.close()
            thread._store_ref.pop("primary", None)
    _run(go)
