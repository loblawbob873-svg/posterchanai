"""REMINDERS, SAVED SEARCHES AND API KEYS — MIRRORED TO THE RELAY BY 200 UNTESTED LINES.

`record_store.py` had ZERO test references. The relay is the ONLY datastore ("legacy sqlite mode
removed"); the SQL tables are a cache a fresh node rebuilds from `hydrate`. So a mirror that quietly
stops writing is not a degraded feature — it is a row that exists until the next rebuild and then
does not, and an API key that stops existing is a client that stops authenticating.

Two things here are subtle enough to be worth the whole file:

  * **`_run_blocking` is called from BOTH worlds.** Plain sync routes have no event loop; async
    command handlers already have one, where `asyncio.run` raises `RuntimeError: cannot be called
    from a running event loop`. The running-loop path hands the coroutine to a worker thread and
    JOINS it, specifically so the caller's `db` session is never touched by two threads at once.
    Get this wrong and the mirror fails only from the async half of the app — half the call sites
    working perfectly is exactly how it would go unnoticed.
  * **`hydrate` is ADDITIVE and must stay additive.** It recreates MISSING rows. If it ever
    overwrote or deleted, a stale relay doc would silently revert live data — the same replaceable-
    document failure this codebase has hit repeatedly. It also has to skip an API key whose VALUE
    already exists, because `APIKey.key` is UNIQUE and the alternative is an IntegrityError that
    aborts the whole hydrate for every user after it. (#161: API keys left SQL for the operator's
    `api_keys` DocTable, so hydrate no longer creates API-key rows at all -- pinned below.)

Every `*_blocking` wrapper also swallows exceptions by design (a failed mirror must not break the
request that triggered it), which is precisely why the layer underneath needs its own tests: the
wrappers cannot report anything.
"""
import asyncio
from datetime import datetime

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, User, Reminder, SavedSearch, APIKey
from app.services import record_store as rs


# --------------------------------------------------------------------------- _run_blocking


async def _answer():
    return "value"


async def _boom():
    raise RuntimeError("the coroutine failed")


def test_it_runs_a_coroutine_with_no_event_loop():
    """The plain sync route path."""
    assert rs._run_blocking(_answer()) == "value"


def test_it_runs_a_coroutine_from_inside_a_running_loop():
    """The async command-handler path. `asyncio.run` raises RuntimeError here, so without the
    worker-thread branch every mirror from an async handler fails — and only those."""
    async def outer():
        return rs._run_blocking(_answer())

    assert asyncio.run(outer()) == "value"


def test_an_exception_propagates_when_there_is_no_loop():
    with pytest.raises(RuntimeError, match="the coroutine failed"):
        rs._run_blocking(_boom())


def test_an_exception_propagates_out_of_the_worker_thread():
    """The thread stores the exception and the caller re-raises it. Without that re-raise a failed
    mirror returns None and reads as success — and every caller here logs on exception only, so
    silence would mean nothing is ever logged."""
    async def outer():
        return rs._run_blocking(_boom())

    with pytest.raises(RuntimeError, match="the coroutine failed"):
        asyncio.run(outer())


def test_the_calling_loop_is_still_usable_afterwards():
    """The worker thread must not disturb the caller's loop — the request continues after this."""
    async def outer():
        first = rs._run_blocking(_answer())
        await asyncio.sleep(0)
        return first, await _answer()

    assert asyncio.run(outer()) == ("value", "value")


# --------------------------------------------------------------------------- date helpers


def test_a_datetime_round_trips():
    dt = datetime(2026, 8, 31, 13, 37, 5)
    assert rs._dt(rs._iso(dt)) == dt


def test_none_stays_none_in_both_directions():
    assert rs._iso(None) is None
    assert rs._dt(None) is None
    assert rs._dt("") is None


@pytest.mark.parametrize("junk", ["not a date", "2026-13-45", 12345, [], {}])
def test_a_junk_timestamp_is_none_rather_than_an_exception(junk):
    """A record written by an older build must not take the whole hydrate down — one bad field
    would otherwise cost every row after it, for every user after that."""
    assert rs._dt(junk) is None


# --------------------------------------------------------------------------- write guards


class _Rec:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture
def store_calls(monkeypatch):
    """Records relay writes so 'did not write' is a positive observation."""
    calls = []

    async def _put(port, sk, d_tag, data, **kw):
        calls.append(("put", d_tag, data))
        return True

    async def _delete(port, sk, d_tag, **kw):
        calls.append(("delete", d_tag))
        return True

    monkeypatch.setattr(rs.store, "put_doc", _put)
    monkeypatch.setattr(rs.store, "delete_doc", _delete)
    monkeypatch.setattr(rs, "user_storage_seckey", lambda db, user: b"\x01" * 32)
    monkeypatch.setattr(rs._ss, "_port", lambda: 3052)
    return calls


def test_a_reminder_is_mirrored_under_its_namespaced_id(store_calls):
    r = _Rec(id=7, text="call the bank", due_at=datetime(2026, 9, 1),
             status="pending", created_at=datetime(2026, 8, 31), delivered_at=None)
    assert asyncio.run(rs.mirror_reminder(None, _Rec(nostr_npub="npub1x"), r)) is True
    assert store_calls[0][0] == "put"
    assert store_calls[0][1] == "pcai:reminder:7"
    assert store_calls[0][2]["text"] == "call the bank"


def test_no_user_writes_nothing(store_calls):
    assert asyncio.run(rs._put(None, None, rs.NS_SEARCH, 1, {}, force=False)) is False
    assert store_calls == []


def test_a_user_with_no_nostr_key_writes_nothing(store_calls):
    """There is no key to encrypt with, so there is nothing that could be written. Attempting it
    would raise inside the mirror on every save for that user."""
    assert asyncio.run(rs._put(None, _Rec(nostr_npub=None), rs.NS_SEARCH, 1, {},
                               force=False)) is False
    assert asyncio.run(rs._delete(None, _Rec(nostr_npub=""), rs.NS_SEARCH, 1,
                                  force=False)) is False
    assert store_calls == []


def test_a_relay_failure_is_reported_as_false_not_raised(monkeypatch):
    """The request that triggered the mirror must survive an unreachable relay."""
    async def _boom_put(*a, **kw):
        raise OSError("relay unreachable")

    monkeypatch.setattr(rs.store, "put_doc", _boom_put)
    monkeypatch.setattr(rs, "user_storage_seckey", lambda db, user: b"\x01" * 32)
    monkeypatch.setattr(rs._ss, "_port", lambda: 3052)
    assert asyncio.run(rs._put(None, _Rec(nostr_npub="npub1x"), rs.NS_SEARCH, 1, {},
                               force=False)) is False


def test_the_per_user_api_key_mirror_is_retired():
    """#161: the operator-signed `api_keys` DocTable is the copy a fresh node rebuilds from; a second,
    per-user mirror would be a second source of truth for a revocation. Only its delete survives (it
    cleans up a deleted key's old copy). last_used_at's churn rule moved to api_key_store.touch."""
    assert not hasattr(rs, "mirror_apikey_blocking") and not hasattr(rs, "_apikey_rec")
    assert hasattr(rs, "delete_apikey_blocking")


def test_the_namespaces_are_distinct_prefixes():
    """They key the relay documents AND are sliced back off by length in `hydrate`. A shared or
    overlapping prefix would make one record type hydrate as another."""
    ns = [rs.NS_REMINDER, rs.NS_SEARCH, rs.NS_APIKEY]
    assert len(set(ns)) == 3
    for a in ns:
        assert a.endswith(":")
        for b in ns:
            if a is not b:
                assert not a.startswith(b)


# --------------------------------------------------------------------------- hydrate


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[
        User.__table__, Reminder.__table__, SavedSearch.__table__, APIKey.__table__])
    session = sessionmaker(bind=engine)()
    session.add(User(id=1, username="alice", password_hash="x", nostr_npub="npub1alice"))
    session.commit()
    monkeypatch.setattr(rs, "user_storage_seckey", lambda db, user: b"\x01" * 32)
    monkeypatch.setattr(rs._ss, "_port", lambda: 3052)
    # Reminders and saved searches moved to DocTables (#161); until their migration marker exists the SQL
    # rebuild below still runs (those rows are the migration's source). These tests are about that rebuild.
    moved = set()

    async def _moved(table):
        return table in moved
    monkeypatch.setattr(rs, "_moved_to_doctable", _moved)
    session.moved = moved
    yield session
    session.close()


def _docs(monkeypatch, reminders=None, searches=None, keys=None):
    async def _list(port, ns, **kw):
        return {rs.NS_REMINDER: reminders or {}, rs.NS_SEARCH: searches or {},
                rs.NS_APIKEY: keys or {}}.get(ns, {})
    monkeypatch.setattr(rs.store, "list_docs", _list)


def test_a_fresh_node_rebuilds_every_record_type(db, monkeypatch):
    _docs(monkeypatch,
          reminders={"pcai:reminder:5": {"text": "call the bank",
                                         "due_at": "2026-09-01T00:00:00", "status": "pending"}},
          searches={"pcai:search:3": {"query": "nostr"}},
          keys={"pcai:apikey:2": {"key": "k" * 32, "name": "Default", "is_active": True}})
    assert asyncio.run(rs.hydrate(db)) == 2
    assert db.query(Reminder).filter(Reminder.id == 5).first().text == "call the bank"
    assert db.query(SavedSearch).filter(SavedSearch.id == 3).first().query == "nostr"
    # API keys are not rebuilt into SQL any more: they live in the `api_keys` DocTable (#161).
    assert db.query(APIKey).count() == 0


def test_hydrate_never_overwrites_a_row_that_already_exists(db, monkeypatch):
    """ADDITIVE IS THE WHOLE CONTRACT. A stale relay doc silently reverting a live edit is the
    replaceable-document failure this codebase keeps paying for."""
    db.add(Reminder(id=5, user_id=1, text="edited locally",
                    due_at=datetime(2026, 9, 1), status="done"))
    db.commit()
    _docs(monkeypatch, reminders={"pcai:reminder:5": {"text": "stale relay copy",
                                                      "status": "pending"}})
    assert asyncio.run(rs.hydrate(db)) == 0
    row = db.query(Reminder).filter(Reminder.id == 5).first()
    assert row.text == "edited locally" and row.status == "done"


def test_hydrate_never_deletes_a_row_the_relay_does_not_mention(db, monkeypatch):
    db.add(SavedSearch(id=9, user_id=1, query="only local"))
    db.commit()
    _docs(monkeypatch)                                     # relay knows nothing
    assert asyncio.run(rs.hydrate(db)) == 0
    assert db.query(SavedSearch).filter(SavedSearch.id == 9).first().query == "only local"


def test_hydrate_never_writes_an_api_key_row_into_sql(db, monkeypatch):
    """#161: whatever the retired per-user mirror still holds, nothing is written back into the SQL
    `api_keys` table -- a duplicate value can therefore no longer abort the pass either."""
    db.add(APIKey(id=1, user_id=1, key="k" * 32, name="Existing", is_active=True))
    db.commit()
    _docs(monkeypatch, keys={"pcai:apikey:77": {"key": "k" * 32, "name": "Duplicate",
                                                "is_active": True},
                             "pcai:apikey:5": {"name": "no key here"},
                             "pcai:apikey:6": {"key": "z" * 32, "name": "Fresh", "is_active": True}})
    assert asyncio.run(rs.hydrate(db)) == 0
    assert db.query(APIKey).count() == 1


@pytest.mark.parametrize("bad_tag", ["pcai:reminder:notanint", "pcai:reminder:", "pcai:reminder:1.5"])
def test_a_malformed_document_id_is_skipped(db, monkeypatch, bad_tag):
    _docs(monkeypatch, reminders={bad_tag: {"text": "x"}})
    assert asyncio.run(rs.hydrate(db)) == 0


@pytest.mark.parametrize("junk", ["a string", 7, None, []])
def test_a_non_dict_record_is_skipped(db, monkeypatch, junk):
    _docs(monkeypatch, reminders={"pcai:reminder:5": junk},
          searches={"pcai:search:5": junk}, keys={"pcai:apikey:5": junk})
    assert asyncio.run(rs.hydrate(db)) == 0


def test_a_reminder_with_no_due_date_still_hydrates(db, monkeypatch):
    """`due_at` is NOT NULL and the reminder scheduler queries on it, so the fallback to now() is
    what stops one dateless record refusing the whole commit."""
    _docs(monkeypatch, reminders={"pcai:reminder:5": {"text": "someday"}})
    assert asyncio.run(rs.hydrate(db)) == 1
    assert db.query(Reminder).filter(Reminder.id == 5).first().due_at is not None


def test_an_unreadable_user_does_not_stop_the_others(db, monkeypatch):
    """One user whose docs cannot be read (no storage key, an unreachable relay) must not cost
    every other user their rebuild — a fresh node would otherwise come up empty for everybody."""
    db.add(User(id=2, username="bob", password_hash="x", nostr_npub="npub1bob"))
    db.commit()

    async def _list(port, ns, *, seckey=None, **kw):
        if seckey == b"\x02" * 32:
            raise OSError("relay unreachable for this user")
        return {"pcai:search:4": {"query": "alice's search"}} if ns == rs.NS_SEARCH else {}

    monkeypatch.setattr(rs.store, "list_docs", _list)
    monkeypatch.setattr(rs, "user_storage_seckey",
                        lambda db, user: (b"\x02" * 32) if user.id == 1 else (b"\x01" * 32))

    assert asyncio.run(rs.hydrate(db)) == 1
    assert db.query(SavedSearch).filter(SavedSearch.id == 4).first().user_id == 2


def test_a_user_with_no_nostr_key_is_not_hydrated(db, monkeypatch):
    """The query filters on `nostr_npub IS NOT NULL` — there is no key to decrypt their docs."""
    db.add(User(id=3, username="carol", password_hash="x", nostr_npub=None))
    db.commit()
    seen = []

    async def _list(port, ns, **kw):
        seen.append(ns)
        return {}

    monkeypatch.setattr(rs.store, "list_docs", _list)
    asyncio.run(rs.hydrate(db))
    assert len(seen) == 2, "expected exactly one user (2 namespaces), so carol was included"


def test_once_a_table_moved_to_doctable_hydrate_no_longer_writes_its_sql_rows(db, monkeypatch):
    """#161: after the reminders / saved_searches migration markers exist, their store of record is the
    DocTable and the SQL rows are dead; rebuilding them would only feed a table nothing reads. API keys
    are never hydrated into SQL any more (they live in the api_keys DocTable)."""
    db.moved.update({"reminders", "saved_searches"})
    asked = []

    async def _list(port, ns, **kw):
        asked.append(ns)
        return {rs.NS_REMINDER: {"pcai:reminder:5": {"text": "call the bank", "due_at": "2026-09-01T00:00:00"}},
                rs.NS_SEARCH: {"pcai:search:3": {"query": "nostr"}},
                rs.NS_APIKEY: {"pcai:apikey:2": {"key": "k" * 32, "name": "Default"}}}.get(ns, {})
    monkeypatch.setattr(rs.store, "list_docs", _list)
    assert asyncio.run(rs.hydrate(db)) == 0
    assert db.query(Reminder).count() == 0 and db.query(SavedSearch).count() == 0
    assert db.query(APIKey).count() == 0
    assert rs.NS_REMINDER not in asked and rs.NS_SEARCH not in asked and rs.NS_APIKEY not in asked
