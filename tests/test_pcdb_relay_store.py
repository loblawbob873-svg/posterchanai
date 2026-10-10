"""PosterChanDB as the relay's ONLY store (POSTERCHANDB_MODE=primary, task #161): nostr_relay/pcdb_store.py.

The existing relay suites (test_relay_prune, test_paid_retention, test_relay_git_state_tie_break,
test_relay_payment_targets) already run against BOTH backends through tests/relay_backends.py. This file holds
what is new with the primary store:

  * an id-prefix collision can no longer make an event unfindable (a bug the both-backend run exposed);
  * the sidecar (relay_kv / wot / bridge tables) survives a SIGKILL at any instant, and refuses a corrupt file;
  * prune, its preview, the content purges, COUNT, negentropy and queries match the Postgres store on
    generated data;
  * kind 30078 -- the app's datastore -- survives every cleaner, on both backends;
  * primary mode never opens a Postgres connection, and an unpromoted directory is refused;
  * scripts/posterchandb_promote.py refuses a token or count mismatch and promotes a matching directory;
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
import uuid

import pytest

from app.services.nostr_relay import pcdb_store as P
from app.services.posterchandb.store import Store
from tests.relay_backends import BACKENDS, pcdb_factory
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


# ------------------------------------------------------------------ parity with the Postgres store
psycopg2 = pytest.importorskip("psycopg2")
from tests import scratch_postgres                          # noqa: E402
from tests.test_posterchandb_vs_relay import OldGen, Gen    # noqa: E402
from app.services.nostr_relay import store as relay        # noqa: E402

DSN = scratch_postgres.dsn()


def _admin():
    try:
        conn = psycopg2.connect(DSN, connect_timeout=5)
    except Exception as e:      # noqa: BLE001
        pytest.skip("Postgres not reachable: %s" % e)
    conn.autocommit = True
    return conn


@pytest.fixture
def both(tmp_path):
    """(RelayStore on a scratch schema, PcdbRelayStore on a fresh primary directory, loop)."""
    schema = "pcai_pcdb_primary_" + uuid.uuid4().hex[:10]
    c = _admin()
    c.cursor().execute(f'CREATE SCHEMA "{schema}"')
    c.close()
    loop = asyncio.new_event_loop()
    rs = relay.RelayStore(DSN + f" options=-csearch_path={schema}")
    rs.open(loop)
    d = str(tmp_path / "primary")
    P.init_new(d)
    ps = P.PcdbRelayStore(d, maintenance=False)
    ps.open(loop)
    try:
        yield rs, ps, loop
    finally:
        ps.close()
        rs.close()
        loop.close()
        c = _admin()
        c.cursor().execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        c.close()


def _feed_both(rs, ps, gen, n, extra_tags=None):
    bad = []
    for _ in range(n):
        ev = gen.event()
        if extra_tags and gen.r.random() < 0.15:
            ev["tags"] = ev["tags"] + [extra_tags(gen)]
            ev["id"] = ev_id(ev)
            gen.made[-1] = ev
        origin = gen.r.choice(["direct", "wot", "wot", "bridge", "ancestor"])
        a = rs._add_event_sync(dict(ev), origin)
        b = ps._add_event_sync(dict(ev), origin)
        if a != b:
            bad.append({"kind": ev["kind"], "pg": a, "pcdb": b, "origin": origin})
    return bad


def _ids_pg(rs):
    return {r["id"] for r in rs._conn().execute("SELECT id FROM events").fetchall()}


def _ids_pcdb(ps):
    st = ps.store
    return {st._id_hex(s) for s in range(len(st.off)) if not st.dead[s] and st.seg[s] != 0xFFFFFFFF}


def _proxy_tag(gen):
    return ["proxy", "https://mostr.pub/objects/%d" % gen.r.randint(0, 9), gen.r.choice(["activitypub", "atproto"])]


def _configure(st, gen, now):
    st.retention_days, st.max_events = 30, 300
    st.set_preserve_pubkeys([gen.authors[0]])
    st.set_paid_tier_enabled(True)
    st.free_retention_days, st.paid_retention_days = 20, 60
    st.set_subscribers([gen.authors[1]], ledger_ok=True)


@pytest.mark.parametrize("seed", [31, 32, 33])
def test_prune_and_purges_match_the_postgres_store_on_generated_data(both, seed):
    """Same history into both stores (stored/refused decisions compared), then every rule at once -- age,
    orphan zaps, bridge DM TTL, retired, expiry, pay-to-stay tiers both ways, the count cap -- previewed, pruned
    in small chunks; then the content purges. After each step the survivors are identical, id for id."""
    rs, ps, loop = both
    gen = OldGen(seed)
    bad = _feed_both(rs, ps, gen, 1100, extra_tags=_proxy_tag)
    assert not bad, "%d stored/refused decisions differ; first %r" % (len(bad), bad[:3])
    assert _ids_pg(rs) == _ids_pcdb(ps), "the stores differ before any cleaning"
    now = int(time.time())
    for st in (rs, ps):
        _configure(st, gen, now)

    pv_pg = loop.run_until_complete(rs.prune_preview())
    pv_me = loop.run_until_complete(ps.prune_preview())
    for k in ("expired", "aged", "orphan_zaps", "bridge_dm", "retired", "retired_by_kind", "aged_free", "aged_paid",
              "capped"):
        assert pv_pg.get(k) == pv_me.get(k), "preview %s: postgres %r, pcdb %r" % (k, pv_pg.get(k), pv_me.get(k))
    assert pv_pg["aged"] + pv_pg["aged_free"] + pv_pg["capped"] > 30, "the history must give the rules real work"

    a = loop.run_until_complete(rs.prune(chunk=37))
    b = loop.run_until_complete(ps.prune(chunk=37))
    assert a == b and _ids_pg(rs) == _ids_pcdb(ps), "prune: postgres removed %d, pcdb %d" % (a, b)

    word = gen.r.choice(["nostr", "hello", "the"])
    for name, args in (("delete_by_words", ([word],)), ("delete_by_langs", ({"en", "de"},)),
                       ("delete_hidden_payload", ()), ("delete_by_proxy", ()),
                       ("delete_pubkeys", ([gen.authors[2], gen.authors[0]],))):
        x = loop.run_until_complete(getattr(rs, name)(*args))
        y = loop.run_until_complete(getattr(ps, name)(*args))
        assert x == y and _ids_pg(rs) == _ids_pcdb(ps), "%s: postgres %d, pcdb %d" % (name, x, y)
    assert loop.run_until_complete(rs.count()) == loop.run_until_complete(ps.count())


@pytest.mark.parametrize("seed", [41, 42])
def test_reads_match_the_postgres_store(both, seed):
    """query (with hard caps), COUNT (with and without the NIP-78 protection), negentropy, has_event,
    filter_existing, the scans the bridge rules use, wot_missing_metadata."""
    rs, ps, loop = both
    gen = Gen(seed)
    assert not _feed_both(rs, ps, gen, 900, extra_tags=_proxy_tag)
    r = loop.run_until_complete
    for _ in range(250):
        f = gen.flt()
        cap = gen.r.choice([0, 1, 7, 5000])
        assert [e["id"] for e in rs._query_sync([dict(f)], cap)] == [e["id"] for e in ps._query_sync([dict(f)], cap)], \
            sorted(f)
        prot = gen.r.random() < 0.5
        f2 = {k: v for k, v in f.items() if k != "limit"}
        assert rs._count_filtered_sync([dict(f2)], prot) == ps._count_filtered_sync([dict(f2)], prot), sorted(f2)
        assert rs._neg_items_sync([dict(f2)], 500000) == ps._neg_items_sync([dict(f2)], 500000), sorted(f2)
    some = [e["id"] for e in gen.made[::7]] + ["f" * 64]
    assert r(rs.filter_existing(some)) == r(ps.filter_existing(some))
    assert [r(rs.has_event(i)) for i in some] == [r(ps.has_event(i)) for i in some]
    assert r(rs.count()) == r(ps.count())
    doms = {"mostr.pub"}
    assert r(rs.bridged_pubkeys(doms)) == r(ps.bridged_pubkeys(doms))
    assert r(rs.social_mirror_pubkeys()) == r(ps.social_mirror_pubkeys())
    assert sorted(r(rs.nip05_domains())) == sorted(r(ps.nip05_domains()))
    for st in (rs, ps):
        r(st.wot_replace(gen.authors[:3], [gen.authors[5]]))
    assert sorted(r(rs.wot_missing_metadata())) == sorted(r(ps.wot_missing_metadata()))
    assert r(rs.wot_members()) == r(ps.wot_members())


# ------------------------------------------------------------------ kind 30078 survives every cleaner
@pytest.mark.parametrize("backend", BACKENDS)
def test_the_datastore_kind_survives_every_cleaner(backend):
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

    if backend == "pcdb":
        with pcdb_factory() as make:
            _run(lambda loop: go(loop, make))
    else:
        schema = "pcai_30078_" + uuid.uuid4().hex[:10]
        c = _admin()
        c.cursor().execute(f'CREATE SCHEMA "{schema}"')
        c.close()
        made = []

        def make(loop, **kw):
            s = relay.RelayStore(DSN + f" options=-csearch_path={schema}", **kw)
            s.open(loop)
            made.append(s)
            return s
        try:
            _run(lambda loop: go(loop, make))
        finally:
            for s in made:
                s.close()
            c = _admin()
            c.cursor().execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            c.close()


# ------------------------------------------------------------------ primary mode never touches Postgres
def _cfg(**kw):
    cfg = {"pcdb_mode": "primary", "pg_dsn": "host=192.0.2.1 dbname=nope", "max_events": 0, "retention_days": 30,
           "pcdb_flush_s": 300, "pcdb_cache_mb": 0}
    cfg.update(kw)
    return cfg


def test_primary_mode_never_opens_a_postgres_connection(tmp_path, monkeypatch):
    """Every public method of the store, through the same constructor the relay uses (thread._open_store), with
    psycopg2.connect made to explode. One missed port of a SQL method would reconnect to a Postgres this node no
    longer trusts -- and `_conn` raises too, so even a path this test does not drive fails loudly."""
    from app.services.nostr_relay import thread
    calls = []

    def _boom(*a, **k):
        calls.append(a)
        raise AssertionError("primary mode opened a Postgres connection")
    monkeypatch.setattr(psycopg2, "connect", _boom)
    monkeypatch.setenv("POSTERCHANDB_DIR", str(tmp_path))
    P.init_new(str(tmp_path / "relay"))

    async def go(loop):
        st = thread._open_store(_cfg(), loop)
        try:
            assert isinstance(st, P.PcdbRelayStore)
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
            with pytest.raises(RuntimeError):
                st._conn()
            assert thread.mirror_stats()["mode"] == "primary"
        finally:
            st.close()
            thread._mirror_ref.pop("primary", None)
    _run(go)
    assert not calls, "psycopg2.connect was called %d time(s)" % len(calls)


def test_an_unpromoted_directory_is_refused_and_postgres_serves(tmp_path, monkeypatch):
    """`primary` on a directory nobody promoted would serve a stale copy and forget the WoT and the pinned
    authors. The relay falls back to Postgres in serve mode instead -- which, here, is the connection attempt."""
    from app.services.nostr_relay import thread
    monkeypatch.setenv("POSTERCHANDB_DIR", str(tmp_path))
    st = Store(str(tmp_path / "relay"), flush_interval=3600)
    st.put(_mk("a" * 64))
    st.close()
    with open(tmp_path / "relay" / "CLEAN", "w") as f:
        f.write("clean:abc\n")
    tried = []

    def _connect(*a, **k):
        tried.append(1)
        raise ConnectionRefusedError("postgres (fallback) reached")
    monkeypatch.setattr(psycopg2, "connect", _connect)
    cfg = _cfg()
    with pytest.raises(ConnectionRefusedError):
        _run(lambda loop: _async(thread._open_store, cfg, loop))
    assert tried and cfg["pcdb_mode"] == "serve"
    assert os.path.exists(tmp_path / "relay" / "CLEAN"), "a refused promotion must change nothing"


async def _async(fn, *a):
    return fn(*a)


def test_a_mirror_run_after_promotion_retires_it(tmp_path):
    """Promoted, then started in serve mode (which rewrites CLEAN with a new token): the sidecar is stale, so
    primary must refuse until a new promotion."""
    path = str(tmp_path / "relay")
    os.makedirs(path)
    with open(os.path.join(path, "CLEAN"), "w") as f:
        f.write("clean:one\n")
    P.Sidecar(path).replace_all({}, {}, {}, ())
    P.write_marker(path, {"state": "promoted", "clean_token": "clean:one"})
    with open(os.path.join(path, "CLEAN"), "w") as f:
        f.write("clean:two\n")
    with pytest.raises(P.PrimaryNotReady):
        P.claim_directory(path)
    with open(os.path.join(path, "CLEAN"), "w") as f:
        f.write("clean:one\n")
    P.claim_directory(path)                          # the promoted state: accepted, CLEAN consumed
    assert not os.path.exists(os.path.join(path, "CLEAN"))
    assert P.read_marker(path)["state"] == "primary"
    P.claim_directory(path)                          # a restart (or a crash) of the primary itself


# ------------------------------------------------------------------ the promote script
@pytest.fixture
def promotable(tmp_path):
    """A scratch Postgres relay with events + every sidecar table, and a mirror directory holding exactly its
    events under a CLEAN token Postgres also holds -- what a clean stop in serve mode leaves."""
    schema = "pcai_promote_" + uuid.uuid4().hex[:10]
    c = _admin()
    c.cursor().execute(f'CREATE SCHEMA "{schema}"')
    c.close()
    dsn = DSN + f" options=-csearch_path={schema}"
    loop = asyncio.new_event_loop()
    rs = relay.RelayStore(dsn)
    rs.open(loop)
    gen = Gen(5)
    for _ in range(300):
        rs._add_event_sync(gen.event(), gen.r.choice(["direct", "wot"]))
    rs._kv_set_sync("pinned_pubkeys", "a" * 64)
    rs._kv_set_sync("sync_offset", "17")
    rs._wot_replace_sync(gen.authors[:4], [gen.authors[4]])
    rs._bridge_nip05_set_sync("alice_host", "b" * 64)
    rs._bridge_puppet_add_sync("b" * 64)
    path = str(tmp_path / "relay")
    st = Store(path, flush_interval=3600)
    for r in rs._conn().execute("SELECT " + relay.EVENT_COLUMNS + ", e.origin FROM events e "
                                "ORDER BY e.created_at, e.id").fetchall():
        st.copy_put(relay.event_from_row(r), origin=r["origin"])
    st.close()
    token = "clean:" + uuid.uuid4().hex
    rs._kv_set_sync(relay.MIRROR_TOKEN_KEY, token)
    with open(os.path.join(path, "CLEAN"), "w") as f:
        f.write(token + "\n")
    try:
        yield rs, dsn, path, loop
    finally:
        rs.close()
        loop.close()
        c = _admin()
        c.cursor().execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
        c.close()


def _promote(*args):
    sys.path.insert(0, os.path.join(ROOT, "scripts"))
    try:
        import posterchandb_promote as m
    finally:
        sys.path.pop(0)
    return m.main(list(args))


def test_promote_refuses_a_token_mismatch(promotable):
    rs, dsn, path, _loop = promotable
    rs._kv_set_sync(relay.MIRROR_TOKEN_KEY, "open:" + uuid.uuid4().hex)     # a relay started since
    assert _promote("--path", path, "--dsn", dsn) == 2
    assert P.read_marker(path) is None and not os.path.exists(os.path.join(path, "sidecar-kv.json"))


def test_promote_refuses_a_count_mismatch(promotable):
    rs, dsn, path, _loop = promotable
    rs._add_event_sync(_mk("e" * 64, content="written after the mirror stopped"), "direct")
    assert _promote("--path", path, "--dsn", dsn) == 2
    assert P.read_marker(path) is None and not os.path.exists(os.path.join(path, "sidecar-kv.json"))


def test_promote_refuses_an_id_mismatch_with_equal_counts(promotable):
    """One event swapped for another in Postgres: the counts still agree, the ids do not."""
    rs, dsn, path, _loop = promotable
    victim = rs._conn().execute("SELECT id FROM events WHERE expiration IS NULL LIMIT 1").fetchone()["id"]
    ev = _mk("e" * 64, content="swapped in")
    c = psycopg2.connect(dsn)
    c.autocommit = True
    cur = c.cursor()
    cur.execute("DELETE FROM events WHERE id=%s", (victim,))
    cur.execute("INSERT INTO events (id,pubkey,created_at,kind,content,tags,sig,origin) VALUES "
                "(%s,%s,%s,1,%s,'[]',%s,'wot')", (ev["id"], ev["pubkey"], ev["created_at"], ev["content"], ev["sig"]))
    c.close()
    assert _promote("--path", path, "--dsn", dsn) == 2
    assert P.read_marker(path) is None and not os.path.exists(os.path.join(path, "sidecar-kv.json"))


def test_promote_refuses_without_a_clean_stop(promotable):
    _rs, dsn, path, _loop = promotable
    os.remove(os.path.join(path, "CLEAN"))
    assert _promote("--path", path, "--dsn", dsn) == 2


def test_promote_then_primary_serves_the_same_relay(promotable, monkeypatch):
    rs, dsn, path, loop = promotable
    assert _promote("--path", path, "--dsn", dsn) == 0
    monkeypatch.setattr(psycopg2, "connect", lambda *a, **k: (_ for _ in ()).throw(AssertionError("postgres")))
    ps = P.PcdbRelayStore(path, maintenance=False)
    ps.open(loop)
    try:
        r = loop.run_until_complete
        assert r(ps.kv_get("pinned_pubkeys")) == "a" * 64 and r(ps.kv_get("sync_offset")) == "17"
        assert r(ps.kv_get(relay.MIRROR_TOKEN_KEY)) is None
        assert r(ps.wot_members()) == rs._wot_members_sync()
        assert r(ps.bridge_nip05_all()) == {"alice_host": "b" * 64} and r(ps.bridge_puppets_all()) == {"b" * 64}
        assert r(ps.count()) == rs._count_sync()
        flt = [{"limit": 5000}]
        assert [e["id"] for e in r(ps.query(flt))] == [e["id"] for e in rs._query_sync(flt, 5000)]
    finally:
        ps.close()
        monkeypatch.undo()                      # the fixture's teardown needs the real psycopg2.connect
    assert not os.path.exists(os.path.join(path, "CLEAN")), "opening as primary must consume CLEAN"


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
