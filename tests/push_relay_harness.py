"""The push tables live on the relay now (#161): a REAL RelayServer + PosterChanDB store for push tests.

`push_relay` starts the shipped relay (tests/test_doc_table.py's harness), points the operator key and the
relay port at it, and runs the real push-table migration against an EMPTY SQL database -- so the gate is
open exactly the way a fresh node opens it, not by a test shortcut. `add_sub(...)` writes a device row
through push_store, the same path the routes use.
"""
import asyncio
import socket

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.services import doc_table, push_store
from app.services.nostr import bech32
from tests.test_doc_table import SK, _Relay


class Relay(_Relay):
    def close(self):
        # The DocTable live streams hold their sockets open, so the server's wait_closed would sit out
        # the base harness's 10s join on every test. The thread is a daemon; do not wait for it.
        self._h["loop"].call_soon_threadsafe(self._h["stop"].set)
        self._t.join(0.5)


def empty_sql():
    """An empty SQL database WITH the push tables (every real node has them: they are never dropped)."""
    from app.models import DirectPushMessage, PushFollowSeen, PushSentWrap, PushSubscription
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    for t in (PushSubscription, DirectPushMessage, PushFollowSeen, PushSentWrap):
        t.__table__.create(engine)
    return sessionmaker(bind=engine)


def reset():
    """A cold process view: no table loaded, no marker seen, nothing bound to SQL."""
    doc_table.DocTable._registry.clear()
    doc_table.reset_state()


def start(tmp_path, monkeypatch, *, migrate=True):
    from app.services import keystore
    monkeypatch.setattr(keystore, "get_operator_nsec", lambda: bech32.encode("nsec", SK))
    r = Relay(str(tmp_path / "relay"))
    monkeypatch.setenv("POSTERCHANAI_RELAY_PORT", str(r.port))
    reset()
    if migrate:
        push_store.migrate_all(empty_sql())
        reset()                          # the code under test starts from a cold process view
    return r


def dead_relay(monkeypatch):
    """Point everything at a port nothing listens on: every read is "could not ask"."""
    from app.services import keystore
    monkeypatch.setattr(keystore, "get_operator_nsec", lambda: bech32.encode("nsec", SK))
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    monkeypatch.setenv("POSTERCHANAI_RELAY_PORT", str(port))
    reset()


@pytest.fixture
def push_relay(tmp_path, monkeypatch):
    r = start(tmp_path, monkeypatch)
    try:
        yield r
    finally:
        reset()
        r.close()


def add_sub(**fields) -> dict:
    row = {"pubkey": "b" * 64, "endpoint": "https://push.example/x", "transport": "webpush",
           "device_id": None, "token_hash": None, "last_seen": None, "p256dh": "p", "auth": "a",
           "prefs": None}
    row.update(fields)
    return asyncio.run(push_store.put_sub(row))


def subs() -> list:
    return push_store.all_subs_sync()


def fresh_subs() -> list:
    """What ANOTHER process would read: a cold view, loaded from the relay."""
    reset()
    return push_store.all_subs_sync()
