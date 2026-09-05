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


def test_git_issue_comments_survive_every_cleaner(store_factory):
    """NIP-34 dropped kind-1622 replies: issue/patch discussion is NIP-22 kind-1111 comments now —
    what gitworkshop publishes and renders, and what this client publishes since the same change.
    But 1111 is ALSO ordinary community/article chatter, whose age-out is the relay's bound on
    firehose growth — so the shield is the uppercase `K` root-kind tag (1621/1617/1618), never the
    kind. BOTH halves are asserted: drop the guard and the git comments here are deleted; widen it
    to all of kind 1111 and the ordinary comments survive — either way this test fails.

    Same worst case as the calendar test: every cleaner at once, pay-to-stay ON, the author neither
    a subscriber nor preserved."""
    async def go(loop):
        store = store_factory(loop, retention_days=0, max_events=1)
        store.free_retention_days = 1
        store.paid_retention_days = 30
        store.set_subscribers([], ledger_ok=True)
        stranger = "c" * 64
        root = "d" * 64
        git = []
        for i, rk in enumerate((1621, 1617, 1618), start=1):
            ev = _ev(i, kind=1111, age_days=400, pubkey=stranger)
            ev["tags"] = [["E", root], ["K", str(rk)], ["P", "b" * 64],
                          ["e", root], ["k", str(rk)]] + ev["tags"]
            git.append(ev)
        # The same shape once more as a DIRECT write — the app's own publish path — which is what the
        # tiered rules (the only rules that can delete a direct event) would otherwise age out.
        gd = _ev(10, kind=1111, age_days=400, pubkey=stranger)
        gd["tags"] = [["E", root], ["K", "1621"], ["e", root], ["k", "1621"]] + gd["tags"]
        # Ordinary NIP-22 comments (an article thread) — the firehose bulk the cleaners exist for.
        ordinary = []
        for i in range(100, 120):
            ev = _ev(i, kind=1111, age_days=400, pubkey=stranger)
            ev["tags"] = [["E", "e" * 64], ["K", "30023"], ["e", "e" * 64], ["k", "30023"]] + ev["tags"]
            ordinary.append(ev)
        await store.add_events_bulk(git, origin="wot")
        await store.add_event(gd, origin="direct")
        await store.add_events_bulk(ordinary, origin="wot")

        for _ in range(6):
            await store.prune(chunk=5)

        left = {e["id"] for e in await store.query([{"kinds": [1111], "limit": 60}])}
        want = {e["id"] for e in git} | {gd["id"]}
        assert want <= left, (
            "a git issue/patch comment was pruned — these are the collaboration record "
            "(_GIT_KINDS in spirit); the `K` guard in store._PRUNABLE_SQL is what keeps them")
        survivors = [e for e in ordinary if e["id"] in left]
        assert not survivors, (
            f"{len(survivors)} ordinary kind-1111 comments survived every cleaner — the git-comment "
            "guard must stay scoped to K in (1617,1621,1618), or 1111 stops aging out at all")

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


def test_private_document_cursor_advances_inside_one_second(store_factory):
    """A timestamp-only cursor repeats page one forever when a large sync batch shares a second."""
    async def go(loop):
        store = store_factory(loop)
        pk = "f" * 64
        rows = []
        for i in range(7):
            e = _ev(1000 + i, kind=30078, pubkey=pk)
            e["created_at"] = 1234567
            e["id"] = f"{i + 1:064x}"
            e["tags"] = [["d", f"pcai:fs:Tied:{i}"]]
            rows.append(e)
        await store.add_events_bulk(rows, origin="direct")
        first = await store.query([{"authors": [pk], "kinds": [30078],
                                    "#d~": ["pcai:fs:Tied:"], "limit": 3}])
        assert [e["id"] for e in first] == [f"{i:064x}" for i in (7, 6, 5)]
        last = first[-1]
        second = await store.query([{"authors": [pk], "kinds": [30078],
                                     "#d~": ["pcai:fs:Tied:"], "limit": 3,
                                     "_cursor": [last["created_at"], last["id"]]}])
        assert [e["id"] for e in second] == [f"{i:064x}" for i in (4, 3, 2)]

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


# ------------------------------------------------------------------ retired features


def _plant(store, events, origin="direct"):
    """Write rows straight into the events table, past the store's ingest gate.

    Ingest now REFUSES the retired kinds (that is half the retirement), so `add_event` cannot be
    used to set up the pruner's own test — the rows it has to clear are the ones written BEFORE the
    refusal existed, which is exactly what this reproduces. Same columns _insert_one writes.
    """
    import json as _json
    conn = psycopg2.connect(store.dsn, connect_timeout=5)
    conn.autocommit = True
    cur = conn.cursor()
    for ev in events:
        cur.execute("INSERT INTO events (id, pubkey, created_at, kind, content, tags, sig, raw, "
                    "origin, expiration) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,NULL) "
                    "ON CONFLICT (id) DO NOTHING",
                    (ev["id"], ev["pubkey"], ev["created_at"], ev["kind"], ev.get("content", ""),
                     _json.dumps(ev.get("tags") or []), ev.get("sig", ""), _json.dumps(ev), origin))
    conn.close()


def _retired_ev(i, kind, *, age_days=0, pubkey=None, dtag=None):
    """A retired-feature event. Addressable kinds (30402/34236/34550) collapse per (pubkey, kind, d),
    so each gets its own `d` — otherwise ten listings under one author would be ONE stored row and
    "the prune deleted them all" would be a statement about a single event."""
    ev = _ev(i, kind=kind, age_days=age_days, pubkey=pubkey)
    if dtag is not None:
        ev["tags"] = [["d", dtag]] + ev["tags"]
    return ev


def test_the_retired_features_are_deleted_at_any_age_and_previewed_per_kind(store_factory):
    """Shopping (30402/30403), Communities (34550), Divine Shorts (34236) and legacy NIP-28 chat
    (40-44) are gone from the client, so the relay deletes what it still holds of them.

    This rule is unlike every other one in the file: it is kind-only. No age window (a listing
    published this morning is as unreadable as one from 2023), no `origin != 'direct'` preserve
    (sparing direct writes would leave exactly the local users' listings and shorts behind, which is
    the opposite of what was asked), no author test. So the two things worth pinning are that it
    really does ignore all of that — and that the PREVIEW says so first, per kind, because an
    operator cannot consent to "≈N notes would go" when the rule is "every event of this kind".
    """
    async def go(loop):
        # retention_days=365 + max_events=0: the age rule and the count cap CANNOT fire, so
        # everything deleted here was deleted by the retired rule and nothing else.
        store = store_factory(loop, retention_days=365, max_events=0)
        local = "a" * 64
        store.preserve_pubkeys = frozenset({local})    # a preserved local author, deliberately
        retired = []
        i = 1
        for kind in (40, 41, 42, 43, 44, 30403):
            for age in (0, 500):                       # fresh AND ancient
                retired.append(_retired_ev(i, kind, age_days=age, pubkey=f"{i:064x}")); i += 1
        for kind in (30402, 34236, 34550):
            for age in (0, 500):
                retired.append(_retired_ev(i, kind, age_days=age, pubkey=local,
                                           dtag=f"{kind}-{age}")); i += 1
        # origin='direct' AND a preserved author: the combination every other rule spares.
        await store.add_events_bulk(retired, origin="direct")
        # The store REFUSES these kinds on insert (that is the ingest half of the retirement), so
        # plant them the way rows written before the retirement exist: straight into the table.
        stored = await store.count()
        assert stored == 0, ("a retired kind was accepted by the store — the ingest backstop in "
                             "_insert_one is what stops the firehose re-importing what this prunes")
        _plant(store, retired)
        assert await store.count() == len(retired)

        preview = await store.prune_preview()
        assert preview["retired"] == len(retired), "the preview under-reported a destructive rule"
        assert preview["retired_by_kind"] == {40: 2, 41: 2, 42: 2, 43: 2, 44: 2,
                                              30402: 2, 30403: 2, 34236: 2, 34550: 2}, \
            "the preview must break the count down per kind — a total does not name the feature"
        assert preview["total"] >= preview["retired"]

        removed = await store.prune(chunk=4)
        assert removed == len(retired), f"prune removed {removed}, preview promised {len(retired)}"
        assert await store.count() == 0
        assert (await store.prune_preview())["retired"] == 0

        # …and UNBOUNDED (chunk=0, the single-transaction form) removes exactly the same rows. The
        # two forms take different SQL paths — one binds a LIMIT, the other binds nothing at all —
        # and a rule whose params tuple is empty is the one most likely to break on the latter.
        _plant(store, retired)
        assert await store.prune(chunk=0) == len(retired)
        assert await store.count() == 0

    _run(go)


def test_concord_the_datastore_and_ordinary_posts_survive_the_retired_rule(store_factory):
    """The other half, and the one that matters: what the retired rule must NEVER reach.

    "Legacy chats" means NIP-28 (40-44). CONCORD is the chat product the owner uses and its kinds
    sit right beside them (9, 1000, 1002, 1018, 1036, 1040, 1059, 1061, 1063, 1068, 1074, 1075) —
    a range check written one digit wide, or a "chat" grep, takes it with them. Kind 30078 is the
    app's own datastore (settings, Notes, calendars, contacts, the vault, the desktop arrangement);
    the git kinds are a repo's source of truth; kind 22 is NIP-71 short video, which OTHER clients
    publish and this relay still serves — the owner's decision was "i just want to reject the divine
    like short-formed videos", so 34236 goes and 22 stays; and 4550 (NIP-72 post approval) appears
    nowhere in this repo, so it was never part of the feature that was removed.

    Nothing here is aged or expired and the age/cap rules are switched off, so a failure can only be
    the retired rule having grown too wide. Widen `_RETIRED_KINDS` by one entry and this fails.
    """
    async def go(loop):
        store = store_factory(loop, retention_days=365, max_events=0)
        keep = []
        i = 1
        # Concord, by kind and by name — the list is also the record of what is being protected.
        for kind in (9, 1000, 1002, 1018, 1036, 1040, 1059, 1061, 1063, 1068, 1074, 1075):
            keep.append(_ev(i, kind=kind, pubkey=f"{i:064x}")); i += 1
        # The app's own datastore, a git repo announcement + an issue, an ordinary note and a
        # repost, and the NIP-71 video kinds the Shorts screen only ever READ.
        for kind in (30078, 30617, 1621, 1, 6, 21, 22, 34235, 4550):
            ev = _ev(i, kind=kind, pubkey=f"{i:064x}")
            if kind in (30078, 30617, 34235):
                ev["tags"] = [["d", f"keep-{kind}"]] + ev["tags"]
            keep.append(ev); i += 1
        retired = [_retired_ev(900 + n, k, pubkey=f"{900 + n:064x}", dtag="x")
                   for n, k in enumerate((42, 30402, 34236, 34550))]
        await store.add_events_bulk(keep, origin="direct")
        _plant(store, retired)                          # ingest refuses them; the pruner clears them

        before = {e["id"] for e in keep}
        for _ in range(4):
            await store.prune(chunk=5)

        left = {e["id"] for e in await store.query([{"limit": 200}])}
        lost = sorted(before - left)
        assert not lost, (
            f"the retired rule deleted {len(lost)} event(s) it must never touch. Concord is the chat "
            "product in use, 30078 is this app's entire datastore, the git kinds are a repo's source "
            "of truth and NIP-71 video (21/22/34235) is ordinary video other clients publish — only "
            "NIP-28 40-44, 30402/30403, 34550 and 34236 are retired. Narrow _RETIRED_KINDS back.")
        assert not ({e["id"] for e in retired} & left), "the retired rows should still be gone"

    _run(go)


def test_the_store_refuses_a_retired_kind_on_every_origin(store_factory):
    """The ingest backstop, at the funnel every path writes through.

    `_insert_one` is where the WS write path, the live firehose, the windowed WoT sync, ancestor
    backfill and a member restore all end up, so refusing here is what makes the retirement
    structural rather than a list of remembered call sites — and it is what stops the next sync tick
    re-importing exactly the rows the pruner just deleted. The origins are the callers: 'direct' is a
    client publishing here, 'wot' the firehose/sync, 'ancestor' a backfilled thread parent.
    """
    async def go(loop):
        store = store_factory(loop, retention_days=365, max_events=0)
        i = 1
        for origin in ("direct", "wot", "ancestor"):
            for kind in (40, 41, 42, 43, 44, 30402, 30403, 34236, 34550):
                ev = _retired_ev(i, kind, pubkey=f"{i:064x}", dtag="d"); i += 1
                assert await store.add_event(ev, origin=origin) is False, \
                    f"kind {kind} was accepted over origin={origin}"
        assert await store.count() == 0
        # …and the same funnel still accepts what is NOT retired, incl. Concord and NIP-71 video.
        keep = [_ev(i + n, kind=k, pubkey=f"{i + n:064x}")
                for n, k in enumerate((1, 9, 22, 1059, 1068, 4550, 34235))]
        for ev in keep:
            if ev["kind"] >= 30000:
                ev["tags"] = [["d", "k"]] + ev["tags"]
        assert await store.add_events_bulk(keep, origin="wot") == len(keep)

    _run(go)
