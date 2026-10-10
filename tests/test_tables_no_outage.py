"""#161: the relay-only app tables must not take the features that read them down after a restart.

Pinned against the SHIPPED relay (tests/test_doc_table.py's RelayServer over a real PosterChanDB store), with NO
SQL anywhere (the tables are relay-only from the first request):

  * AFTER A RESTART the strict load of a big table (~1 ms a row: ~95 s for 100k blobs) must not make the paths that
    read it 503: the load runs on a thread of its own, a POINT read (an API key, a blob, a reminder by id) reads
    its one document from the relay meanwhile, and only WHOLE-TABLE reads answer 503 + Retry-After -- at once,
    never by waiting on the load inside the request;
  * "could not ask" is never "no rows": a relay that cannot be asked raises Unavailable, never an empty answer.
"""
import asyncio
import hashlib
import itertools
import os
import socket
import threading
import time
from datetime import datetime

import pytest

from app.services import api_key_store, blob_index, doc_table, doc_table_bulk, reminder_service
from app.services.nostr import bech32
from app.services.relay_reader import Unavailable
from tests.test_doc_table import SK, _Relay

KEY = "sk-" + "a" * 64
_ns = itertools.count(1)


def run(coro):
    return asyncio.run(coro)


@pytest.fixture(scope="module")
def shared_relay(tmp_path_factory):
    r = _Relay(str(tmp_path_factory.mktemp("nooutage") / "relay"))
    try:
        yield r
    finally:
        r._h["loop"].call_soon_threadsafe(r._h["stop"].set)
        r._t.join(0.5)


def _cold_process():
    """What a freshly started process holds: no table loaded -- and no loader thread of an earlier "process"
    still running (an orphaned loader stops at its next pass; wait for it)."""
    doc_table.DocTable._registry.clear()
    end = time.monotonic() + 15
    while time.monotonic() < end and any(t.name.startswith("doctable-load-") for t in threading.enumerate()):
        time.sleep(0.1)
    doc_table.reset_state()


@pytest.fixture
def node(shared_relay, monkeypatch):
    """This test's own namespace on the shared relay. No SQL."""
    from app.services import keystore, settings_store
    monkeypatch.setattr(keystore, "get_operator_nsec", lambda: bech32.encode("nsec", SK))
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: {"nostr_relay_retention_days": "30"}.get(k, d))
    monkeypatch.setenv("POSTERCHANAI_RELAY_PORT", str(shared_relay.port))
    monkeypatch.setattr(doc_table, "NS", "pcai:nooutage%d:" % next(_ns))
    _cold_process()
    yield None
    _cold_process()


def _seed_relay(table, rows):
    run(doc_table_bulk.abulk_put(table, rows))


def _key_row(kid=3, key=KEY, active=True):
    return {"id": kid, "user_id": 1, "key": key, "name": "Default", "created_at": "2026-10-01T00:00:00",
            "last_used_at": None, "is_active": active}


def _in_thread(fn, timeout=8.0):
    """Run `fn` on a worker thread (where a sync route runs) and give up after `timeout`: a lookup that waits for
    a big table's load inside the request is the outage being tested, and must fail, not hang the suite."""
    box = {}

    def go():
        try:
            box["v"] = fn()
        except BaseException as e:      # noqa: BLE001
            box["e"] = e
    th = threading.Thread(target=go, daemon=True)
    th.start()
    th.join(timeout)
    if th.is_alive():
        pytest.fail("no answer within %.0fs -- the request waited on the table's load" % timeout)
    if "e" in box:
        raise box["e"]
    return box["v"]


# ============================================================================ a slow load
# ============================================================================ after a restart: point reads answer
class _SlowLoad:
    """Hold the strict listing of one table (what takes ~95 s for 100k rows) until released."""

    def __init__(self, monkeypatch, *tables):
        self.release = threading.Event()
        self.prefixes = tuple(doc_table.DocTable(t).prefix for t in tables)
        real = doc_table.nostr_store.list_all_docs

        async def slow(port, prefix, **kw):
            if prefix.startswith(self.prefixes):
                while not self.release.is_set():
                    await asyncio.sleep(0.05)
            return await real(port, prefix, **kw)
        monkeypatch.setattr(doc_table.nostr_store, "list_all_docs", slow)


def _start_loads(_node=None, names=("api_keys", "api_key_index", "blossom_blobs", "reminders")):
    """What the app does at startup: every table loads on a thread of its own."""
    for n in names:
        doc_table.DocTable(n).start_background_load()


def test_after_a_restart_an_api_key_is_checked_while_its_table_loads(node, monkeypatch):
    _seed_relay("api_keys", {"3": _key_row()})
    _seed_relay("api_key_index", {hashlib.sha256(KEY.encode()).hexdigest(): {"id": 3}})
    _cold_process()
    slow = _SlowLoad(monkeypatch, "api_keys")
    try:
        _start_loads(node)
        k = _in_thread(lambda: api_key_store.lookup(KEY))
        assert k is not None and k.id == 3, "a valid key was refused while its table loaded"
        assert _in_thread(lambda: api_key_store.lookup("sk-" + "b" * 64)) is None
        assert run(api_key_store.alookup(KEY)).id == 3
        # asked again later (idempotent): that must not wait for the running load
        t0 = time.monotonic()
        _in_thread(lambda: _start_loads(node), timeout=5)
        assert time.monotonic() - t0 < 2, "starting the loads again waited on a running load"
    finally:
        slow.release.set()


def test_after_a_restart_a_blob_and_a_reminder_by_id_are_point_reads(node, monkeypatch):
    sha = "cd" * 32
    row = blob_index.new_row(pubkey="c" * 64, size=9, mime="text/plain", created_at=1, expires_at=None,
                             storage="local", path="/y")
    row["owners"]["c" * 64] = [1, None]
    _seed_relay("blossom_blobs", {sha: row})
    _seed_relay("reminders", {"77": reminder_service.reminder_row(1, "by id", datetime(2026, 11, 1))})
    _cold_process()
    slow = _SlowLoad(monkeypatch, "blossom_blobs", "reminders")
    try:
        _start_loads(node)
        me = type("U", (), {"id": 1})()
        got = _in_thread(lambda: run(reminder_service.aget_reminder(None, me, 77)))
        assert got is not None and got.text == "by id"
        assert _in_thread(lambda: run(blob_index.aget(sha))).size == 9
        assert _in_thread(lambda: run(blob_index.aget("ef" * 32))) is None
    finally:
        slow.release.set()


def test_after_a_restart_whole_table_reads_are_503_with_retry_after_at_once(node, monkeypatch):
    from app.routers import blossom as R
    from app.services import blossom_service
    _seed_relay("reminders", {"77": reminder_service.reminder_row(1, "x", datetime(2026, 11, 1))})
    _cold_process()
    slow = _SlowLoad(monkeypatch, "blossom_blobs", "reminders")
    try:
        _start_loads(node)
        me = type("U", (), {"id": 1})()
        t0 = time.monotonic()
        with pytest.raises(Unavailable) as e:
            _in_thread(lambda: run(reminder_service.alist_reminders(None, me)), timeout=5)
        assert time.monotonic() - t0 < 2, "the whole-table read waited on the load"
        assert getattr(e.value, "retry_after", None), "no Retry-After for the client"
        from starlette.requests import Request
        req = Request({"type": "http", "method": "GET", "path": "/list/" + "c" * 64, "query_string": b"",
                       "headers": [], "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 3051), "scheme": "http"})
        monkeypatch.setattr(blossom_service, "is_enabled", lambda db: True)
        got = _in_thread(lambda: run(R.list_blobs("c" * 64, req, db=None)))
        assert got.status_code == 503 and got.headers.get("retry-after")
    finally:
        slow.release.set()


# ============================================================================ "could not ask" is never "no rows"
def test_could_not_ask_is_never_no_rows(node, monkeypatch):
    # with the relay unreachable, a point read and a whole-table read both raise -- never "no such key"
    _cold_process()
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    dead = s.getsockname()[1]
    s.close()
    monkeypatch.setenv("POSTERCHANAI_RELAY_PORT", str(dead))
    with pytest.raises(Unavailable):
        api_key_store.lookup(KEY)
    with pytest.raises(Unavailable):
        run(blob_index.aget("ab" * 32))
    with pytest.raises(Unavailable):
        run(doc_table.DocTable("reminders").aget_remote("1"))
    me = type("U", (), {"id": 1})()
    with pytest.raises(Unavailable):
        run(reminder_service.alist_reminders(None, me))


# ============================================================================ the window that remains
def test_measure_the_whole_table_503_window(node):
    """Whole-table reads still answer 503 + Retry-After until a restart's load finishes: that window is the
    strict load, measured here (PC_MEASURE_ROWS=100000 for the full figure; the default keeps the suite quick)."""
    n = int(os.environ.get("PC_MEASURE_ROWS", "2000"))
    rows = {}
    for i in range(n):
        r = blob_index.new_row(pubkey="%064x" % (i % 300), size=1000 + i, mime="image/jpeg",
                               created_at=1700000000 + i, expires_at=None, storage="proxy",
                               path="blossom/%02x/%064x" % (i % 256, i), keep=bool(i % 2))
        r["owners"]["%064x" % (i % 300)] = [1700000000 + i, None]
        rows["%064x" % (10 ** 9 + i)] = r
    _seed_relay("blossom_blobs", rows)
    _cold_process()
    t = doc_table.DocTable("blossom_blobs")
    t0 = time.monotonic()
    t.start_background_load()
    while not t.loaded and time.monotonic() - t0 < 600:
        time.sleep(0.05)
    window = time.monotonic() - t0
    assert t.loaded
    print("\n[measure] whole-table 503 window after a restart: %d rows loaded in %.2fs (%.2f ms/row; "
          "100k rows ~ %.0fs)" % (n, window, window * 1000 / n, window * 100000 / n))
    assert window / n < 0.01
