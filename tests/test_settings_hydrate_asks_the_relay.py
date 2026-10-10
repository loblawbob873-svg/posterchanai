"""Settings are read from THIS node's relay, never from its Postgres tables (#161: retiring Postgres as the
relay's store) -- and "could not ask" never becomes "the relay holds no settings".

What used to be one SQL statement against `events`/`event_tags` is now a NIP-42-authenticated REQ for the
operator's `pcai:setting:` documents (`#d~` prefix), paged to the END of the namespace. Three rules carry over
from the SQL read and each has a test that fails without it:

  * operator-signed documents only, newest wins per d-tag;
  * a relay that cannot be asked changes nothing: no key is hydrated, `is_hydrated()` stays False, nothing is
    written back, and the legacy-table migration (which skips "keys the relay holds") does not run on an empty
    answer -- or every stale legacy row would overwrite the relay's newer value;
  * the RELAY PROCESS reads its own settings from its own store (it reads them before its websocket server
    listens, and from inside its own event loop), so relay_main opens the store FIRST.
"""
import json
import socket

import pytest

from app.services import relay_reader, settings_store as S
from app.services.nostr import bip340, nip44
from tests.relay_fake import FakeRelay, ShippedRelay, ev

OP_SK = bytes.fromhex("42" * 32)
OP = bip340.pubkey_from_seckey(OP_SK).hex()
STRANGER = "ee" * 32


def _doc(key, value, at, *, author=OP, sk=OP_SK):
    return ev(author, 30078, at, tags=[["d", "pcai:setting:" + key]],
              content=nip44.encrypt_self(sk, json.dumps({"value": value})))


@pytest.fixture
def store(monkeypatch):
    """A cold process: empty cache, never hydrated, the operator key known, nothing may be written."""
    monkeypatch.setattr(S, "_CACHE", {"keep_me": "default"})
    monkeypatch.setattr(S, "_HYDRATED", False)
    monkeypatch.setattr(S, "_HYDRATED_KEYS", set())
    monkeypatch.setattr(S, "_LOCAL_KEYS", set())
    monkeypatch.setattr(S, "_OP_SK", None)
    monkeypatch.setattr(S, "_EVENT_SOURCE", None, raising=False)
    monkeypatch.setattr(S, "_operator_seckey", lambda db: OP_SK)
    writes = []
    monkeypatch.setattr(S, "_schedule_relay_write", lambda changes: writes.append(dict(changes)))
    monkeypatch.setattr(S, "_save_local_file", lambda: None)

    def refuse(*a, **k):
        raise AssertionError("settings were read from a database; they must be asked of the relay")
    monkeypatch.setattr("psycopg2.connect", refuse)
    return writes


class _NoSQL:
    """A db session that is only good for finding the operator key -- any SQL against it is the old read."""
    def execute(self, *a, **k):
        raise AssertionError("hydrate ran SQL against the relay's tables")

    def rollback(self):
        pass


def _relay(monkeypatch, events):
    r = FakeRelay(events)
    monkeypatch.setattr(S, "_port", lambda db=None: r.port)
    return r


def test_hydrate_asks_the_relay_as_the_operator_and_keeps_operator_signed_newest(store, monkeypatch):
    r = _relay(monkeypatch, [
        _doc("site_name", "old name", 1700000000),
        _doc("site_name", "new name", 1700000500),          # newest wins
        _doc("llm_model", "qwen", 1700000100),
        _doc("llm_model", "evil", 1800000000, author=STRANGER, sk=bytes.fromhex("11" * 32)),
        ev(OP, 30078, 1700000200, tags=[["d", "pcai:user:alice"]], content="x"),   # not a setting
    ])
    try:
        n = S.hydrate_from_db(_NoSQL())
    finally:
        r.close()
    assert S.get("site_name") == "new name"
    assert S.get("llm_model") == "qwen", "a document the operator did not sign was taken as a setting"
    assert n == 2 and S.is_hydrated()
    assert OP in r.auths, "kind 30078 is served only to its author; the read must sign in as the operator"
    assert store == []


def test_a_relay_that_cannot_be_asked_hydrates_nothing_and_writes_nothing(store, monkeypatch):
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    monkeypatch.setattr(S, "_port", lambda db=None: port)
    assert S.hydrate_from_db(_NoSQL()) == 0
    assert not S.is_hydrated(), "could not ask was recorded as 'the cache reflects the relay'"
    assert S.get("keep_me") == "default" and S._HYDRATED_KEYS == set()
    # The legacy migration skips keys the relay holds -- and with nothing read that is NO key, so every
    # stale legacy row would be written over the relay. It must wait for a real read.
    class Legacy:
        bind = object()

        def execute(self, *a, **k):
            class R:
                def fetchall(self):
                    return [("site_name", "a stale legacy value")]
            return R()

    class Has:
        def has_table(self, name):
            return name == "settings"
    monkeypatch.setattr("sqlalchemy.inspect", lambda bind: Has())
    assert S.migrate_legacy_table(Legacy()) == 0
    assert S.get("site_name") is None, "a legacy row was migrated over a relay nobody had read"
    keys, ok = S._relay_setting_keys_from_db(None)
    assert ok is False, "an unreadable relay must never authorize the first-boot seed"
    assert store == []


def test_a_short_answer_is_paged_to_the_end_not_taken_as_everything(store, monkeypatch):
    monkeypatch.setattr(S, "_SETTINGS_PAGE", 3)
    docs = [_doc("k%02d" % i, "v%d" % i, 1700000000 + (i // 2)) for i in range(11)]   # shared seconds too
    r = _relay(monkeypatch, docs)
    try:
        S.hydrate_from_db(None)
        keys, ok = S._relay_setting_keys_from_db(None)
    finally:
        r.close()
    assert all(S.get("k%02d" % i) == "v%d" % i for i in range(11))
    assert ok and keys == {"k%02d" % i for i in range(11)}


def test_the_relay_process_reads_its_own_store_not_its_own_socket(store, monkeypatch):
    def no_socket(*a, **k):
        raise AssertionError("the relay process asked its own websocket (not listening yet / its own loop)")
    monkeypatch.setattr(relay_reader, "query", no_socket)
    asked = []

    def source(filters):
        asked.append(filters)
        return [_doc("site_name", "from the store", 1700000000)]
    S.set_event_source(source)
    assert S.hydrate_from_db(None) == 1 and S.get("site_name") == "from the store"
    assert asked and asked[0][0]["authors"] == [OP] and asked[0][0]["#d~"] == ["pcai:setting:"]

    def broken(filters):
        raise RuntimeError("store closed")
    monkeypatch.setattr(S, "_HYDRATED", False)
    S.set_event_source(broken)
    assert S.hydrate_from_db(None) == 0 and not S.is_hydrated()
    assert S._relay_setting_keys_from_db(None)[1] is False


def test_relay_main_opens_its_store_before_it_reads_its_settings(monkeypatch):
    import relay_main
    from app.services.nostr_relay import thread as T
    order = []
    monkeypatch.setattr(T, "open_store_for_settings", lambda loop: order.append("store") or object())
    monkeypatch.setattr(T, "_read_config", lambda: order.append("settings") or {"enabled": False})
    import asyncio
    try:
        relay_main.main()
    finally:
        asyncio.set_event_loop(None)
    assert order == ["store", "settings"]


def test_open_store_for_settings_makes_the_store_the_settings_source(monkeypatch):
    from app.services.nostr_relay import thread as T
    monkeypatch.setattr(S, "_EVENT_SOURCE", None)
    monkeypatch.setattr(S, "load_local", lambda: None)
    seen = {}

    class Store:
        def __init__(self, dsn):
            seen["dsn"] = dsn

        def open(self, loop):
            seen["open"] = loop

        def _query_sync(self, filters, cap):
            return [{"filters": filters, "cap": cap}]
    monkeypatch.setattr(T, "RelayStore", Store)
    monkeypatch.setattr(T, "_pg_dsn", lambda: "dbname=x")
    st = T.open_store_for_settings("LOOP")
    assert isinstance(st, Store) and seen == {"dsn": "dbname=x", "open": "LOOP"}
    assert S._EVENT_SOURCE([{"a": 1}]) == [{"filters": [{"a": 1}], "cap": 5000}]


# --- against the SHIPPED relay server ---------------------------------------------------------------------
# The fakes above speak the protocol as written down; this speaks it with the real RelayServer (its NIP-42 check
# of the relay URL and challenge, its NIP-78 read gate, its NIP-45 COUNT), over a real socket, with only the
# event store replaced by memory. A reader that signs the wrong relay URL, or a COUNT the relay would refuse,
# passes every fake and fails here.

@pytest.fixture
def real_relay():
    servers = []

    def start(events, cfg=None):
        srv = ShippedRelay(events, cfg)
        servers.append(srv)
        return srv.port
    yield start
    for srv in servers:
        srv.close()


def test_the_shipped_relay_serves_the_operator_its_settings(store, monkeypatch, real_relay):
    port = real_relay([_doc("site_name", "poster place", 1700000000), _doc("llm_model", "qwen", 1700000001)])
    monkeypatch.setattr(S, "_port", lambda db=None: port)
    assert S.hydrate_from_db(None) == 2
    assert S.get("site_name") == "poster place" and S.is_hydrated()
    keys, ok = S._relay_setting_keys_from_db(None)
    assert ok and keys == {"site_name", "llm_model"}


def test_the_shipped_relay_answers_identities_activity(monkeypatch, real_relay):
    from app.services import nip05_registry as N
    A, D = "a" * 64, "d" * 64
    events = [ev(A, 0, 1780000000)] + [ev(A, 1, 1791400000 - i) for i in range(12)]
    events += [ev(A, 5, 1791450000 - i) for i in range(28)]
    events += [ev(D, 0, 1700000000), ev(D, 5, 1760000000)]
    events += [ev(D, 30078, 1770000000 + i, tags=[["d", "pcai:note:%d" % i]]) for i in range(10)]
    events += [ev("e" * 64, 30078, 1790000000 + i, tags=[["d", "pcai:note:%d" % i]]) for i in range(2)]
    port = real_relay(events, {"node_pubkey": OP})
    monkeypatch.setenv("POSTERCHANAI_RELAY_PORT", str(port))
    monkeypatch.setattr(N, "_operator_key", lambda: OP_SK, raising=False)
    got = N.activity([A, D, "c" * 64, "e" * 64])
    assert got[A] == {"posts": 12, "events": 40, "last_post": 1791400000, "last_event": 1791450000,
                      "first_seen": 1780000000}
    assert got[D] == {"posts": 0, "events": 11, "last_post": None, "last_event": 1770000009, "first_seen": 1700000000}
    assert got["e" * 64] == {"posts": 0, "events": 2, "last_post": None, "last_event": 1790000001,
                             "first_seen": 1790000000}, "a Notes-only member read as inactive"
    assert got["c" * 64]["events"] == 0
