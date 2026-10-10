"""The Blossom blob index as relay documents (#161) — RUN against the shipped RelayServer + PosterChanDB.

`blossom_blobs` and `blossom_blob_owners` left Postgres. These pin what that move must not break, each one a
way users lose files:

  * "could not ask" is never "no rows": an index that is unreachable, still loading or not yet migrated makes
    the upload, listing, quota, delete, sweep and repair paths REFUSE (Unavailable → 503) — the sweep and the
    repair delete nothing;
  * the migration copies BOTH tables (owners merged into their blob), re-reads the relay and compares row for
    row before it writes a marker; it is idempotent, removes only its own stale copies, and refuses (no
    marker) on any mismatch — and before the marker the code serves blob metadata from SQL, read-only;
  * after the marker, none of these paths touches the SQL models at all;
  * a write made while the big table is still loading is not lost from memory (DocTable's load merge).
"""
import asyncio
import os
import time
from unittest import mock

import pytest

from app.services import blob_index, blob_index_migrate, blossom_service, doc_table, doc_table_bulk
from app.services.relay_reader import Unavailable
from tests.blob_index_harness import (alive, bind_sql, idx, idx_own, load, put, relay, reset_process_state,
                                      shared_relay, sql_blob, sql_session)
_FIXTURES = (idx, idx_own, relay, shared_relay)   # pytest finds fixtures by module name

A, B, C = "a" * 64, "b" * 64, "c" * 64
CFG = {"backend": "local", "blob_dir": "/tmp", "storage_url": "", "cache_mb": 0, "ttl_days": 0,
       "user_quota_gb": 0, "mirror_servers": [], "public_url": ""}


class _NoSQL:
    """A session that fails the test on ANY use — what "these paths no longer touch the SQL models" means."""
    def __getattr__(self, name):
        if name in ("rollback", "close"):
            return lambda *a, **k: None
        raise AssertionError("SQL touched after the migration: db.%s" % name)


@pytest.fixture
def cfg(tmp_path):
    c = dict(CFG, blob_dir=str(tmp_path / "blobs"))
    with mock.patch.object(blossom_service, "_cfg", lambda db: dict(c)):
        yield c


def sha(i):
    return "%064x" % i


# ------------------------------------------------------------------ migration
def test_migration_copies_both_tables_verifies_marks_and_is_idempotent(relay):  # noqa: F811
    reset_process_state()
    db = sql_session(
        blobs=[sql_blob(A, keep=True), sql_blob(B, expires_at=1234567890, private=True), sql_blob(C)],
        owners=[dict(sha256=A, pubkey="1" * 64, created_at=5, name="cat.jpg"),
                dict(sha256=A, pubkey="2" * 64, created_at=6, name=None),
                dict(sha256=B, pubkey="1" * 64, created_at=7, name="b.bin")])
    out = asyncio.run(blob_index_migrate.amigrate(db))
    assert out["verified"] and out["rows"] == 3 and out["owners"] == 3 and out["written"] == 3
    got = alive()
    assert got == {A, B, C}
    rows = asyncio.run(doc_table_bulk.aread_all(blob_index.TABLE))
    assert rows[A]["keep"] is True and rows[A]["owners"] == {"1" * 64: [5, "cat.jpg"], "2" * 64: [6, None]}
    assert rows[B]["expires_at"] == 1234567890 and rows[B]["private"] is True
    assert rows[C]["owners"] == {}
    marker = asyncio.run(doc_table_bulk.amarker(blob_index.TABLE))
    assert marker["verified"] and marker["rows"] == 3
    # idempotent: a second run (a restart) neither rewrites nor re-verifies
    reset_process_state()
    again = asyncio.run(blob_index_migrate.amigrate(db))
    assert again["skipped"]
    # and SQL was never written to
    from app.models import BlossomBlob, BlossomBlobOwner
    assert db.query(BlossomBlob).count() == 3 and db.query(BlossomBlobOwner).count() == 3


def test_an_interrupted_copy_is_finished_and_its_stale_copies_removed(relay):  # noqa: F811
    reset_process_state()
    # an earlier run copied A and a row that SQL no longer holds, then died before the marker
    asyncio.run(doc_table_bulk.abulk_put(blob_index.TABLE, {
        A: blob_index.new_row(pubkey=A, size=10, mime=None, created_at=1, expires_at=None, storage="local",
                              path="/x"),
        "f" * 64: blob_index.new_row(pubkey=A, size=1, mime=None, created_at=1, expires_at=None,
                                     storage="local", path="/y")}))
    db = sql_session(blobs=[sql_blob(A), sql_blob(B)])
    out = asyncio.run(blob_index_migrate.amigrate(db))
    assert out["verified"] and out["removed_stale"] == 1 and out["written"] == 2   # A differed (created_at)
    assert alive() == {A, B}


def test_a_copy_that_does_not_verify_writes_no_marker(relay):  # noqa: F811
    """The relay keeps losing one row: every settle round finds it missing again, so no marker -- and the index
    stays on SQL (its store of record), never on the incomplete copy."""
    reset_process_state()
    db = sql_session(blobs=[sql_blob(A), sql_blob(B)])
    bind_sql(db)
    real = doc_table_bulk.abulk_put

    async def drops_b(table, rows, **kw):
        return await real(table, {k: v for k, v in rows.items() if k != B}, **kw)
    with mock.patch.object(doc_table_bulk, "abulk_put", drops_b):
        with pytest.raises(doc_table_bulk.MigrationMismatch):
            asyncio.run(blob_index_migrate.amigrate(db))
    assert asyncio.run(doc_table_bulk.amarker(blob_index.TABLE)) is None, "a marker after a failed verify"
    assert not asyncio.run(blob_index.amigrated())
    assert asyncio.run(blob_index.aget(B)) is not None, "the row the relay lost is still served, from SQL"


def test_a_copy_whose_content_differs_writes_no_marker(relay):  # noqa: F811
    reset_process_state()
    db = sql_session(blobs=[sql_blob(A)])
    real = doc_table_bulk.abulk_put

    async def altered(table, rows, **kw):
        return await real(table, {k: (dict(v, keep=True) if k == A else v) for k, v in rows.items()}, **kw)
    with mock.patch.object(doc_table_bulk, "abulk_put", altered):
        with pytest.raises(doc_table_bulk.MigrationMismatch):
            asyncio.run(blob_index_migrate.amigrate(db))
    assert asyncio.run(doc_table_bulk.amarker(blob_index.TABLE)) is None


def test_before_the_marker_sql_answers_and_writes_land_in_both(relay, cfg):  # noqa: F811
    """#161 wave 1: before the index's marker exists SQL is its store of record. Uploads, listings and serving
    keep working (they used to answer 503 for the whole copy -- ~5.5 min per 100k blobs): reads come from
    SQL, and a write goes to SQL AND the relay, so the copy that follows loses nothing."""
    from app.models import BlossomBlob, BlossomBlobOwner
    reset_process_state()
    db = sql_session(blobs=[sql_blob(A, size=77)], owners=[dict(sha256=A, pubkey=A, created_at=1, name="a")])
    bind_sql(db)
    m = asyncio.run(blossom_service.get_blob_meta(db, A))
    assert m is not None and m.size == 77, "a blob that is in SQL was not served"
    assert [b.sha256 for b in blossom_service.list_for_pubkey(db, A)] == [A]
    data = b"hello"
    s = blossom_service.compute_sha256(data)
    with mock.patch.object(blossom_service, "_cache_put", lambda *a: None):
        asyncio.run(blossom_service.save_blob(db, B, data, "text/plain", filename="h.txt"))
    db.expire_all()
    assert db.get(BlossomBlob, s) is not None and \
        db.query(BlossomBlobOwner).filter_by(sha256=s, pubkey=B).one().name == "h.txt", "the upload skipped SQL"
    assert s in alive(), "the upload skipped the relay"
    assert {b.sha256 for b in blossom_service.list_for_pubkey(db, B)} == {s}
    out = asyncio.run(blob_index_migrate.amigrate(db))
    assert out["verified"] and out["rows"] == 2
    reset_process_state()                                   # a fresh process: the relay is authoritative now
    assert asyncio.run(blossom_service.get_blob_meta(None, s)).size == len(data)


# ------------------------------------------------------------------ after the marker: no SQL at all
def test_every_converted_path_runs_without_sql(idx, cfg):  # noqa: F811
    db = _NoSQL()
    data = b"some bytes"
    s = blossom_service.compute_sha256(data)
    with mock.patch.object(blossom_service, "_cache_put", lambda *a: None):
        asyncio.run(blossom_service.save_blob(db, A, data, "text/plain", filename="n.txt", keep=True))
        asyncio.run(blossom_service.save_blob(db, B, data, "text/plain", filename="other.txt"))
    load()
    assert [b.sha256 for b in blossom_service.list_for_pubkey(db, A)] == [s]
    assert [b.sha256 for b in blossom_service.list_for_pubkey(db, B)] == [s]
    assert blossom_service.usage_for_pubkey(db, A) == len(data)
    assert blossom_service.names_for_pubkey(db, A) == {s: "n.txt"}
    assert asyncio.run(blossom_service.name_for(db, s, B)) == "other.txt"
    assert asyncio.run(blossom_service.is_owner(db, s, A))
    assert asyncio.run(blossom_service.get_blob_meta(db, s)).size == len(data)
    assert asyncio.run(blossom_service.expire_blob_in(db, s, 3))
    assert asyncio.run(blossom_service.clear_blob_expiry(db, s))
    assert asyncio.run(blossom_service.release_owner(db, s, B)) == 1
    assert blossom_service.list_for_pubkey(db, B) == []
    assert blossom_service.list_for_pubkey(db, A)[0].keep is True


def test_a_second_owner_of_the_same_bytes_is_recorded_and_keep_only_rises(idx, cfg):  # noqa: F811
    data = b"dedup me"
    s = blossom_service.compute_sha256(data)
    with mock.patch.object(blossom_service, "_cache_put", lambda *a: None):
        asyncio.run(blossom_service.save_blob(None, A, data, "x/y", keep=True, filename="first"))
        asyncio.run(blossom_service.save_blob(None, B, data, "x/y", keep=False, filename="second"))
    row = asyncio.run(doc_table_bulk.aread_all(blob_index.TABLE))[s]
    assert set(row["owners"]) == {A, B}
    assert row["keep"] is True, "an ordinary re-upload un-kept drive content"
    assert row["pubkey"] == A, "the first uploader stays the row's attribution"


def test_concurrent_first_uploads_of_the_same_bytes_keep_both_owners(idx, cfg):  # noqa: F811
    data = b"raced"

    async def both():
        with mock.patch.object(blossom_service, "_cache_put", lambda *a: None):
            await asyncio.gather(blossom_service.save_blob(None, A, data, "x/y"),
                                 blossom_service.save_blob(None, B, data, "x/y"))
    asyncio.run(both())
    row = asyncio.run(doc_table_bulk.aread_all(blob_index.TABLE))[blossom_service.compute_sha256(data)]
    assert set(row["owners"]) == {A, B}, "the race loser's (or winner's) reference was lost"


# ------------------------------------------------------------------ "could not ask"
def test_whole_table_paths_refuse_until_the_index_is_loaded(idx, cfg):  # noqa: F811
    put(A, owners=(A,))
    reset_process_state()
    db = _NoSQL()
    for fn in (lambda: blossom_service.list_for_pubkey(db, A), lambda: blossom_service.usage_for_pubkey(db, A),
               lambda: blob_index.rows(), lambda: blob_index.count()):
        with pytest.raises(Unavailable):
            fn()
    with pytest.raises(Unavailable):
        asyncio.run(blossom_service.scan_store(db))
    with pytest.raises(Unavailable):
        asyncio.run(blossom_service.forget_missing(db, [A]))
    # ...while a POINT lookup works before the load, from that one document
    assert asyncio.run(blossom_service.get_blob_meta(db, A)).sha256 == A
    assert asyncio.run(blossom_service.is_owner(db, A, A))


def test_an_unreachable_relay_deletes_nothing_and_answers_nothing(idx_own, cfg):  # noqa: F811
    put(A, owners=(A,), age_days=400)
    put(B, owners=(A,), expires_at=int(time.time()) - 60)
    idx_own.close()                                  # the relay goes away; this process never loaded the table
    reset_process_state()
    doc_table_bulk._seen.add(blob_index.TABLE)   # even with the marker already seen
    db = _NoSQL()
    deleted = []
    with mock.patch.object(blossom_service, "SessionLocal", lambda: db), \
            mock.patch.object(blossom_service, "_cfg", lambda d: dict(CFG, ttl_days=1)), \
            mock.patch.object(blossom_service, "delete_blob_bytes",
                              mock.AsyncMock(side_effect=lambda d, b, **k: deleted.append(b.sha256))):
        assert blossom_service._cleanup_once() == 0
    assert deleted == [], "the sweep deleted bytes on an index it could not read"
    with pytest.raises(Unavailable):
        asyncio.run(blossom_service.get_blob_meta(db, A))      # never None: that is a 404 for a real file
    with pytest.raises(Unavailable):
        asyncio.run(blossom_service.save_blob(db, A, b"x", "x/y"))
    with pytest.raises(Unavailable):
        blossom_service.list_for_pubkey(db, A)


def test_an_unreachable_relay_mid_session_stops_the_repair(idx_own, cfg):  # noqa: F811
    """Loaded, then the relay dies: the repair's deletes are refused and nothing is reported removed."""
    for i in range(3):
        put(sha(i), owners=(A,))
    load()
    idx_own.close()

    async def gone(_p):
        return "gone"
    with mock.patch.object(blossom_service, "_probe_local", gone):
        out = asyncio.run(blossom_service.forget_missing(None, [sha(0)]))
    assert out["removed"] == 0 and out["refused"], out


# ------------------------------------------------------------------ the load race (DocTable)
def test_a_write_made_while_the_table_loads_is_not_lost(idx, cfg):  # noqa: F811
    for i in range(5):
        put(sha(i), owners=(A,))
    reset_process_state()
    assert asyncio.run(blob_index.amigrated())
    t = blob_index.table()
    real = doc_table.nostr_store.list_all_docs

    async def slow_listing(*a, **k):
        docs = await real(*a, **k)           # the listing has been taken...
        row = blob_index.new_row(pubkey=B, size=1, mime=None, created_at=1, expires_at=None,
                                 storage="local", path="/w")
        row["owners"][B] = [1, None]
        await blob_index.aupdate(sha(99), lambda _c: row)   # ...and a write lands behind it,
        await asyncio.sleep(6.5)             # ...longer before the load ends than a reconnect's 5s replay window
        return docs
    with mock.patch.object(doc_table.nostr_store, "list_all_docs", slow_listing):
        asyncio.run(t.aload())
    assert t.peek(sha(99)) is not None, "an upload made during the load vanished from memory"
    assert [b.sha256 for b in blob_index.owned_by(B)] == [sha(99)]


def test_a_write_by_another_process_reaches_the_owner_index(idx, cfg):  # noqa: F811
    load()
    theirs = object.__new__(doc_table.DocTable)     # a second process's view of the same table
    theirs._init(blob_index.TABLE)
    row = blob_index.new_row(pubkey=B, size=3, mime=None, created_at=1, expires_at=None, storage="local", path="/z")
    row["owners"][B] = [1, None]
    asyncio.run(theirs.aput(C, row))
    end = time.monotonic() + 8
    while time.monotonic() < end and not blob_index.owned_by(B):
        time.sleep(0.1)
    assert [b.sha256 for b in blob_index.owned_by(B)] == [C], "another process's write never reached the index"
    asyncio.run(theirs.adelete(C))
    end = time.monotonic() + 8
    while time.monotonic() < end and blob_index.owned_by(B):
        time.sleep(0.1)
    assert blob_index.owned_by(B) == [], "another process's delete never reached the index"


# ------------------------------------------------------------------ scale
def test_load_cost_per_row_is_measured(idx, cfg):  # noqa: F811
    """A scaled-down measurement that runs in the suite: N rows copied, then a strict load, timed. The
    100k figure is PC_MEASURE_ROWS=100000 (minutes; see the commit message for the numbers)."""
    import tracemalloc
    n = int(os.environ.get("PC_MEASURE_ROWS", "2000"))
    rows = {}
    for i in range(n):
        r = blob_index.new_row(pubkey="%064x" % (i % 300), size=1000 + i, mime="image/jpeg",
                               created_at=1700000000 + i, expires_at=None, storage="proxy",
                               path="blossom/%02x/%064x" % (i % 256, i), keep=bool(i % 2))
        r["owners"]["%064x" % (i % 300)] = [1700000000 + i, None]
        rows[sha(i)] = r
    t0 = time.time()
    asyncio.run(doc_table_bulk.abulk_put(blob_index.TABLE, rows))
    t_copy = time.time() - t0
    reset_process_state()
    doc_table.nostr_store._PLAIN_CACHE.clear()
    doc_table.nostr_store._PLAIN_BYTES[0] = 0
    tracemalloc.start()
    t0 = time.time()
    load()
    t_load = time.time() - t0
    doc_table.nostr_store._PLAIN_CACHE.clear()
    doc_table.nostr_store._PLAIN_BYTES[0] = 0
    mem = tracemalloc.get_traced_memory()[0]
    tracemalloc.stop()
    assert blob_index.count() == n
    print("\n[measure] %d rows: copy %.1fs (%.2f ms/row), strict load %.1fs (%.2f ms/row), held %.0f MB (%.0f B/row)"
          % (n, t_copy, t_copy * 1000 / n, t_load, t_load * 1000 / n, mem / 1e6, mem / n))
    assert t_load / n < 0.01, "a strict load costs more than 10 ms per row"


# ------------------------------------------------------------------ the routes say 503, never 404 / []
def _request(method="GET", path="/blossom/x", headers=None):
    from starlette.requests import Request
    hdrs = [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()]
    return Request({"type": "http", "method": method, "path": path, "query_string": b"", "headers": hdrs,
                    "client": ("127.0.0.1", 1), "server": ("127.0.0.1", 3051), "scheme": "http"})


def test_the_routes_answer_503_when_the_index_cannot_be_asked(idx_own, cfg):  # noqa: F811
    from app.routers import blossom as R
    put(A, owners=(A,))
    idx_own.close()
    reset_process_state()
    doc_table_bulk._seen.add(blob_index.TABLE)
    with mock.patch.object(blossom_service, "is_enabled", lambda db: True):
        got = asyncio.run(R._serve_blob(A, _request(), _NoSQL()))
        assert got.status_code == 503, "a blob that may well be there was answered %d" % got.status_code
        assert got.headers.get("retry-after")
        listing = asyncio.run(R.list_blobs(A, _request(), db=_NoSQL()))
        assert listing.status_code == 503, "an unreadable index was listed as a drive"


def test_a_blob_get_before_the_marker_is_served_from_sql(relay, cfg):  # noqa: F811
    from app.routers import blossom as R
    reset_process_state()
    db = sql_session(blobs=[sql_blob(A, size=5, mime="image/png")])
    bind_sql(db)
    with mock.patch.object(blossom_service, "is_enabled", lambda d: True):
        got = asyncio.run(R._serve_blob(A, _request("HEAD"), db))
    assert got.status_code == 200 and got.headers["content-length"] == "5"


# ------------------------------------------------------------------ the startup path
def test_startup_migrates_then_loads_in_the_background(relay, cfg):  # noqa: F811
    reset_process_state()
    db = sql_session(blobs=[sql_blob(A), sql_blob(B)],
                     owners=[dict(sha256=A, pubkey=C, created_at=1, name="x.txt")])

    bind_sql(db)
    blob_index.start_background()                   # waits for the marker: SQL answers meanwhile
    time.sleep(0.5)
    assert not blob_index.table().loaded, "loaded an index that was not copied yet"
    assert [b.sha256 for b in blossom_service.list_for_pubkey(None, C)] == [A]      # from SQL
    asyncio.run(blob_index_migrate.amigrate(db))
    end = time.monotonic() + 30
    while time.monotonic() < end and not blob_index.table().loaded:
        time.sleep(0.2)
    assert blob_index.table().loaded, "the index never loaded after the copy"
    doc_table.set_session_factory(None)             # from here nothing may need SQL
    assert [b.sha256 for b in blossom_service.list_for_pubkey(None, C)] == [A]
    assert blob_index.count() == 2


# ------------------------------------------------------------------ nothing reads the old tables any more
def test_no_code_path_queries_the_old_tables():
    """Only the one-time copy, and blob metadata DURING the copy, may read them; nothing may write them."""
    import pathlib
    import re
    root = pathlib.Path(__file__).resolve().parents[1]
    allowed = {"app/models.py", "app/database.py", "app/services/blob_index_migrate.py",
               "app/services/stream_vod_store.py",
               # BlobLegacy: the SQL side DocTable reads and writes until the marker exists (#161 wave 1)
               "app/services/blob_index.py"}
    bad = []
    for p in list((root / "app").rglob("*.py")) + list((root / "scripts").rglob("*.py")) \
            + list((root / "botframework").rglob("*.py")):
        rel = str(p.relative_to(root))
        if rel in allowed:
            continue
        src = p.read_text(encoding="utf-8", errors="replace")
        for m in re.finditer(r"\b(BlossomBlob|BlossomBlobOwner|StreamVOD|ProxyImageCache)\b|"
                             r"\b(blossom_blobs|blossom_blob_owners|stream_vods|proxy_image_cache)\b\s+(where|set)",
                             src):
            line = src[:m.start()].count("\n") + 1
            text = src.splitlines()[line - 1].strip()
            if text.startswith("#") or '"""' in text:
                continue
            bad.append("%s:%d: %s" % (rel, line, text))
    assert not bad, "the old tables are still used:\n" + "\n".join(bad)
    src = (root / "app/services/blob_index.py").read_text()
    legacy = src[src.index("class BlobLegacy("):src.index("_LEGACY = BlobLegacy()")]
    assert "commit" not in legacy, "the Legacy commits on its own: DocTable commits, SQL first, then the relay"


def test_an_incomplete_read_leaves_the_index_unloaded_and_the_sweep_idle(idx, cfg):  # noqa: F811
    """A namespace that cannot be read to its end is not a smaller table."""
    put(A, owners=(A,), expires_at=int(time.time()) - 60)
    reset_process_state()

    async def torn(*a, **k):
        raise doc_table.nostr_store.IncompleteRead("blossom_blobs could not be read to its end")
    deleted = []
    with mock.patch.object(doc_table.nostr_store, "list_all_docs", torn):
        with pytest.raises(Unavailable):
            load()
        with mock.patch.object(blossom_service, "SessionLocal", lambda: _NoSQL()), \
                mock.patch.object(blossom_service, "delete_blob_bytes",
                                  mock.AsyncMock(side_effect=lambda d, b, **k: deleted.append(b.sha256))):
            assert blossom_service._cleanup_once() == 0
    assert deleted == [] and alive() == {A}
