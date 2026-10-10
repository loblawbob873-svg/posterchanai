"""A real relay + a throwaway SQL database for the wave-2 app tables (#161: bots, user_settings, conversations).

One RelayServer over the real PosterChanDB store per test MODULE (tests/test_doc_table.py's `_Relay` -- the
relay's NIP-42 check, NIP-78 read gate, live fan-out and NIP-09 deletions are what DocTable depends on), a fresh
DocTable namespace per TEST, and an in-memory SQLite database holding the four legacy tables. The app's own
`SessionLocal` (whose default URL is this node's PRODUCTION Postgres) is replaced by the SQLite factory, so no
path under test can reach the real database.

    from tests.wave2_harness import relay_mode, sql_mode, no_relay, shared_relay  # noqa: F401  (fixtures)
"""
import itertools
import socket
from types import SimpleNamespace

import pytest

from app.services import doc_table, doc_table_bulk, table_gate
from app.services.nostr import bech32
from tests.test_doc_table import SK, _Relay

WAVE2 = ("bots", "user_settings", "conversations", "messages")
_ns = itertools.count(1)


@pytest.fixture(scope="module")
def shared_relay(tmp_path_factory):
    r = _Relay(str(tmp_path_factory.mktemp("relay") / "relay"))
    try:
        yield r
    finally:
        r.close()


def make_sql():
    """An in-memory SQLite database with the legacy tables, and a session factory for it."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.database import Base
    from app.models import Bot, Conversation, Message, User, UserSetting
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[User.__table__, Conversation.__table__, Message.__table__,
                                          UserSetting.__table__, Bot.__table__])
    return sessionmaker(autocommit=False, autoflush=False, bind=eng)


def add_user(factory, username="alice", npub=None):
    from app.models import User
    db = factory()
    try:
        u = User(username=username, password_hash="x", nostr_npub=npub)
        db.add(u)
        db.commit()
        return u.id
    finally:
        db.close()


def _reset():
    doc_table.DocTable._registry.clear()
    doc_table_bulk._seen.clear()
    table_gate._loading.clear()


def _isolate(monkeypatch, port, factory, *, migrated):
    from app.services import keystore
    import app.database
    monkeypatch.setattr(app.database, "SessionLocal", factory)
    monkeypatch.setattr(keystore, "get_operator_nsec", lambda: bech32.encode("nsec", SK))
    monkeypatch.setenv("POSTERCHANAI_RELAY_PORT", str(port))
    monkeypatch.setattr(doc_table, "NS", "pcai:w2t%d:" % next(_ns))
    _reset()
    if migrated:
        for name in WAVE2:
            doc_table_bulk._seen.add(name)


def _fixture(monkeypatch, port, *, migrated):
    factory = make_sql()
    _isolate(monkeypatch, port, factory, migrated=migrated)
    return SimpleNamespace(port=port, Session=factory)


@pytest.fixture
def relay_mode(shared_relay, monkeypatch):
    """Every wave-2 table migrated: the relay is authoritative."""
    try:
        yield _fixture(monkeypatch, shared_relay.port, migrated=True)
    finally:
        _reset()


@pytest.fixture
def sql_mode(shared_relay, monkeypatch):
    """No migration marker yet: SQL is authoritative, writes go to both."""
    try:
        yield _fixture(monkeypatch, shared_relay.port, migrated=False)
    finally:
        _reset()


@pytest.fixture
def no_relay(monkeypatch):
    """Nothing listening on the relay port (tables marked migrated): every read is "could not ask"."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    try:
        yield _fixture(monkeypatch, port, migrated=True)
    finally:
        _reset()


def fresh_process_view(name):
    """Another process's view of a table: a new instance that must load it from the relay."""
    doc_table.DocTable._registry.pop(name, None)
    return doc_table.DocTable(name)


def forget_markers():
    """A fresh process that has not yet seen any migration marker (it must read them from the relay)."""
    doc_table_bulk._seen.clear()
    doc_table.DocTable._registry.pop(doc_table_bulk.MARKER_TABLE, None)
