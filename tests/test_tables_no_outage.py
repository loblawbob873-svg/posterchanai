"""#161 wave 1: moving the app tables off Postgres must not take the features that read them down.

Two outages shipped in the four branches this wave combines, and both are pinned here against the SHIPPED relay
(tests/test_doc_table.py's RelayServer over a real PosterChanDB store) and a real SQLAlchemy session:

  1. BEFORE A TABLE'S `_migrated` MARKER EXISTS (the first deploy, ~5.5 min per 100k blob rows) every converted
     path answered 503: every `sk-` key was refused, every reminder list, every Blossom upload. Now SQL stays the
     store of record until the marker: reads come from SQL, writes go to SQL first and then the relay, and the
     copy verifies against a FRESH SQL read -- so a write that lands while the copy runs is not lost.
  2. AFTER A RESTART the strict load of a big table (~1 ms a row: ~95 s for 100k blobs) made the same paths 503
     again, every restart. Now the load runs on a background thread started at startup, a POINT read (an API
     key, a blob, a reminder by id) reads its one document from the relay meanwhile, and only WHOLE-TABLE reads
     answer 503 + Retry-After -- immediately, never by waiting on the load inside the request.

And the rule the branches already kept: "could not ask" is never "no rows" -- a relay or a SQL database that
cannot be asked raises Unavailable, which is never an empty answer.

The tests degrade to plain behaviour assertions where an API is new (getattr), so the same file run against the
pre-fix merge fails on the behaviour, not on an import.
"""
import asyncio
import hashlib
import itertools
import os
import socket
import threading
import time
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import APIKey, Base, BlossomBlob, BlossomBlobOwner, Reminder, User
from app.services import api_key_store, blob_index, doc_table, doc_table_bulk, reminder_service, table_migration
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
    """What a freshly started process holds: no table loaded, no marker seen -- and no loader thread of an
    earlier "process" still running (an orphaned loader stops at its next pass; wait for it, or it could
    record a marker of the previous test's namespace in this one)."""
    doc_table.DocTable._registry.clear()
    end = time.monotonic() + 15
    while time.monotonic() < end and any(t.name.startswith("doctable-load-") for t in threading.enumerate()):
        time.sleep(0.1)
    doc_table_bulk._seen.clear()
    reset = getattr(doc_table, "reset_state", None)
    if reset:
        reset()
    for name in ("_pass_done",):
        from app.services import app_tables
        getattr(app_tables, name).clear()


@pytest.fixture
def node(shared_relay, monkeypatch):
    """This test's own namespace on the shared relay, and a SQL database with every moved table in it."""
    from app.services import keystore, settings_store
    monkeypatch.setattr(keystore, "get_operator_nsec", lambda: bech32.encode("nsec", SK))
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: {"nostr_relay_retention_days": "30"}.get(k, d))
    monkeypatch.setenv("POSTERCHANAI_RELAY_PORT", str(shared_relay.port))
    monkeypatch.setattr(doc_table, "NS", "pcai:nooutage%d:" % next(_ns))
    _cold_process()
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[User.__table__, APIKey.__table__, Reminder.__table__,
                                             BlossomBlob.__table__, BlossomBlobOwner.__table__])
    S = sessionmaker(bind=engine)
    db = S()
    db.add(User(id=1, username="alice", password_hash="x"))
    db.commit()
    db.close()
    yield S
    _cold_process()


def _bind(S):
    """What every process does at start (main.py, worker.py, role_runner): SQL sessions for the moving tables."""
    bind = getattr(table_migration, "bind", None)
    if bind is not None:
        bind(S)


def _seed_relay(table, rows):
    run(doc_table_bulk.abulk_put(table, rows))


def _mark(*tables):
    """The `_migrated` markers, as the copy writes them."""
    _seed_relay("_migrated", {t: {"ok": True, "verified": True, "rows": 0, "at": int(time.time())} for t in tables})


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


# ============================================================================ 1. before the marker: SQL answers
def test_before_the_marker_an_api_key_only_in_sql_is_accepted(node):
    db = node()
    db.add(APIKey(id=3, user_id=1, key=KEY, name="Default", is_active=True))
    db.commit()
    db.close()
    _bind(node)
    try:
        k = api_key_store.lookup(KEY)
    except Unavailable as e:
        pytest.fail("every sk- key is refused until the copy has run: %s" % e)
    assert k is not None and k.id == 3 and k.user_id == 1
    assert api_key_store.lookup("sk-" + "b" * 64) is None


def test_before_the_marker_a_new_key_lands_in_sql_and_on_the_relay_and_survives_the_copy(node):
    _bind(node)
    try:
        k = api_key_store.create(1, "laptop")
    except Unavailable as e:
        pytest.fail("a key could not be created while the table was being moved: %s" % e)
    db = node()
    row = db.query(APIKey).filter_by(key=k.key).one()
    db.close()
    assert row.id == k.id, "the key's id is not the one SQL holds"
    held = run(doc_table_bulk.aread_all("api_keys"))
    assert held.get(str(k.id), {}).get("key") == k.key, "the write skipped the relay"
    for name in ("api_key_index", "api_keys"):
        table_migration.migrate(name)
    _cold_process()
    doc_table.set_session_factory(None)              # from here the relay alone must answer
    assert api_key_store.lookup(k.key).id == k.id


def test_before_the_marker_reminders_and_blobs_are_served_from_sql(node):
    db = node()
    db.add(Reminder(id=5, user_id=1, text="only in SQL", due_at=datetime.utcnow() + timedelta(days=1),
                    status="pending", created_at=datetime.utcnow()))
    sha = "ab" * 32
    db.add(BlossomBlob(sha256=sha, pubkey="c" * 64, size=12, mime="image/png", created_at=1, storage="local",
                       path="/x", private=False, keep=False))
    db.add(BlossomBlobOwner(sha256=sha, pubkey="c" * 64, created_at=1, name="cat.png"))
    db.commit()
    db.close()
    _bind(node)
    me = type("U", (), {"id": 1})()
    try:
        got = run(reminder_service.alist_reminders(None, me))
        blob = run(blob_index.aget(sha))
    except Unavailable as e:
        pytest.fail("a table still in SQL answered 'unavailable': %s" % e)
    assert [r.text for r in got] == ["only in SQL"]
    assert blob is not None and blob.size == 12 and blob.owners == {"c" * 64: [1, "cat.png"]}


def test_a_write_racing_the_copy_is_not_lost(node, monkeypatch):
    """The copy reads SQL, then writes the relay. A write that lands in between (SQL first, then the relay)
    is overwritten on the relay by the copy's older snapshot -- unless the verification compares the relay
    with a FRESH SQL read and settles the difference, which it must."""
    db = node()
    db.add(Reminder(id=5, user_id=1, text="v1", due_at=datetime.utcnow() + timedelta(days=1), status="pending",
                    created_at=datetime.utcnow()))
    db.commit()
    db.close()
    _bind(node)
    first = type("R", (), {"id": 5})()
    real_put = doc_table_bulk.abulk_put
    raced = {"done": False}

    async def racing_put(table, rows, **kw):
        if table == "reminders" and not raced["done"]:
            raced["done"] = True
            # a cancel lands between the copy's SQL read and its relay write
            ok = await reminder_service.acancel_reminder(None, type("U", (), {"id": 1})(), first.id)
            assert ok
        return await real_put(table, rows, **kw)
    monkeypatch.setattr(doc_table_bulk, "abulk_put", racing_put)
    table_migration.migrate("reminders")
    assert raced["done"]
    db = node()
    assert db.get(Reminder, first.id).status == "cancelled"
    db.close()
    held = run(doc_table_bulk.aread_all("reminders"))
    assert held[str(first.id)]["status"] == "cancelled", "the copy's stale snapshot overwrote a newer write"


# ============================================================================ 2. after a restart: the load
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


def _start_loads(S):
    start = getattr(table_migration, "start_loading", None)
    if start is not None:
        start(S)


def test_after_a_restart_an_api_key_is_checked_while_its_table_loads(node, monkeypatch):
    _seed_relay("api_keys", {"3": _key_row()})
    _seed_relay("api_key_index", {hashlib.sha256(KEY.encode()).hexdigest(): {"id": 3}})
    _mark("api_keys", "api_key_index")
    _cold_process()
    slow = _SlowLoad(monkeypatch, "api_keys")
    try:
        _start_loads(node)
        k = _in_thread(lambda: api_key_store.lookup(KEY))
        assert k is not None and k.id == 3, "a valid key was refused while its table loaded"
        assert _in_thread(lambda: api_key_store.lookup("sk-" + "b" * 64)) is None
        assert run(api_key_store.alookup(KEY)).id == 3
        # main.py asks again later (start_background, on the event loop): that must not wait for the load
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
    _mark("blossom_blobs", "reminders")
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
    _mark("blossom_blobs", "reminders")
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


def test_the_loads_start_at_startup_on_threads_of_their_own(node):
    """main.py starts them on port 3051 BEFORE the hydrates (which can take a long while), and each one is a
    thread -- never a load inside a request on the event loop."""
    import pathlib
    src = (pathlib.Path(__file__).resolve().parents[1] / "app" / "main.py").read_text()
    assert "table_migration.start_loading(" in src
    assert src.index("table_migration.start_loading(") < src.index("async def _hydrate_settings")
    _mark(*[n for n in table_migration.tables() if n != "push_sent_wraps"])
    _cold_process()
    table_migration.start_loading(node)
    names = {t.name for t in threading.enumerate()}
    for name in table_migration.tables():
        if name != "push_sent_wraps":
            assert "doctable-load-" + name in names, name


# ============================================================================ "could not ask" is never "no rows"
def test_could_not_ask_is_never_no_rows(node, monkeypatch):
    # before the marker, with SQL unreachable: unavailable -- never "no such key"
    def broken():
        raise RuntimeError("database is down")
    doc_table.set_session_factory(broken)
    with pytest.raises(Unavailable):
        api_key_store.lookup(KEY)
    with pytest.raises(Unavailable):
        run(blob_index.aget("ab" * 32))
    # after it, with the relay unreachable, a point read and a whole-table read both raise
    _mark("api_keys", "api_key_index", "blossom_blobs")
    _cold_process()
    run(doc_table.amarked("api_keys"))
    run(doc_table.amarked("blossom_blobs"))
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
    _mark("blossom_blobs")
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
