"""A real relay for the app tables that moved to DocTable (#161, part A): reminders, scheduled posts, saved
searches, the Telegram social-reply map.

One RelayServer over the real PosterChanDB store per test MODULE (tests/test_doc_table.py's `_Relay`: the
relay's NIP-42 check, NIP-78 read gate, live fan-out and NIP-09 deletions are what DocTable depends on), and a
fresh DocTable namespace per TEST, so tests never see each other's rows without paying a relay start each.

    from tests.app_tables_harness import tables, shared_relay  # noqa: F401  (pytest fixtures)
"""
import itertools
import socket
from types import SimpleNamespace

import pytest

from app.services import app_tables, doc_table
from app.services.nostr import bech32
from tests.test_doc_table import SK, _Relay

_ns = itertools.count(1)


@pytest.fixture(scope="module")
def shared_relay(tmp_path_factory):
    r = _Relay(str(tmp_path_factory.mktemp("relay") / "relay"))
    try:
        yield r
    finally:
        r.close()


def _no_database(*a, **k):
    raise AssertionError("an app-table test opened the app's real database session")


def _isolate(monkeypatch, port, *, ready=True):
    from app.services import keystore
    import app.database
    # These tables left Postgres. A path under test that still opens the app's own session would reach the
    # node's REAL database (the default URL is the local production Postgres) -- refuse it outright.
    monkeypatch.setattr(app.database, "SessionLocal", _no_database)
    monkeypatch.setattr(keystore, "get_operator_nsec", lambda: bech32.encode("nsec", SK))
    monkeypatch.setenv("POSTERCHANAI_RELAY_PORT", str(port))
    monkeypatch.setattr(doc_table, "NS", "pcai:t%d:" % next(_ns))
    doc_table.DocTable._registry.clear()
    app_tables._pass_done.clear()
    if ready:
        for name in app_tables.TABLES:
            app_tables.mark_pass_done(name)


@pytest.fixture
def tables(shared_relay, monkeypatch):
    """Every app table empty, migrated (ready) and backed by the shared relay."""
    _isolate(monkeypatch, shared_relay.port)
    try:
        yield SimpleNamespace(relay=shared_relay, port=shared_relay.port)
    finally:
        doc_table.DocTable._registry.clear()
        app_tables._pass_done.clear()


@pytest.fixture
def unmigrated(shared_relay, monkeypatch):
    """The same, but no table has a migration verdict yet."""
    _isolate(monkeypatch, shared_relay.port, ready=False)
    try:
        yield SimpleNamespace(relay=shared_relay, port=shared_relay.port)
    finally:
        doc_table.DocTable._registry.clear()
        app_tables._pass_done.clear()


@pytest.fixture
def no_relay(monkeypatch):
    """Nothing listening on the relay port: every read must be "could not ask"."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    _isolate(monkeypatch, port)
    try:
        yield port
    finally:
        doc_table.DocTable._registry.clear()
        app_tables._pass_done.clear()


def fresh_process_view(name):
    """Another process's view of a table: a new instance that must load it from the relay."""
    doc_table.DocTable._registry.pop(name, None)
    return doc_table.DocTable(name)
