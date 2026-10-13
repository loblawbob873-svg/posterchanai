"""Relay invariants required by Concord private streams (CORD-01)."""

import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SERVER = (ROOT / "app/services/nostr_relay/server.py").read_text()


def test_concord_wraps_do_not_use_dm_recipient_wot_gate():
    """A Concord p-tag is cover traffic and its stream author is a derived key."""
    branch = re.search(r"elif kind in \(1059, 21059\):(.*?)(?=\n        elif )", SERVER, re.S)
    assert branch, "durable and ephemeral Concord wraps need an explicit admission branch"
    assert "pass" in branch.group(1)
    assert "is_member" not in branch.group(1)
    assert "_dm_for_operator" not in branch.group(1)


def _ev(kind, *, pubkey, tags=None, created_at=None, content="x"):
    import time

    from tests.test_posterchandb import ev_id
    ev = {"pubkey": pubkey, "created_at": created_at or int(time.time()) - 60, "kind": kind,
          "tags": tags or [], "content": content, "sig": "0" * 128}
    ev["id"] = ev_id(ev)
    return ev


def _with_store(fn, **kw):
    """Run `fn(store)` (a coroutine function) on a real, empty PosterChanDB relay store -- the only store."""
    import asyncio

    from tests.relay_backends import pcdb_factory
    loop = asyncio.new_event_loop()
    try:
        with pcdb_factory() as make:
            return loop.run_until_complete(fn(make(loop, **kw)))
    finally:
        loop.close()


STREAM = "5" * 64     # a Concord stream key: every member of the room holds it


def test_concord_stream_wrap_cannot_be_deleted_by_shared_author():
    """Every member knows the stream key; it must not confer relay-side delete power.

    THE PROPERTY, NOT THE SPELLING (this used to grep the Postgres store's DELETE statement, which is
    gone): a NIP-09 kind 5 signed by the wrap's own author cannot reach a gift wrap, while the same
    author's ordinary event IS deleted by the same request -- so the guard is specific, not a broken
    deletion path."""
    async def go(store):
        wrap = _ev(1059, pubkey=STREAM, content="sealed room history")
        note = _ev(1, pubkey=STREAM, content="an ordinary note by the same key")
        assert await store.add_event(wrap) and await store.add_event(note)
        kill = _ev(5, pubkey=STREAM, tags=[["e", wrap["id"]], ["e", note["id"]]])
        await store.add_event(kill)
        return await store.has_event(wrap["id"]), await store.has_event(note["id"])
    wrap_left, note_left = _with_store(go)
    assert wrap_left, "an author-scoped deletion reached a Concord gift wrap"
    assert not note_left, "the same deletion must still remove the author's ordinary event"


def test_concord_cleanup_lifecycle_matches_cord01_and_cord08(monkeypatch):
    """History is durable by default; timers expire it; realtime presence never lands."""
    from app.services.nostr_relay import pcdb_store, store as rules
    assert 1059 not in rules._PRUNABLE_KINDS, \
        "ordinary retention/count cleanup must not silently truncate encrypted room history"
    assert 1059 not in rules._NEVER_EXPIRE_KINDS, \
        "CORD-08 NIP-40 expiration must remain able to purge chat wraps"

    import time
    import types
    now = int(time.time())

    async def go(store):
        old = _ev(1059, pubkey=STREAM, created_at=now - 400 * 86400, content="a year-old room message")
        timed = _ev(1059, pubkey=STREAM, content="a disappearing message", tags=[["expiration", str(now + 600)]])
        assert await store.add_event(old) and await store.add_event(timed)
        await store.prune()
        assert await store.has_event(old["id"]), "retention aged out durable room history"
        assert await store.has_event(timed["id"]), "expired before its time"
        # an hour later: the expiration sweep removes the timed wrap, and only it
        later = types.SimpleNamespace(time=lambda: time.time() + 3600, perf_counter=time.perf_counter,
                                      sleep=time.sleep)
        monkeypatch.setattr(pcdb_store, "time", later)
        removed = await store.prune()
        monkeypatch.setattr(pcdb_store, "time", time)
        return removed, await store.has_event(old["id"]), await store.has_event(timed["id"])
    removed, old_left, timed_left = _with_store(go, retention_days=30, max_events=0)
    assert removed == 1 and old_left and not timed_left, (removed, old_left, timed_left)
    assert "if _is_ephemeral(kind):" in SERVER
    assert "20000 <= kind < 30000" in SERVER


def test_nip11_advertises_cord_family_without_inventing_a_nip_number():
    assert '"concord": {"cords": [1, 2, 3, 4, 5, 6, 7, 8]' in SERVER
    supported = re.search(r'"supported_nips": \[([^]]+)\]', SERVER)
    assert supported and "1059" not in supported.group(1)
