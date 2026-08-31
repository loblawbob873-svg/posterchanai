"""THE RELAY IS AUTHORITATIVE FOR BOT CONFIG, AND THE WRITE PATH FAILS QUIETLY BY DESIGN.

`bots_store.py` had ZERO test references. The relay holds each bot's config
(`pcai:bot:<name>`, operator-signed) and the SQLite `bots` table is a read-through cache that
`hydrate()` refills at startup. That ordering is what makes a silent write failure expensive: a
config that never reached the relay looks fine until the next restart, when hydrate refills the
cache from the relay and the change is simply gone. Nothing errors — the admin panel showed a save,
and the bot is running the old config.

Every write path here swallows its exceptions and returns False or None, which is right (a relay
hiccup must not fail the admin's save) and is exactly why the layer needs its own tests: the callers
cannot report anything.

THE ONE STRUCTURAL TRAP. `sync_bot_blocking` drives its coroutine with a bare `asyncio.run`, which
raises `RuntimeError` inside a running event loop. Its sibling `record_store._run_blocking` handles
both worlds; this one does not, and does not need to — every caller today is a synchronous `def`
route, which FastAPI runs in a threadpool with no loop. That is a property of the CALLERS, so
`test_every_blocking_caller_is_a_synchronous_route` is what keeps it true. Turn one of those routes
into `async def` and the write raises, the wrapper logs a warning, and bot config silently stops
reaching the relay.
"""
import asyncio
import pathlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Base, Bot
from app.services import bots_store as bs
from app.services import nostr_store as store


SK = b"\x01" * 32


class FakeRelay:
    def __init__(self, docs=None, put_ok=True, read_error=None, write_error=None):
        self.docs = dict(docs or {})
        self.put_ok = put_ok
        self.read_error = read_error
        self.write_error = write_error
        self.puts = []
        self.deletes = []

    async def put_doc(self, port, sk, d_tag, data, **kw):
        if self.write_error:
            raise self.write_error
        self.puts.append((d_tag, data))
        return self.put_ok

    async def delete_doc(self, port, sk, d_tag, **kw):
        if self.write_error:
            raise self.write_error
        self.deletes.append(d_tag)
        return True

    async def list_docs(self, port, prefix, *, seckey=None, **kw):
        if self.read_error:
            raise self.read_error
        return {k: v for k, v in self.docs.items() if k.startswith(prefix)}


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine, tables=[Bot.__table__])
    session = sessionmaker(bind=engine)()
    yield session
    session.close()


@pytest.fixture
def relay(monkeypatch):
    def _install(**kw):
        f = FakeRelay(**kw)
        monkeypatch.setattr(bs.store, "put_doc", f.put_doc)
        monkeypatch.setattr(bs.store, "delete_doc", f.delete_doc)
        monkeypatch.setattr(bs.store, "list_docs", f.list_docs)
        monkeypatch.setattr(bs._ss, "_operator_seckey", lambda db: SK)
        monkeypatch.setattr(bs._ss, "_port", lambda db=None: 3052)
        return f
    return _install


def mkbot(**kw):
    base = dict(name="alice", enabled=True, bot_type="nostr", platform="nostr",
                host="server1", modes="listen", config='{"use_app_service": true}')
    base.update(kw)
    return Bot(**base)


def run(coro):
    return asyncio.run(coro)


# --------------------------------------------------------------------------- write-through


def test_a_bot_is_written_under_its_namespaced_name(relay, db):
    f = relay()
    assert run(bs.sync_bot(db, mkbot())) is True
    assert f.puts[0][0] == store.NS_BOT + "alice"


def test_every_declared_field_is_written(relay, db):
    """`BOT_FIELDS` is the contract between the writer and `_apply`. A field dropped here is a
    field that reverts on the next hydrate — silently, and only after a restart."""
    f = relay()
    run(bs.sync_bot(db, mkbot()))
    assert set(f.puts[0][1]) == set(bs.BOT_FIELDS)


def test_the_config_blob_travels_intact(relay, db):
    """`config` is a TEXT column holding JSON, and this layer never parses it. That is what makes
    an operator's leftover key — or a key from an older build — survive a save: nothing here can
    drop what it does not look at. Re-serialising it would also reorder and reformat the blob on
    every write, which turns every startup hydrate into a spurious "changed"."""
    cfg = '{"use_app_service": true, "unknown_legacy_key": [1, 2, {"deep": "v"}], "n": 0}'
    f = relay()
    run(bs.sync_bot(db, mkbot(config=cfg)))
    assert f.puts[0][1]["config"] == cfg


def test_no_operator_key_writes_nothing(relay, db, monkeypatch):
    """A node that has not minted its operator key cannot sign the doc. Attempting it would raise
    on every bot save."""
    f = relay()
    monkeypatch.setattr(bs._ss, "_operator_seckey", lambda db: None)
    assert run(bs.sync_bot(db, mkbot())) is False
    assert f.puts == []


def test_a_bot_with_no_name_writes_nothing(relay, db):
    """The name IS the document key — an empty one would write to `pcai:bot:` and collide with the
    next nameless bot."""
    f = relay()
    assert run(bs.sync_bot(db, mkbot(name=""))) is False
    assert run(bs.sync_bot(db, None)) is False
    assert f.puts == []


def test_a_relay_refusal_is_reported_as_false(relay, db):
    f = relay(put_ok=False)
    assert run(bs.sync_bot(db, mkbot())) is False


def test_a_relay_exception_is_swallowed_and_reported_as_false(relay, db):
    """The admin's save must not fail because the relay hiccuped — but it must not report success
    either, because the relay is what the next hydrate reads."""
    relay(write_error=OSError("relay unreachable"))
    assert run(bs.sync_bot(db, mkbot())) is False


def test_deleting_removes_the_namespaced_doc(relay, db):
    f = relay()
    assert run(bs.delete_bot(db, "alice")) is True
    assert f.deletes == [store.NS_BOT + "alice"]


def test_deleting_without_a_name_does_nothing(relay, db):
    """A blank name here would delete `pcai:bot:` — and on a rename this is called with the OLD
    name, which is exactly where an empty value can arrive."""
    f = relay()
    assert run(bs.delete_bot(db, "")) is False
    assert run(bs.delete_bot(db, None)) is False
    assert f.deletes == []


# --------------------------------------------------------------------------- hydrate


def test_hydrate_creates_a_missing_bot(relay, db):
    relay(docs={store.NS_BOT + "alice": {"name": "alice", "enabled": True, "host": "server1"}})
    assert run(bs.hydrate(db)) == 1
    row = db.query(Bot).filter(Bot.name == "alice").first()
    assert row is not None and row.host == "server1"


def test_hydrate_updates_an_existing_bot(relay, db):
    db.add(mkbot(host="old-host"))
    db.commit()
    relay(docs={store.NS_BOT + "alice": {"name": "alice", "host": "new-host"}})
    assert run(bs.hydrate(db)) == 1
    assert db.query(Bot).filter(Bot.name == "alice").first().host == "new-host"


def test_hydrate_reports_no_change_when_nothing_moved(relay, db):
    """The count drives a `db.commit()` and the log line. Reporting a change every startup would
    make a real one invisible."""
    db.add(mkbot(host="server1"))
    db.commit()
    relay(docs={store.NS_BOT + "alice": {"name": "alice", "host": "server1"}})
    assert run(bs.hydrate(db)) == 0


def test_a_partial_record_does_not_blank_the_fields_it_omits(relay, db):
    """`_apply` only assigns fields PRESENT in the record. A doc written by an older build carries
    fewer keys, and treating absent as empty would wipe a bot's config on the next startup."""
    db.add(mkbot(host="server1", modes="listen", config='{"k": "v"}'))
    db.commit()
    relay(docs={store.NS_BOT + "alice": {"name": "alice", "enabled": False}})
    run(bs.hydrate(db))
    row = db.query(Bot).filter(Bot.name == "alice").first()
    assert row.enabled is False
    assert row.host == "server1" and row.modes == "listen" and row.config == '{"k": "v"}'


def test_hydrate_never_deletes_a_local_bot(relay, db):
    """It is an UPSERT. A relay that answers with fewer bots than the cache holds must not be read
    as "these were removed" — the same short-list-is-a-delete-order rule the folder sync learned."""
    db.add(mkbot(name="alice"))
    db.add(mkbot(name="bob"))
    db.commit()
    relay(docs={store.NS_BOT + "alice": {"name": "alice"}})
    run(bs.hydrate(db))
    assert {b.name for b in db.query(Bot).all()} == {"alice", "bob"}


def test_an_unreadable_relay_is_survivable(relay, db):
    """It returns 0 and does not raise — hydrate runs at startup, and an exception here would take
    the boot with it.

    Deliberately NOT claimed as a wipe guard. Measured: making the failed read fall through as an
    empty document set changes nothing observable, because hydrate is UPSERT-only and an empty set
    simply means "nothing to apply". What actually protects the cache is that it never deletes at
    all — `test_hydrate_never_deletes_a_local_bot` is that guard, and this one would be taking
    credit for it."""
    db.add(mkbot())
    db.commit()
    relay(read_error=OSError("relay unreachable"))
    assert run(bs.hydrate(db)) == 0
    assert db.query(Bot).count() == 1


def test_no_operator_key_hydrates_nothing(relay, db, monkeypatch):
    monkeypatch.setattr(bs._ss, "_operator_seckey", lambda db: None)
    assert run(bs.hydrate(db)) == 0


def test_a_wrapped_record_is_unwrapped(relay, db):
    """Some docs store the record under `value`. Both shapes are live, and reading only one leaves
    those bots permanently un-hydrated."""
    relay(docs={store.NS_BOT + "alice": {"value": {"name": "alice", "host": "wrapped"}}})
    assert run(bs.hydrate(db)) == 1
    assert db.query(Bot).filter(Bot.name == "alice").first().host == "wrapped"


@pytest.mark.parametrize("junk", ["a string", 7, None, [], {"no_name": True}, {"name": ""}])
def test_a_malformed_record_is_skipped_rather_than_creating_a_junk_bot(relay, db, junk):
    """One bad doc must not create a nameless Bot row, and must not stop the others — the bot
    manager reconciles from this table and would try to spawn it."""
    relay(docs={store.NS_BOT + "bad": junk,
                store.NS_BOT + "alice": {"name": "alice", "host": "server1"}})
    assert run(bs.hydrate(db)) == 1
    assert {b.name for b in db.query(Bot).all()} == {"alice"}


def test_hydrate_only_reads_bot_documents(relay, db):
    """The operator key signs every app doc. A prefix that leaked would hand `_apply` a setting."""
    relay(docs={store.NS_BOT + "alice": {"name": "alice"},
                "pcai:setting:llm_model": {"value": "qwen"},
                "pcai:kv:uptime": {"monitors": {}}})
    assert run(bs.hydrate(db)) == 1
    assert {b.name for b in db.query(Bot).all()} == {"alice"}


def test_a_bot_survives_a_full_round_trip(relay, db):
    """Writer and reader agree, which is the only thing that makes the cache safe to rebuild."""
    f = relay()
    cfg = '{"use_app_service": true, "leftover": "keep me"}'
    run(bs.sync_bot(db, mkbot(config=cfg, host="nas")))
    d_tag, record = f.puts[0]

    f2 = relay(docs={d_tag: record})
    run(bs.hydrate(db))
    row = db.query(Bot).filter(Bot.name == "alice").first()
    assert row.host == "nas" and row.config == cfg and row.modes == "listen"


# --------------------------------------------------------------------------- the blocking wrappers


def test_the_blocking_wrapper_actually_writes(relay, db):
    f = relay()
    bs.sync_bot_blocking(db, mkbot())
    assert f.puts and f.puts[0][0] == store.NS_BOT + "alice"


def test_the_blocking_wrapper_never_raises_into_the_admin_route(relay, db):
    """A failed relay write must not turn the admin's save into a 500."""
    relay(write_error=OSError("relay unreachable"))
    bs.sync_bot_blocking(db, mkbot())      # must not raise
    bs.delete_bot_blocking(db, "alice")


def test_every_blocking_caller_is_a_synchronous_route():
    """THE STRUCTURAL GUARD, and the reason it is worth writing down.

    `sync_bot_blocking` / `delete_bot_blocking` drive their coroutines with a bare `asyncio.run`,
    which raises `RuntimeError: cannot be called from a running event loop`. Every caller today is
    a synchronous `def` route — FastAPI runs those in a threadpool where there is no loop — so it
    works. `record_store._run_blocking` handles both worlds; this one does not.

    Make one of these routes `async def` and the call raises, the wrapper catches it and logs a
    warning, and bot config silently stops reaching the relay. The relay is authoritative, so the
    admin sees a successful save and the setting reverts at the next restart."""
    import ast
    import pathlib
    root = pathlib.Path(bs.__file__).resolve().parents[2]
    offenders = []
    for path in sorted((root / "app").rglob("*.py")):
        if path.name == "bots_store.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        for fn in ast.walk(tree):
            if not isinstance(fn, ast.AsyncFunctionDef):
                continue
            body = ast.unparse(fn)
            for call in ("sync_bot_blocking", "delete_bot_blocking"):
                if call in body:
                    offenders.append(f"{path.relative_to(root)}: async def {fn.name} calls {call}")
    assert offenders == [], (
        "a bare asyncio.run inside a running event loop raises, and the wrapper swallows it — "
        "these would stop writing bot config to the relay, silently:\n" + "\n".join(offenders))


def test_the_wrappers_still_use_asyncio_run_so_the_guard_above_means_something():
    """If they ever gain `record_store`'s both-worlds driver, the sweep above stops being a real
    constraint and should be replaced rather than left looking like protection."""
    src = pathlib.Path(bs.__file__).read_text(encoding="utf-8")
    assert "asyncio.run(" in src

