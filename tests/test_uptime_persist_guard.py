"""AN UNREADABLE RELAY MUST NOT BE ALLOWED TO ERASE THE UPTIME HISTORY.

`uptime_service.py` had ZERO test references, and it is 619 lines whose entire state — every
monitor's heartbeats, the 24h/30d figures, and the up/down flag the alerts are keyed on — lives in
ONE replaceable kind-30078 doc. There is no SQL table to fall back on.

That makes it the same shape as the failure CLAUDE.md records twice already:

    it reads with `nostr_store.get_doc(..., strict=True)` and refuses to persist unless the restore
    succeeded — `_ws_query` otherwise returns `[]` for BOTH "no document" and "relay unreachable",
    and writing on the strength of that empty read replaces the whole history (the same
    replaceable-doc wipe that took out a drive's `pcai:files-index`).

Two separate guards make that safe and NEITHER was being checked:

  1. `_load` sets `_loaded` only AFTER the read comes back — its own comment says marking it
     up-front "would mean a relay that wasn't ready on the first tick permanently lost the history".
  2. `_persist` returns early unless `_loaded`. No read, no write.

Both are one-line guards in the middle of a long module, and both fail silently in the direction
that destroys data: the service keeps checking monitors, the page keeps rendering, and the only
symptom is that the history restarts and every monitor re-alerts as though it had just changed
state. The costly half is unrecoverable — a replaceable event has no previous version to go back to.
"""
import asyncio

import pytest

from app.services import uptime_service as up


class _FakeStore:
    """Stands in for nostr_store. Records writes so 'did not persist' is a positive observation
    rather than the absence of a crash."""

    def __init__(self, doc=None, raises=None):
        self.doc = doc
        self.raises = raises
        self.writes = []
        self.reads = []

    async def get_doc(self, port, d_tag, *, seckey=None, pubkey=None, strict=False, **kw):
        self.reads.append({"d_tag": d_tag, "strict": strict})
        if self.raises:
            raise self.raises
        return self.doc

    async def put_doc(self, port, seckey, d_tag, data, **kw):
        self.writes.append({"d_tag": d_tag, "data": data})
        return True


@pytest.fixture
def store(monkeypatch):
    """Reset the module's process-wide state and wire it to a fake relay + operator key."""
    from app.services import nostr_store, settings_store

    monkeypatch.setattr(up, "_state", {}, raising=False)
    monkeypatch.setattr(up, "_loaded", False, raising=False)
    monkeypatch.setattr(up, "_last_persist", 0.0, raising=False)

    class _Session:
        def close(self):
            pass

    import app.database
    monkeypatch.setattr(app.database, "SessionLocal", lambda: _Session())
    monkeypatch.setattr(settings_store, "_operator_seckey", lambda db: b"\x01" * 32)
    monkeypatch.setattr(settings_store, "get_int", lambda key, default=0: 3052)

    fake = _FakeStore()
    monkeypatch.setattr(nostr_store, "get_doc", fake.get_doc)
    monkeypatch.setattr(nostr_store, "put_doc", fake.put_doc)
    return fake


def _run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- the wipe


def test_an_unreachable_relay_never_becomes_an_empty_write(store):
    """The whole point. A failed read must not be laundered into "there was nothing there"."""
    store.raises = OSError("relay unreachable")
    _run(up._load())
    assert up._loaded is False, \
        "a failed restore marked the state loaded — the next persist would replace the history"

    up._state["api"] = {"name": "api", "checks": [1, 1, 0]}
    _run(up._persist(force=True))
    assert store.writes == [], \
        "the uptime history was overwritten after a read that never succeeded"


def test_a_failed_load_is_retried_rather_than_latched(store):
    """`_load` returns early when `_loaded`, so a permanent latch would be a permanent wipe risk.
    A relay that is merely slow to start must be able to restore on a later tick."""
    store.raises = OSError("relay not up yet")
    _run(up._load())
    assert up._loaded is False

    store.raises = None
    store.doc = {"monitors": {"api": {"name": "api", "checks": [1]}}}
    _run(up._load())
    assert up._loaded is True
    assert up._state["api"]["checks"] == [1], "the retry did not restore the history"


def test_no_operator_key_yet_is_also_not_a_load(store, monkeypatch):
    """A node that has not minted its operator key cannot read the doc, so it has not read it.
    Setting `_loaded` on that path would wipe on the first tick after the key appeared."""
    from app.services import settings_store
    monkeypatch.setattr(settings_store, "_operator_seckey", lambda db: None)
    _run(up._load())
    assert up._loaded is False
    up._state["api"] = {"name": "api"}
    _run(up._persist(force=True))
    assert store.writes == []


def test_the_read_is_strict(store):
    """The load-bearing keyword. Without `strict=True`, `_ws_query` returns [] for BOTH "no
    document" and "relay unreachable", so the guard above can never fire — `_load` would succeed,
    set `_loaded`, and the next persist would write an empty history over a full one."""
    store.doc = {"monitors": {}}
    _run(up._load())
    assert store.reads, "the doc was never read"
    assert store.reads[0]["strict"] is True, \
        "the uptime doc is read non-strictly: an unreachable relay is indistinguishable from an " \
        "empty one, and the empty one gets written back"
    assert store.reads[0]["d_tag"] == up.DOC


# --------------------------------------------------------------------------- it still works


def test_a_genuinely_absent_document_does_load(store):
    """The other direction, and the reason this cannot simply be "never write unless the doc had
    content": the FIRST run on a new node legitimately reads nothing. Refusing to load there would
    mean uptime state was never persisted at all, on every fresh install, for ever."""
    store.doc = None
    _run(up._load())
    assert up._loaded is True, "a fresh node can never persist its first monitor"

    up._state["api"] = {"name": "api", "checks": [1]}
    _run(up._persist(force=True))
    assert len(store.writes) == 1
    assert store.writes[0]["data"]["monitors"]["api"]["checks"] == [1]


def test_a_successful_load_restores_the_history(store):
    store.doc = {"monitors": {"api": {"name": "api", "checks": [1, 0, 1]},
                              "web": {"name": "web", "checks": [1]}}}
    _run(up._load())
    assert set(up._state) == {"api", "web"}
    assert up._state["api"]["checks"] == [1, 0, 1]


def test_a_malformed_document_does_not_replace_live_state_with_junk(store):
    """A doc whose `monitors` is a list, a string or absent must be ignored rather than unpacked —
    and must still count as a read, because the relay answered."""
    for junk in ({"monitors": ["api"]}, {"monitors": "api"}, {}, {"monitors": None}):
        up._loaded = False
        up._state.clear()
        store.doc = junk
        _run(up._load())
        assert up._state == {}, f"{junk!r} was unpacked into the live state"
        assert up._loaded is True, f"{junk!r} was treated as a failed read"


def test_only_dict_records_are_restored(store):
    """One corrupt entry must not take the whole restore with it."""
    store.doc = {"monitors": {"api": {"name": "api"}, "bad": "not-a-record", "worse": 7}}
    _run(up._load())
    assert set(up._state) == {"api"}


# --------------------------------------------------------------------------- write rate


def test_an_unforced_persist_is_rate_limited(store):
    """The doc carries every heartbeat, so writing it per check would republish the whole history
    every 20 seconds. `force` exists for the transitions that must not wait."""
    store.doc = {"monitors": {}}
    _run(up._load())
    up._state["api"] = {"name": "api"}

    _run(up._persist(force=True))
    _run(up._persist())                       # immediately after — inside the window
    assert len(store.writes) == 1, "the rate limit is gone; the doc is rewritten on every tick"

    _run(up._persist(force=True))
    assert len(store.writes) == 2, "force no longer overrides the rate limit"


def test_a_persist_failure_is_survivable(store, monkeypatch):
    """A relay that refuses the write must not take the checker down with it — the next tick
    retries, and the in-memory state is still correct."""
    from app.services import nostr_store
    store.doc = {"monitors": {}}
    _run(up._load())

    async def _boom(*a, **kw):
        raise OSError("relay refused the write")

    monkeypatch.setattr(nostr_store, "put_doc", _boom)
    up._state["api"] = {"name": "api"}
    _run(up._persist(force=True))             # must not raise
    assert up._state["api"]["name"] == "api"
