"""Shared set-up for the blob-index tests: the SHIPPED RelayServer over the real PosterChanDB store (the
test_doc_table harness), a SQLite copy of the old SQL tables for the migration to read, and helpers to put rows
into the index the way the app does. Not a test module itself."""
import asyncio
import itertools
import os
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import BlossomBlob, BlossomBlobOwner, StreamVOD
from app.services import blob_index, blossom_service, doc_table, doc_table_bulk
from tests.test_doc_table import relay

DAY = 86400
_bound: set = set()
_FIXTURES = (relay,)       # re-exported: a module that imports `relay` from here gets the fixture


def reset_process_state():
    """What a fresh process would hold: no tables, no markers seen, empty caches."""
    doc_table.DocTable._registry.clear()
    doc_table_bulk._seen.clear()
    doc_table.set_session_factory(None)
    while _bound:                       # only the test-named tables `bind_sql` gave a SQL side -- never the real one
        doc_table.LEGACIES.pop(_bound.pop(), None)
    with blob_index._idx_lock:
        blob_index._by_owner.clear()
        blob_index._owners_of.clear()
    blob_index._locks.clear()
    with blossom_service._meta_lock:
        blossom_service._meta_cache.clear()
    with blossom_service._cache_lock:
        blossom_service._cache.clear()


_seq = itertools.count(1)


def _fresh_index(monkeypatch):
    """A table name of this test's own, so tests sharing one relay never see each other's rows, and an
    EMPTY verified index under it (marker written) seen from a fresh process."""
    monkeypatch.setattr(blob_index, "TABLE", "blossom_blobs_t%d_%d" % (os.getpid(), next(_seq)))
    reset_process_state()
    asyncio.run(doc_table_bulk.amigrate_rows(blob_index.TABLE, {}))
    reset_process_state()


@pytest.fixture(scope="module")
def shared_relay(tmp_path_factory):
    """ONE relay for a module: starting and stopping the shipped relay per test costs ~10s at teardown."""
    from app.services import keystore
    from app.services.nostr import bech32
    from tests.test_doc_table import SK, _Relay
    mp = pytest.MonkeyPatch()
    mp.setattr(keystore, "get_operator_nsec", lambda: bech32.encode("nsec", SK))
    r = _Relay(str(tmp_path_factory.mktemp("blobrelay") / "relay"))
    mp.setenv("POSTERCHANAI_RELAY_PORT", str(r.port))
    try:
        yield r
    finally:
        mp.undo()
        r.close()


@pytest.fixture
def idx(shared_relay, monkeypatch):
    """The module's relay with an empty, verified, private blob index for this test."""
    monkeypatch.setenv("POSTERCHANAI_RELAY_PORT", str(shared_relay.port))
    _fresh_index(monkeypatch)
    yield shared_relay
    reset_process_state()


@pytest.fixture
def idx_own(relay, monkeypatch):  # noqa: F811
    """Like `idx`, on a relay of the test's own — for a test that takes the relay away."""
    _fresh_index(monkeypatch)
    yield relay
    reset_process_state()


def sql_session(blobs=(), owners=(), vods=()):
    """An in-memory SQLite database holding the old tables, with the REAL schema."""
    from sqlalchemy.pool import StaticPool
    # one shared connection, usable from the migration's worker thread (Postgres has no such limit)
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    BlossomBlob.metadata.create_all(eng, tables=[BlossomBlob.__table__, BlossomBlobOwner.__table__,
                                                 StreamVOD.__table__])
    db = sessionmaker(bind=eng)()
    for b in blobs:
        db.add(BlossomBlob(**b))
    for o in owners:
        db.add(BlossomBlobOwner(**o))
    for v in vods:
        db.add(StreamVOD(**v))
    db.commit()
    return db


def sql_blob(sha, *, pubkey="a" * 64, size=10, age_days=0, keep=False, expires_at=None, private=False,
             storage="local", path=None, mime="application/octet-stream"):
    return dict(sha256=sha, pubkey=pubkey, size=size, mime=mime, created_at=int(time.time()) - age_days * DAY,
                expires_at=expires_at, storage=storage, path=path or "/tmp/" + sha[:8], private=private, keep=keep)


def put(sha, *, owners=("a" * 64,), age_days=0, keep=False, expires_at=None, private=False, size=10,
        storage="local", path=None, pubkey=None, mime="application/octet-stream"):
    """Write one blob row into the index through the app's own write path."""
    row = blob_index.new_row(pubkey=pubkey or (owners[0] if owners else "a" * 64), size=size, mime=mime,
                             created_at=int(time.time()) - age_days * DAY, expires_at=expires_at,
                             storage=storage, path=path or "/tmp/" + sha[:8], private=private, keep=keep)
    for pk in owners:
        row["owners"][pk] = [int(time.time()), None]
    asyncio.run(blob_index.aupdate(sha, lambda _cur: row))


def load():
    """Load the whole index into this process, strictly (what the background thread does)."""
    blob_index.table().load(force=True)


def alive() -> set:
    """The shas the relay holds, read independently of this process's memory."""
    return set(asyncio.run(doc_table_bulk.aread_all(blob_index.TABLE)))


def bind_sql(db):
    """Make this test's index table one that still lives in SQL (#161 wave 1): its Legacy bound and the session
    factory pointed at `db`'s database -- what `table_migration.bind(SessionLocal)` does for the real table."""
    if blob_index.TABLE not in doc_table.LEGACIES:
        _bound.add(blob_index.TABLE)
    doc_table.LEGACIES[blob_index.TABLE] = blob_index.BlobLegacy()
    doc_table.DocTable._registry.pop(blob_index.TABLE, None)
    doc_table.set_session_factory(sessionmaker(bind=db.get_bind()))
