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
  * **`hydrate` is RETIRED** (#161: there is no SQL to rebuild; reminders, pins and API keys are relay
    DocTables) -- pinned below as a no-op that never opens a session.

Every `*_blocking` wrapper also swallows exceptions by design (a failed mirror must not break the
request that triggered it), which is precisely why the layer underneath needs its own tests: the
wrappers cannot report anything.
"""
import asyncio
from datetime import datetime

import pytest

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


# --------------------------------------------------------------------------- hydrate (retired)
def test_hydrate_is_retired_and_touches_no_sql():
    """It rebuilt SQL Reminder / SavedSearch rows from the per-user documents. There is no SQL any more (#161):
    reminders and pins are relay DocTables. It must do nothing -- above all it must not open a session."""
    from app.database import NoSQL
    assert asyncio.run(rs.hydrate(NoSQL())) == 0
