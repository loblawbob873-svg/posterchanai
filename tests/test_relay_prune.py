"""Auto-clean (the relay's age/retention prune) — what it deletes, and what it must not.

Two things are pinned here.

FIRST, `prune_preview()` restates `_prune_sync()`'s WHERE clauses in a second place so the Admin
"Preview auto-clean" button can show a count before an admin deletes a few hundred thousand notes.
Two hand-written copies of one predicate is exactly the drift this repo has been bitten by before
(the four copied effect-command literals), and the failure mode here is nastier than a mis-wired
command: a preview that under-reports makes a destructive button look safe. So each test runs the
preview, then the real prune, and demands they agree.

SECOND, the prune is CHUNKED — it deletes in bounded passes so a big first run can't hold the store's
single writer thread for minutes while relay ingestion queues behind it. Chunked and unbounded must
remove exactly the same events; a chunk boundary may not spare or eat one.

Needs Postgres (the store is Postgres-only). The `posterchan` role can't CREATE DATABASE, so each
test isolates itself in a scratch SCHEMA whose name is the ONLY entry in search_path — an unqualified
table can then only resolve inside it, so a mistake errors out instead of touching the live relay.
Skipped when the server isn't reachable.
"""
import asyncio
import time
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from app.services.nostr_relay.store import RelayStore, _PRUNE_CHUNK  # noqa: E402

DSN = "host=127.0.0.1 port=5432 dbname=posterchan_relay user=posterchan"
DAY = 86400


def _admin():
    try:
        conn = psycopg2.connect(DSN, connect_timeout=5)
    except Exception as e:                                    # no server / no role — not a failure
        pytest.skip(f"Postgres not reachable for the relay store: {e}")
    conn.autocommit = True
    return conn


@pytest.fixture
def store_factory():
    """Builds opened RelayStores inside a scratch schema, each with a clean slate; drops it after.

    The per-store truncate matters: a test that builds two stores (chunked vs unbounded) would
    otherwise run the second against the first's survivors, and duplicate event ids would be
    rejected as already-stored — which silently turns an assertion into a tautology.
    """
    schema = "pcai_prune_test_" + uuid.uuid4().hex[:10]
    conn = _admin()
    conn.cursor().execute(f'CREATE SCHEMA "{schema}"')
    conn.close()
    dsn = DSN + f" options=-csearch_path={schema}"
    made = []

    def _truncate():
        c = _admin()
        c.cursor().execute(f"""DO $$ DECLARE r record; BEGIN
            FOR r IN SELECT tablename FROM pg_tables WHERE schemaname='{schema}' LOOP
                EXECUTE 'TRUNCATE TABLE "{schema}".'||quote_ident(r.tablename)||' CASCADE';
            END LOOP; END $$;""")
        c.close()

    def _make(loop, **kw):
        st = RelayStore(dsn, **kw)
        st.open(loop)
        _truncate()
        made.append(st)
        return st

    try:
        yield _make
    finally:
        for st in made:
            st.close()
        try:
            c = _admin()
            c.cursor().execute(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE')
            c.close()
        except Exception:
            pass


def _run(coro_fn):
    """Drive one async test body on its own loop (the store binds to the loop passed to open())."""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro_fn(loop))
    finally:
        loop.close()


def _ev(i, *, kind=1, age_days=0, pubkey=None, expiration=None):
    """A minimally-valid stored event. The prune reads kind/created_at/pubkey/origin/expiration only,
    so ids just have to be distinct 64-hex."""
    ev = {"id": f"{i:064x}", "pubkey": pubkey or ("a" * 64), "kind": kind,
          "created_at": int(time.time()) - age_days * DAY, "content": f"note {i}",
          "tags": [["t", "pcai"]], "sig": "0" * 128}
    if expiration is not None:
        ev["tags"].append(["expiration", str(expiration)])
    return ev


def test_preview_matches_what_prune_actually_deletes(store_factory):
    """The number on the button is the number that goes. Preview counts, prune deletes, compare."""
    async def go(loop):
        store = store_factory(loop, retention_days=60)
        # kind 0 is REPLACEABLE — ten profiles under one pubkey collapse to one, so give each its own
        # author or the "profiles are never pruned" half of this assertion tests a single row.
        await store.add_events_bulk(
            [_ev(i, age_days=90) for i in range(1, 121)]                       # past the window
            + [_ev(i, age_days=5) for i in range(200, 260)]                    # inside it
            + [_ev(i, kind=0, age_days=90, pubkey=f"{i:064x}") for i in range(400, 410)])

        before = await store.count()
        preview = await store.prune_preview()
        removed = await store.prune()
        after = await store.count()

        assert preview["aged"] == 120, f"preview undercounted the aged notes: {preview}"
        assert preview["total"] == removed == 120
        assert before - after == removed
        assert after == 70, "60 in-window notes + 10 profiles must survive"

    _run(go)


def test_chunked_and_unbounded_prunes_remove_the_same_events(store_factory):
    """A chunk boundary must not spare or eat an event. Same corpus, both paths, same survivors."""
    n = 250

    async def go(loop):
        async def run(chunk):
            store = store_factory(loop, retention_days=30)
            await store.add_events_bulk([_ev(i, age_days=90) for i in range(1, n + 1)]
                                        + [_ev(i, age_days=1) for i in range(1000, 1000 + n)])
            removed = await store.prune(chunk=chunk)
            return removed, {e["id"] for e in await store.query([{"limit": 5000}])}

        whole_removed, whole_survivors = await run(0)      # single transaction (old behaviour)
        chunked_removed, chunked_survivors = await run(7)  # many small passes

        assert whole_removed == chunked_removed == n
        assert whole_survivors == chunked_survivors
        assert len(chunked_survivors) == n

    _run(go)


def test_chunking_needs_several_passes_and_still_terminates(store_factory):
    """Guards the loop in prune(): `capped` must clear once the backlog is gone, or it spins."""
    async def go(loop):
        store = store_factory(loop, retention_days=30)
        await store.add_events_bulk([_ev(i, age_days=90) for i in range(1, 51)])

        removed = await asyncio.wait_for(store.prune(chunk=5), timeout=30)   # 10+ passes
        assert removed == 50
        assert await store.count() == 0
        assert await asyncio.wait_for(store.prune(chunk=5), timeout=10) == 0  # no-op, no spin

    _run(go)


def test_preserved_authors_and_direct_writes_survive_a_chunked_prune(store_factory):
    """The preserve set is what stops auto-clean eating a local user's history. Chunking must not
    route around it — the bounding subselect has to keep each rule's predicate intact."""
    mine = "b" * 64

    async def go(loop):
        store = store_factory(loop, retention_days=30)
        await store.add_events_bulk([_ev(i, age_days=365, pubkey=mine) for i in range(1, 41)])
        await store.add_events_bulk([_ev(i, age_days=365) for i in range(500, 540)], origin="direct")
        await store.add_events_bulk([_ev(i, age_days=365) for i in range(900, 940)])
        store.set_preserve_pubkeys([mine])

        preview = await store.prune_preview()
        removed = await store.prune(chunk=3)

        assert preview["aged"] == removed == 40, "only the unprotected author's notes may go"
        assert await store.count() == 80, "preserved author + direct-published writes must survive"

    _run(go)


def test_git_events_survive_a_stray_expiration_tag(store_factory):
    """_GIT_KINDS are a repo's source of truth: an `expiration` tag must not delete one, and the
    expiry sweep runs even with retention off — so this is the rule chunking could most easily break.
    """
    async def go(loop):
        store = store_factory(loop, retention_days=0)
        # An ALREADY-expired event is never STORED (see _insert_one), so seed just-future and let it
        # lapse — seeding it in the past would assert against an empty table.
        soon = int(time.time()) + 1
        await store.add_events_bulk(
            [_ev(i, kind=30617, expiration=soon, pubkey=f"{i:064x}") for i in range(1, 6)]
            + [_ev(i, kind=1617, expiration=soon) for i in range(10, 15)]
            + [_ev(i, kind=1, expiration=soon) for i in range(100, 120)])
        assert await store.count() == 30, "all 30 must be stored while still unexpired"

        await asyncio.sleep(1.6)
        preview = await store.prune_preview()
        removed = await store.prune(chunk=4)

        assert preview["expired"] == removed == 20, "only the non-git expired notes may go"
        assert preview["aged"] == 0, "retention off → the age rule contributes nothing"
        assert await store.count() == 10, "all 10 git events survive their expiration tag"

    _run(go)


def test_datastore_docs_survive_a_stray_expiration_tag(store_factory):
    """Kind 30078 is the app's own datastore — settings, users, chats, and Notes, whose ONLY copy
    is this relay. NIP-37 recommends stamping drafts `expiration: now + 90 days`, so any client
    following that convention would otherwise delete a user's notes 90 days on, silently.

    Both halves are pinned: the tag must not be STORED for these kinds (a stored expiration hides
    the event from every read, since the query builder filters on `expiration > now` — intact on
    disk and invisible is worse than deleted), and the sweep must not delete one.
    """
    async def go(loop):
        store = store_factory(loop, retention_days=0)
        soon = int(time.time()) + 1
        # Addressable (30000-39999) → same pubkey+kind+d collapses to one. Distinct authors keep
        # five distinct rows, matching how the git test seeds its 30617s.
        await store.add_events_bulk(
            [_ev(i, kind=30078, expiration=soon, pubkey=f"{i:064x}") for i in range(1, 6)]
            + [_ev(i, kind=1, expiration=soon) for i in range(100, 110)])
        assert await store.count() == 15, "all 15 must be stored while still unexpired"

        await asyncio.sleep(1.6)
        # Already visible, and still visible after the sweep: the notes never carried an expiration
        # at all. A read here that returned 10 would mean the tag was stored and is hiding them.
        assert len(await store.query([{"kinds": [30078], "limit": 50}])) == 5

        preview = await store.prune_preview()
        removed = await store.prune(chunk=3)

        assert preview["expired"] == removed == 10, "only the kind-1 notes may expire"
        assert await store.count() == 5, "every datastore doc survives its expiration tag"
        assert len(await store.query([{"kinds": [30078], "limit": 50}])) == 5

    _run(go)


def test_the_password_vault_survives_every_cleaner(store_factory):
    """A password is the least reconstructable thing this relay holds.

    The vault is kind 30078 (`d = pcai:pw:<id>`, plus the `pcai:pwkey` event that is the ONLY key
    able to decrypt any of them). It is already safe by construction — every prune rule is an
    allowlist, `_PRUNABLE_KINDS`, and 30078 is not in it — but "safe by construction" is exactly
    what stops being true when somebody adds a kind to that tuple to clean up something else.

    So this asserts it BY NAME, against all four rules at once: age, the NIP-40 expiration sweep,
    the bridge-DM rule and the count cap. If it ever fails, the fix is to take 30078 back out of
    `_PRUNABLE_KINDS`, never to relax this test.
    """
    async def go(loop):
        # retention_days=0 → the age rule prunes everything it is ALLOWED to prune; max_events=1
        # forces the count cap to run too, on a store holding far more than one event.
        store = store_factory(loop, retention_days=0, max_events=1)
        soon = int(time.time()) + 1
        vault = [_ev(i, kind=30078, age_days=400, expiration=soon, pubkey=f"{i:064x}")
                 for i in range(1, 6)]
        noise = [_ev(i, kind=1, age_days=400) for i in range(100, 120)]
        await store.add_events_bulk(vault + noise)

        await asyncio.sleep(1.6)
        await store.prune(chunk=3)
        await store.prune(chunk=3)

        left = await store.query([{"kinds": [30078], "limit": 50}])
        assert len(left) == 5, (
            "the password vault was pruned — every entry and the key that decrypts them are "
            "unrecoverable; 30078 must never be in _PRUNABLE_KINDS")

    _run(go)


def test_default_chunk_is_bounded():
    """A 0/None default would silently restore the single-transaction prune this file exists to
    prevent — the ingestion stall was the whole reason for chunking."""
    assert _PRUNE_CHUNK and _PRUNE_CHUNK > 0
    assert _PRUNE_CHUNK <= 50000, "a pass this large is long enough to stall ingestion"


def test_calendars_and_contacts_survive_every_cleaner(store_factory):
    """EVERY app document — calendars, contacts, notes, the vault, the budget, the desktop
    arrangement, this node's own settings — is kind 30078 written by the app itself, so it lands
    with `origin = 'direct'`.

    That combination is the one worth pinning by name. Everything a person publishes normally is
    protected by the kind allowlist, but PAY-TO-STAY's tiered rules are the only rules in this file
    that can delete a direct write at all — they exist to age out a non-subscriber's own feed posts.
    Their third qualifier, `kind IN (_PRUNABLE_KINDS)`, is the single clause standing between a
    relay with the paid tier switched on and somebody's entire calendar and phone book. It is easy
    to read that rule as "delete this stranger's old direct writes" and not notice that a calendar
    is one.

    Run against every cleaner at once, with pay-to-stay ON and the author NOT a subscriber and NOT
    preserved — the worst case the tiered rules can construct.
    """
    async def go(loop):
        # retention_days=0 → the age rule deletes everything it is allowed to; max_events=1 forces
        # the count cap; free_retention_days=1 with a 400-day-old event puts it far past the free
        # window a non-subscriber gets.
        store = store_factory(loop, retention_days=0, max_events=1)
        store.free_retention_days = 1
        store.paid_retention_days = 30
        store.set_subscribers([], ledger_ok=True)      # nobody has paid; the tiered rules may run
        stranger = "c" * 64
        soon = int(time.time()) + 1
        # DISTINCT `d` tags. Kind 30078 is parameterized-replaceable, so events sharing one
        # (pubkey, kind, d) coordinate collapse to the newest — seven tagless events would prove
        # nothing except that replacement works.
        # EVERY app document, by name. The kind allowlist protects all of kind 30078 at once, so any
        # ONE of these would prove the rule — but the test is also the list of what is being
        # protected, and a name that is not on it is a thing nobody checks for. The desktop
        # arrangement is the worked example: it is the only document here whose loss looks like
        # SUCCESS, because a desktop that cannot read it silently draws the DEFAULT layout, which is
        # indistinguishable from never having arranged one. Nobody files that as data loss.
        dtags = [f"pcai:cal:main:uid-{i}" for i in range(1, 6)] + [
            "pcai:calmeta:main", "pcai:cal:contacts:card-1",
            "pcai:desktop",                    # icon order, folders, hidden icons, WIDGETS
            "pcai:note:abc", "pcai:notefolder:xyz",
            "pcai:budget",
            "pcai:pw:1", "pcai:pwfolder:1", "pcai:pwkey",
            "pcai:playlist:mix",
            "pcai:setting:ssh_hosts",          # the node's own settings live here too
            "pcai:files-index", "pcai:sync:Documents",
            "pcai:kv:uptime", "pcai:kv:paid_retention",
        ]
        cal = []
        for i, d in enumerate(dtags, start=1):          # events, their metadata, and a vCard
            ev = _ev(i, kind=30078, age_days=400, expiration=soon, pubkey=stranger)
            ev["tags"] = [["d", d]] + ev["tags"]
            cal.append(ev)
        noise = [_ev(i, kind=1, age_days=400, pubkey=stranger) for i in range(200, 220)]
        # origin="direct": what the APP's own writes are, and the only origin the tiered
        # rules can delete. Storing these as "wot" would leave the rule this test exists for
        # completely unexercised — it would pass with the guard removed.
        await store.add_events_bulk(cal, origin="direct")
        await store.add_events_bulk(noise, origin="direct")

        await asyncio.sleep(1.6)                       # let the expiration fall due
        for _ in range(4):
            await store.prune(chunk=3)

        left = await store.query([{"kinds": [30078], "limit": 60}])
        kept = {(e["tags"][0][1]) for e in left if e.get("tags")}
        missing = sorted(set(dtags) - kept)
        assert not missing, (
            f"an app document was pruned: {missing}. There is no second copy of any of these — a "
            "calendar, a phone book, a notebook, a password vault, a desktop arrangement. Keep 30078 "
            "out of _PRUNABLE_KINDS and in _NEVER_EXPIRE_KINDS; never relax this test")
        assert len(left) == len(dtags)

    _run(go)


def test_a_webxdc_game_keeps_its_whole_history(store_factory):
    """Kind 4932 is a webxdc mini app's state — every move of every game, as an append-only log.

    Unlike the documents above there is no single event holding the answer: the app's state IS the
    sequence, so losing the oldest ones does not lose "some history", it makes the game unreadable
    from the start (a chess app replays moves from move one). It is also the first kind here that is
    a REGULAR event rather than a 30078 document, so the protection it relies on is different: not
    the parameterized-replaceable path, just absence from _PRUNABLE_KINDS.

    Run against every cleaner at once with pay-to-stay on and the author a non-subscribing stranger,
    which is the case the tiered rules exist for and the one that could delete a direct write.
    """
    async def go(loop):
        store = store_factory(loop, retention_days=0, max_events=1)
        store.free_retention_days = 1
        store.paid_retention_days = 30
        store.set_subscribers([], ledger_ok=True)
        stranger = "d" * 64
        uuid = "9b1deb4d-3b7d-4bad-9bdd-2b0d7b3dcb6d"
        moves = []
        for i in range(1, 13):
            ev = _ev(500 + i, kind=4932, age_days=400, pubkey=stranger)
            ev["tags"] = [["i", uuid], ["alt", "Webxdc update"]] + ev["tags"]
            moves.append(ev)
        noise = [_ev(i, kind=1, age_days=400, pubkey=stranger) for i in range(600, 620)]
        await store.add_events_bulk(moves, origin="direct")
        await store.add_events_bulk(noise, origin="direct")

        for _ in range(4):
            await store.prune(chunk=3)

        left = await store.query([{"kinds": [4932], "limit": 60}])
        assert len(left) == len(moves), (
            f"a webxdc game lost {len(moves) - len(left)} of its {len(moves)} updates. The state of "
            "a mini app is the whole sequence — dropping the oldest does not shorten the history, it "
            "makes the game unreplayable. Keep 4932 out of _PRUNABLE_KINDS.")

    _run(go)


def test_the_tiered_rules_only_ever_touch_feed_kinds(store_factory):
    """The qualifier above, asserted directly rather than through its effect.

    `_tiered_rules` is the only place in the codebase that can delete an `origin='direct'` event, so
    its SQL is worth reading in a test: every rule it produces must be restricted to the prunable
    feed kinds. A rule that ever loses that clause deletes the app's own datastore — settings,
    chats, notes, calendars, contacts — for any author without an account here.
    """
    async def go(loop):
        store = store_factory(loop)
        store.free_retention_days = 1
        store.paid_retention_days = 30
        store.set_subscribers([], ledger_ok=True)
        rules = store._tiered_rules(int(time.time()))
        assert rules, "pay-to-stay is on, so there should be rules to inspect"
        for label, where, _params in rules:
            assert "kind IN (" in where, f"the {label} rule is not restricted to any kind at all"
            assert "30078" not in where, f"the {label} rule names the datastore kind"
            assert "origin = 'direct'" in where, f"the {label} rule is not limited to direct writes"

    _run(go)


def test_the_tiered_rules_stay_off_when_the_ledger_could_not_be_read(store_factory):
    """Fail closed. An unreadable ledger and "nobody subscribed" are the same empty list, and acting
    on the second when it was the first deletes what people paid to keep."""
    async def go(loop):
        store = store_factory(loop)
        store.free_retention_days = 1
        store.set_subscribers([], ledger_ok=False)
        assert store._tiered_rules(int(time.time())) == []

    _run(go)


def test_a_replaceable_write_does_not_walk_the_authors_whole_kind(store_factory):
    """Storing one document must cost ONE lookup, not one per document the author already has.

    Every parameterized-replaceable write used to SELECT all of that author's events of that kind
    and then ask `event_tags` for each row's `d` value, one row at a time. This app keeps its entire
    datastore in kind 30078 — settings, chats, calendars, contacts, mail — so the row set is not a
    handful. Measured on a live node: 2405 documents for one user, meaning a single new message cost
    2405 single-row queries, and the cost grew with every message stored. A mail sync of a few
    hundred was hundreds of thousands of tiny queries and pinned Postgres at ~62% with nothing slow
    in pg_stat_activity, because each one really was sub-millisecond.

    This asserts the BEHAVIOUR the rewrite had to preserve — replacement, isolation between
    different d-tags, the tie-break both ways round, and a tagless document being its own
    coordinate. It does NOT measure the query count: the speed-up is visible in the plan (one
    indexed lookup on the existing idx_event_tags_tv, 0.073 ms against the live table) rather than
    from here. What this guards is that making it fast did not change what it decides.
    """
    async def go(loop):
        store = store_factory(loop)
        pk = "d" * 64

        def ev(i, d, age=0, ident=None):
            e = _ev(i, kind=30078, age_days=age, pubkey=pk)
            if ident:
                e["id"] = ident
            e["tags"] = [["d", d]]
            return e

        # 40 unrelated documents under the same key — the neighbours a write must not walk.
        # A day old, so the replacement below is genuinely NEWER. Created in the same second they
        # would tie, and a tie is settled by the lower id — which is a different rule under test.
        await store.add_events_bulk([ev(i, f"pcai:cal:main:uid-{i}", age=1) for i in range(1, 41)],
                                    origin="direct")
        assert len(await store.query([{"kinds": [30078], "authors": [pk], "limit": 200}])) == 40

        # A NEWER version of one of them replaces exactly that one, and nothing else.
        await store.add_event(ev(500, "pcai:cal:main:uid-7"), origin="direct")
        left = await store.query([{"kinds": [30078], "authors": [pk], "limit": 200}])
        assert len(left) == 40, "replacement must not add or remove neighbours"
        ids = {e["id"] for e in left}
        assert f"{500:064x}" in ids and f"{7:064x}" not in ids, "the newer version must win"

        # An OLDER version of a stored document is refused outright.
        await store.add_event(ev(501, "pcai:cal:main:uid-7", age=5), origin="direct")
        ids = {e["id"] for e in await store.query([{"kinds": [30078], "authors": [pk], "limit": 200}])}
        assert f"{501:064x}" not in ids and f"{500:064x}" in ids, "an older version must not win"

        # TIES, two different rules on purpose. A DIRECT write replacing the author's own DIRECT
        # document wins a same-second tie — ids are random, so the spec's lowest-id rule made a
        # device saving twice in one second lose half its own saves ("not stored, retry", measured
        # six a second on a real account mid-sweep). SYNCED copies still settle by the spec's
        # lowest-id rule, so two relays mirroring each other cannot flip-flop.
        same = (await store.query([{"kinds": [30078], "authors": [pk], "#d": ["pcai:cal:main:uid-7"],
                                    "limit": 5}]))[0]
        hi = "f" * 64
        e_hi = ev(0, "pcai:cal:main:uid-7", ident=hi)
        e_hi["created_at"] = same["created_at"]
        await store.add_event(e_hi, origin="direct")
        ids = {e["id"] for e in await store.query([{"kinds": [30078], "authors": [pk], "limit": 200}])}
        assert hi in ids, "a device's own later save lost a coin flip to its own earlier one"

        # A SYNCED lower id wins the tie (spec), so cross-relay convergence is intact…
        lo = f"{0x100:064x}"
        e_lo = ev(0, "pcai:cal:main:uid-7", ident=lo)
        e_lo["created_at"] = same["created_at"]
        await store.add_event(e_lo, origin="wot")
        ids = {e["id"] for e in await store.query([{"kinds": [30078], "authors": [pk], "limit": 200}])}
        assert lo in ids and hi not in ids, "a synced tie must go to the LOWER id"

        # …and a synced HIGHER id still loses, both ways of the spec rule.
        hi2 = f"{0x200:064x}"
        e_syn = ev(0, "pcai:cal:main:uid-7", ident=hi2)
        e_syn["created_at"] = same["created_at"]
        await store.add_event(e_syn, origin="wot")
        ids = {e["id"] for e in await store.query([{"kinds": [30078], "authors": [pk], "limit": 200}])}
        assert hi2 not in ids and lo in ids, "a synced higher id must lose the tie"

        # A tagless document is its own coordinate and must not collide with the tagged ones.
        bare = _ev(600, kind=30078, pubkey=pk)
        bare["tags"] = []
        await store.add_event(bare, origin="direct")
        assert len(await store.query([{"kinds": [30078], "authors": [pk], "limit": 200}])) == 41

    _run(go)


def test_deleting_a_private_document_is_not_broadcast():
    """A kind-5 removing an app-datastore document must never reach the public upstreams.

    The document itself was never federated — kind 30078 with a `pcai:` d-tag is excluded — but a
    deletion is an ordinary event, and it carries the coordinate it removes IN THE CLEAR:

        ["a", "30078:<pubkey>:pcai:mail:someone@example.com:INBOX:6623"]

    The mail is ciphertext; that tag publishes the account's email address, its folder names and the
    message id to ~20 relays run by other people, permanently, with no way to withdraw it. The same
    shape leaks note ids, calendar uids and contact uids. It is also what pins the outbox: emptying
    a folder is one broadcast per message, times every upstream.
    """
    from app.services.nostr_relay.server import _broadcastable
    pk = "d" * 64
    private = [f"30078:{pk}:pcai:mail:someone@example.com:INBOX:6623",
               f"30078:{pk}:pcai:cal:main:uid-1",
               f"30078:{pk}:pcai:note:abc",
               f"30078:{pk}:pcai:pw:entry-1"]
    for coord in private:
        assert not _broadcastable({"kind": 5, "tags": [["e", "x" * 64], ["a", coord]]}), coord

    # …while an ordinary deletion still federates, or retracting a post would stop working.
    assert _broadcastable({"kind": 5, "tags": [["e", "y" * 64]]})
    assert _broadcastable({"kind": 5, "tags": [["a", f"30023:{pk}:my-article"]]})


def test_a_prefix_tag_filter_reads_one_namespace_not_the_whole_key(store_factory):
    """`#d~` — a LOCAL filter extension, and the app's datastore depends on it.

    A `d` tag is a path (`pcai:mail:<account>:<folder>:<uid>`) but NIP-01 can only match a tag
    exactly, so reading one folder meant asking for every kind-30078 document the author owns and
    filtering client-side. Measured on a live node: opening a mail folder moved 5000 events and
    91.9 MB across the socket to display 35 messages — and hit the limit, so it silently truncated
    too. One user clicking Email could saturate the relay.
    """
    async def go(loop):
        store = store_factory(loop)
        pk = "e" * 64

        def doc(i, d):
            e = _ev(i, kind=30078, pubkey=pk)
            e["tags"] = [["d", d]]
            return e

        await store.add_events_bulk(
            [doc(i, f"pcai:mail:me@example.com:INBOX:{i}") for i in range(1, 6)]
            + [doc(100 + i, f"pcai:mail:me@example.com:Trash:{i}") for i in range(1, 21)]
            + [doc(200 + i, f"pcai:cal:main:uid-{i}") for i in range(1, 11)], origin="direct")

        inbox = await store.query([{"authors": [pk], "kinds": [30078],
                                    "#d~": ["pcai:mail:me@example.com:INBOX:"], "limit": 500}])
        assert len(inbox) == 5, f"the prefix filter returned {len(inbox)} instead of the 5 in INBOX"

        mail = await store.query([{"authors": [pk], "kinds": [30078],
                                   "#d~": ["pcai:mail:"], "limit": 500}])
        assert len(mail) == 25, "a shorter prefix must match everything under it"

        # A `%` or `_` in the prefix is a LITERAL. Unescaped they are LIKE wildcards, and one
        # document's name could then read another's.
        await store.add_events_bulk([doc(900, "pcai:kv:a%b"), doc(901, "pcai:kv:axxb")],
                                    origin="direct")
        got = await store.query([{"authors": [pk], "kinds": [30078],
                                  "#d~": ["pcai:kv:a%b"], "limit": 50}])
        assert len(got) == 1, "`%` in a prefix must not act as a wildcard"

        # …and the ordinary exact filter still behaves.
        one = await store.query([{"authors": [pk], "kinds": [30078],
                                  "#d": ["pcai:cal:main:uid-3"], "limit": 50}])
        assert len(one) == 1

    _run(go)


def test_deleting_a_backup_document_still_federates():
    """The private-deletion guard must not swallow the DR-backup namespaces.

    `_broadcastable` deliberately DOES send settings/accounts/per-user config/bots upstream when
    `backup_datastore` is on (it defaults on), so those documents exist on relays we do not control.
    Their TOMBSTONES therefore have to travel too. Suppressing them leaves the upstream copy
    permanent: delete a bot, rebuild the node, restore from upstream with the operator nsec, and the
    bot is back and posting. Same for a deleted user and an unset setting — the resurrection
    CLAUDE.md documents for settings, extended to accounts.
    """
    from app.services.nostr_relay.server import _broadcastable
    pk = "a" * 64
    cfg = {"backup_datastore": True}
    for ns in ("pcai:setting:foo", "pcai:user:npub1x", "pcai:usercfg:npub1x", "pcai:bot:shobot"):
        ev = {"kind": 5, "tags": [["a", f"30078:{pk}:{ns}"]]}
        assert _broadcastable(ev, cfg), f"{ns} is backed up upstream, so its delete must federate"
    # …and the private libraries still stay home, backup or not.
    for ns in ("pcai:mail:me@example.com:INBOX:1", "pcai:note:abc", "pcai:cal:main:u1",
               "pcai:pw:entry"):
        ev = {"kind": 5, "tags": [["a", f"30078:{pk}:{ns}"]]}
        assert not _broadcastable(ev, cfg), ns
        assert not _broadcastable(ev, {"backup_datastore": False}), ns


def test_an_explicit_empty_d_tag_still_replaces(store_factory):
    """`["d",""]` is the same coordinate as no `d` tag at all, and must replace rather than pile up.

    Ingest indexes empty tag values, so a NOT EXISTS-only lookup could not see an incumbent that
    carried an explicit empty `d` — every revision accumulated under one coordinate, a `#d`
    query returned the whole history instead of one document, and the relay grew without bound on
    firehose traffic from any client that emits one.
    """
    async def go(loop):
        store = store_factory(loop)
        pk = "b" * 64

        def ev(i, tags, age=0):
            e = _ev(i, kind=30078, age_days=age, pubkey=pk)
            e["tags"] = tags
            return e

        # Ages differ deliberately: created in the same second these would TIE, and a tie is
        # settled by the lower id — a different rule, tested elsewhere.
        await store.add_event(ev(1, [["d", ""]], age=2), origin="direct")
        await store.add_event(ev(2, [["d", ""]], age=1), origin="direct")
        left = await store.query([{"authors": [pk], "kinds": [30078], "limit": 50}])
        assert len(left) == 1, f"an explicit empty d must replace, got {len(left)} revisions"
        assert left[0]["id"] == f"{2:064x}"

        # A tagless revision addresses that same coordinate and replaces it too.
        await store.add_event(ev(3, []), origin="direct")   # newest (age 0)
        left = await store.query([{"authors": [pk], "kinds": [30078], "limit": 50}])
        assert len(left) == 1, "no-d and empty-d are one coordinate"
        assert left[0]["id"] == f"{3:064x}"

        # …and a real d-tag beside it is untouched.
        await store.add_event(ev(4, [["d", "pcai:kv:real"]]), origin="direct")
        assert len(await store.query([{"authors": [pk], "kinds": [30078], "limit": 50}])) == 2

    _run(go)
