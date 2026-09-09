"""A same-second git repo state must settle the way NIP-01 says, on every relay independently.

The store carries a deliberate local exception to NIP-01's lowest-event-id tie-break: for two
addressable events with the SAME `created_at`, a DIRECT write from the author replacing their own
direct write wins, instead of the lower id. It was added for a real failure — one device saving a
document six times a second had half its saves refused "not stored, retry", because ids are random —
and it is right for a document only its author reads.

It is wrong for kinds 30617 and 30618. `git_auth.decide_push_ref` authorises a push by SHA-equality
against the newest maintainer-signed 30618 and reads the maintainer set off the 30617, so both are
an AUTHORISATION input that this relay and the pushing client must resolve identically. Resolving a
tie by arrival order means `pre-receive` deciding against a state the pusher considers superseded —
a push refused, or allowed, for a reason neither side can see. ngit v3 honours the lower-event-ID
tie-break (its CHANGELOG says so) and grinds nonces to avoid same-second collisions, so the window
is small and the cost when it opens is invisible, which is the worst combination to leave to luck.

Runs the REAL store against Postgres: the exception lives in one SQL-driving branch of
`_add_event_sync`, and which row survives is the only observable that matters.
"""
import asyncio
import time
import uuid

import pytest

psycopg2 = pytest.importorskip("psycopg2")

from app.services.nostr_relay.store import RelayStore, _STRICT_TIE_KINDS  # noqa: E402

DSN = "host=127.0.0.1 port=5432 dbname=posterchan_relay user=posterchan"
OWNER = "a" * 64


def _admin():
    try:
        conn = psycopg2.connect(DSN, connect_timeout=5)
    except Exception as e:
        pytest.skip(f"Postgres not reachable for the relay store: {e}")
    conn.autocommit = True
    return conn


@pytest.fixture
def store_factory():
    schema = "pcai_tie_test_" + uuid.uuid4().hex[:10]
    conn = _admin()
    conn.cursor().execute(f'CREATE SCHEMA "{schema}"')
    conn.close()
    dsn = DSN + f" options=-csearch_path={schema}"
    made = []

    def _make(loop):
        st = RelayStore(dsn)
        st.open(loop)
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


def _run(fn):
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(fn(loop))
    finally:
        loop.close()


def _ev(first_hex, kind, when, d="myrepo"):
    """Distinct 64-hex ids whose ORDER is chosen by the caller — the whole point is which one is
    lower."""
    return {"id": first_hex * 64, "pubkey": OWNER, "kind": kind, "created_at": when,
            "content": "", "tags": [["d", d], ["refs/heads/master", "b" * 40]], "sig": "0" * 128}


async def _ids(store, kind):
    return sorted(e["id"] for e in await store.query([{"kinds": [kind], "authors": [OWNER]}]))


@pytest.mark.parametrize("kind", [30617, 30618])
def test_the_lower_id_wins_a_same_second_git_tie_even_between_direct_writes(kind, store_factory):
    """The pre-fix behaviour: the second direct write replaced the first regardless of id, so the
    survivor was whichever arrived last — and two relays holding the same two events disagreed."""
    async def go(loop):
        store = store_factory(loop)
        now = int(time.time())
        # Store the LOWER id first, then offer a HIGHER one. NIP-01 says the incumbent keeps it.
        assert await store.add_event(_ev("1", kind, now), origin="direct")
        await store.add_event(_ev("9", kind, now), origin="direct")
        assert await _ids(store, kind) == ["1" * 64], "the higher id must not displace the lower"

        # And the other order: a LOWER id arriving second must take it.
        assert await store.add_event(_ev("0", kind, now), origin="direct")
        assert await _ids(store, kind) == ["0" * 64]
    _run(go)


@pytest.mark.parametrize("kind", [30617, 30618])
def test_a_genuinely_newer_git_state_still_replaces(kind, store_factory):
    """The tie-break must not become a freeze: a later created_at always wins, whatever the ids."""
    async def go(loop):
        store = store_factory(loop)
        now = int(time.time())
        await store.add_event(_ev("0", kind, now), origin="direct")
        await store.add_event(_ev("f", kind, now + 1), origin="direct")
        assert await _ids(store, kind) == ["f" * 64]
    _run(go)


@pytest.mark.parametrize("kind", [30617, 30618])
def test_two_repos_do_not_collide(kind, store_factory):
    """The tie is per COORDINATE. A stricter rule must not start replacing across `d` tags."""
    async def go(loop):
        store = store_factory(loop)
        now = int(time.time())
        await store.add_event(_ev("1", kind, now, d="one"), origin="direct")
        await store.add_event(_ev("9", kind, now, d="two"), origin="direct")
        assert await _ids(store, kind) == ["1" * 64, "9" * 64]
    _run(go)


def test_an_ordinary_document_keeps_the_rapid_save_exception(store_factory):
    """The guard against fixing this by deleting the exception. A person saving one kind-30078
    document twice in a second must still have the SECOND save stored — half of them were refused
    'not stored, retry' before it existed, measured six-a-second on a real account."""
    async def go(loop):
        store = store_factory(loop)
        now = int(time.time())
        await store.add_event(_ev("9", 30078, now, d="pcai:note:1"), origin="direct")
        assert await store.add_event(_ev("1", 30078, now, d="pcai:note:1"), origin="direct")
        assert await _ids(store, 30078) == ["1" * 64]
        # ...and the higher id, arriving last, still wins for this kind.
        assert await store.add_event(_ev("9", 30078, now, d="pcai:note:1"), origin="direct")
        assert await _ids(store, 30078) == ["9" * 64]
    _run(go)


def test_only_the_git_authorisation_kinds_are_strict():
    """Named rather than open-ended: this exception costs a rapid saver a refused write, so it may
    only cover kinds two independent parties must agree on."""
    assert _STRICT_TIE_KINDS == (30617, 30618)
