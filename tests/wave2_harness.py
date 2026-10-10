"""A real relay for the relay-only app tables (#161): bots, user_settings, conversations, users, wallet backups ...

One RelayServer over the real PosterChanDB store per test MODULE (tests/test_doc_table.py's `_Relay` -- the relay's
NIP-42 check, NIP-78 read gate, live fan-out and NIP-09 deletions are what DocTable depends on), and a fresh
DocTable namespace per TEST. There is no SQL anywhere: the app's `db` is app.database's NoSQL session, which any
leftover SQL path would trip over loudly.

    from tests.wave2_harness import relay_mode, no_relay, shared_relay  # noqa: F401  (fixtures)
"""
import itertools
import socket
from types import SimpleNamespace

import pytest

from app.services import doc_table
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


def _reset():
    doc_table.DocTable._registry.clear()
    doc_table.reset_state()
    try:
        from app.services import user_table
        user_table._bg["loading"] = False
    except ImportError:
        pass


def use_relay(monkeypatch, port) -> None:
    """Point this test at the relay on `port`: the operator key, the port, and a namespace of its own."""
    from app.services import keystore
    monkeypatch.setattr(keystore, "get_operator_nsec", lambda: bech32.encode("nsec", SK))
    monkeypatch.setenv("POSTERCHANAI_RELAY_PORT", str(port))
    monkeypatch.setattr(doc_table, "NS", "pcai:w2t%d:" % next(_ns))
    _reset()


def no_sql():
    """The `db` the app hands around now (commit/close are no-ops; a query raises)."""
    from app.database import NoSQL
    return NoSQL()


@pytest.fixture
def relay_mode(shared_relay, monkeypatch):
    """The relay up, the tables empty."""
    use_relay(monkeypatch, shared_relay.port)
    try:
        yield SimpleNamespace(port=shared_relay.port, db=no_sql)
    finally:
        _reset()


@pytest.fixture
def no_relay(monkeypatch):
    """Nothing listening on the relay port: every read and write is "could not ask"."""
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    use_relay(monkeypatch, port)
    try:
        yield SimpleNamespace(port=port, db=no_sql)
    finally:
        _reset()


def fresh_process_view(name):
    """Another process's view of a table: a new instance that must load it from the relay."""
    doc_table.DocTable._registry.pop(name, None)
    return doc_table.DocTable(name)
