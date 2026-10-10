"""#161 batch d: fedi_puppets, fedi_bridge_delivered, external_storage, api_keys, shared_files and
verification_tokens leave Postgres for operator DocTables -- run against the SHIPPED RelayServer over a real
socket with the real PosterChanDB store behind it (the harness from tests/test_doc_table.py).

What is pinned, each the reason the rule exists:
  * the one-time copy's SQL readers (scripts/nostr_migration/wave1_sources.py) produce exactly the rows these
    stores read: a node's old rows, written as the copy writes them, are served unchanged (ids, inactive keys,
    the ledger's first-row dedup and its retention window, a mount's allowed users);
  * "COULD NOT ASK" IS NEVER "NO ROWS": with the relay down an API key is neither accepted nor called
    invalid (503); a fediverse account gets NO puppet (nothing derived, nothing published); a DM is neither
    sent nor recorded (and the listener drops the subscription so the replay retries it);
  * a REVOCATION lands on the relay: a deleted key, a used verification token and a deactivated key are
    gone for a fresh process too -- and a delete the relay never confirmed is an error, not a success;
  * share links keep their expiry and access limit; a LIMITED share is not served when its count could not
    be stored;
  * puppet identity: the alias rule still reuses one person's /@alice for /users/alice, and still never on
    a third party's claim (a handle alone);
  * none of these paths opens a SQL session any more.
"""
import asyncio
import socket
from datetime import datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (APIKey, Base, ExternalStorage, FediBridgeDelivered, FediPuppet, SharedFile, User,
                        VerificationToken, external_storage_users)
from app.services import (api_key_store, doc_table, external_storage_store, fedi_tables, share_store,
                          table_migrate, verification_store)
from app.services.nostr import bech32
from app.services.relay_reader import Unavailable
from tests.test_doc_table import SK, _Relay

ALL = ("fedi_puppets", "fedi_bridge_delivered", "external_storage", "api_keys", "shared_files",
       "verification_tokens")


def _fresh_process():
    """What a second process (or this one after a restart) sees: no table held in memory."""
    doc_table.DocTable._registry.clear()


@pytest.fixture
def relay(tmp_path, monkeypatch):
    from app.services import keystore, settings_store
    monkeypatch.setattr(keystore, "get_operator_nsec", lambda: bech32.encode("nsec", SK))
    vals = {"nostr_relay_retention_days": "30"}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: vals.get(k, d))
    r = _Relay(str(tmp_path / "relay"))
    monkeypatch.setenv("POSTERCHANAI_RELAY_PORT", str(r.port))
    _fresh_process()
    try:
        yield r
    finally:
        _fresh_process()
        r.close()


@pytest.fixture
def sql():
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[User.__table__, APIKey.__table__, SharedFile.__table__,
                                             VerificationToken.__table__, ExternalStorage.__table__,
                                             external_storage_users, FediPuppet.__table__,
                                             FediBridgeDelivered.__table__])
    db = sessionmaker(bind=engine)()
    now = datetime.utcnow()
    db.add_all([User(id=1, username="alice", password_hash="x"), User(id=2, username="bob", password_hash="x")])
    db.flush()
    db.add_all([
        APIKey(id=3, user_id=1, key="sk-" + "a" * 64, name="Default", created_at=now, is_active=True),
        APIKey(id=7, user_id=2, key="sk-" + "b" * 64, name="obs-stream", created_at=now, is_active=False,
               last_used_at=now),
        SharedFile(id=4, user_id=1, token="tok-share-1", file_path="docs/a.pdf", filename="a.pdf",
                   created_at=now, expires_at=now + timedelta(days=1), access_count=2, max_accesses=5,
                   is_active=True),
        VerificationToken(id=9, user_id=2, token="tok-verify-1", created_at=now, expires_at=now + timedelta(hours=24)),
        FediPuppet(actor_uri="https://m.example/users/carol", acct="carol@m.example", instance_host="m.example",
                   pubkey_hex="c3" * 32, nip05_name="carol", created_at=now, last_seen=now),
        FediBridgeDelivered(id=1, platform="activitypub", instance_url="https://m.example", note_id="https://m.example/n/1",
                            note_uri="https://m.example/n/1", nostr_event_id="e1" * 32, nostr_pubkey="c3" * 32,
                            created_at=now),
        # a duplicate of the same note: the dedup only ever consulted the first
        FediBridgeDelivered(id=2, platform="activitypub", instance_url="https://m.example", note_id="https://m.example/n/1",
                            note_uri="https://m.example/n/1", nostr_event_id="e2" * 32, created_at=now),
        # past retention: the next daily prune would delete it -- not copied
        FediBridgeDelivered(id=3, platform="activitypub", instance_url="https://m.example", note_id="old",
                            note_uri="https://m.example/old", nostr_event_id="e3" * 32,
                            created_at=now - timedelta(days=90)),
    ])
    mount = ExternalStorage(id=5, name="Video", mount_path="/raid/video", mount_point="video", is_active=True,
                            created_at=now, updated_at=now)
    mount.allowed_users = [db.get(User, 2)]
    db.add(mount)
    db.commit()
    yield db
    db.close()


def _copy(db):
    """What scripts/migrate_tables_to_nostr.py writes for these tables: the SQL readers' rows, as documents."""
    import sys
    import pathlib
    from app.services import doc_table_bulk
    sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))
    from scripts.nostr_migration import wave1_sources as W
    got = {name: W.SOURCES[name](db) for name in ALL}
    got["api_key_index"] = W.DERIVED["api_key_index"](got)
    for name, rows in got.items():
        asyncio.run(doc_table_bulk.abulk_put(name, rows))
    return got


# ============================================================================ the copy's rows are the stores' rows

def test_every_copied_row_is_served_as_the_store_reads_it(relay, sql):
    _copy(sql)
    _fresh_process()                                  # read back as a new process would
    k = api_key_store.lookup("sk-" + "a" * 64)
    assert (k.id, k.user_id, k.name, k.is_active) == (3, 1, "Default", True)
    assert api_key_store.lookup("sk-" + "b" * 64) is None, "an inactive key stays inactive"
    assert api_key_store.get_for_user(7, 2).is_active is False
    s = asyncio.run(share_store.aactive_by_token("tok-share-1"))
    assert (s.id, s.file_path, s.access_count, s.max_accesses) == (4, "docs/a.pdf", 2, 5)
    assert verification_store.for_user(2)[0].token == "tok-verify-1"
    m = external_storage_store.get(5)
    assert (m.mount_point, m.mount_path, m.allowed_user_ids) == ("video", "/raid/video", [2])
    p = fedi_tables.puppet_by_pubkey("c3" * 32)
    assert (p.actor_uri, p.acct, p.nip05_name) == ("https://m.example/users/carol", "carol@m.example", "carol")
    led = fedi_tables.delivered_by_uri("https://m.example/n/1")
    assert led.nostr_event_id == "e1" * 32, "duplicates collapse into the FIRST row (what dedup consulted)"
    assert fedi_tables.delivered_by_uri("https://m.example/old") is None, "past retention is not carried over"
    # and SQL is untouched
    assert sql.query(APIKey).count() == 2 and sql.query(FediBridgeDelivered).count() == 3


@pytest.fixture
def down(tmp_path, monkeypatch):
    """No relay listening at all."""
    from app.services import keystore
    monkeypatch.setattr(keystore, "get_operator_nsec", lambda: bech32.encode("nsec", SK))
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    monkeypatch.setenv("POSTERCHANAI_RELAY_PORT", str(port))
    _fresh_process()
    yield
    _fresh_process()


def test_an_api_key_check_that_cannot_read_is_503_not_401(down):
    from fastapi import HTTPException
    from app.auth import get_current_user

    class Req:
        cookies, query_params = {}, {}

    class Cred:
        credentials = "sk-" + "a" * 64
    with pytest.raises(HTTPException) as e:
        get_current_user(Req(), Cred(), None)
    assert e.value.status_code == 503


def test_no_puppet_is_minted_after_a_failed_registry_read(down, monkeypatch):
    from app.services import fedi_bridge_identity as ident
    published = []

    async def publish(port, ev, timeout=8.0):
        published.append(ev)
        return True, ""
    monkeypatch.setattr(ident, "publish", publish)
    monkeypatch.setattr(ident, "_secret", lambda: b"bridge-secret-for-tests")
    derived = []
    real = ident.puppet_for
    monkeypatch.setattr(ident, "puppet_for", lambda *a, **k: derived.append(1) or real(*a, **k))
    ident._PUPPET_CACHE.clear()
    with pytest.raises(Unavailable):
        asyncio.run(ident.ensure_puppet(None, 1, {"uri": "https://m.example/users/dave", "acct": "dave@m.example",
                                                  "display_name": "Dave"}))
    assert published == [] and derived == [], "a key was derived/published without a registry answer"


def test_a_dm_is_neither_sent_nor_recorded_when_the_ledger_cannot_be_read(down, monkeypatch):
    from app.services.activitypub import config, dm
    monkeypatch.setattr(config, "enabled", lambda: True)
    monkeypatch.setattr(config, "dms", lambda: True)
    monkeypatch.setattr(config, "base_url", lambda: "https://poster.test")
    dm._puppets.update(at=0.0, set=frozenset())
    with pytest.raises(Unavailable):
        asyncio.run(dm.handle_wrap({"id": "w" * 64, "kind": 1059, "tags": [["p", "c3" * 32]], "content": ""}))
    with pytest.raises(Unavailable):
        asyncio.run(dm._done("x"))


def test_the_dm_listener_drops_its_subscription_on_an_unreadable_ledger():
    """The retry IS the replay: a reconnect re-reads three days of wraps and `_done` skips what went."""
    import inspect
    from app.services.activitypub import dm
    src = inspect.getsource(dm._listen_once)
    assert "except Unavailable:" in src and src.index("except Unavailable:") < src.index("except Exception")


def test_the_inbox_answers_503_while_the_tables_cannot_be_read():
    import inspect
    from app.routers import activitypub as routes
    src = inspect.getsource(routes._receive)
    assert "fedi_tables.aready()" in src and src.index("fedi_tables.aready()") < src.index("inbox.schedule(activity")


def test_a_delete_the_relay_never_confirmed_is_an_error_not_a_success(down):
    """nostr_store.delete_doc reads "could not ask" as "nothing to delete" and says True; DocTable must
    not drop the row on that answer, or a revoked key / used token returns on the next load."""
    t = doc_table.DocTable("api_keys")
    t._rows, t._loaded_at = {"3": {"id": 3}}, 1.0
    with pytest.raises(Unavailable):
        t.delete("3")
    assert t._rows == {"3": {"id": 3}}


# ============================================================================ revocations land

@pytest.fixture
def migrated(relay, sql):
    _copy(sql)
    return sql


def test_a_deleted_or_disabled_key_is_gone_for_a_fresh_process(migrated):
    k = api_key_store.create(1, "laptop")
    assert api_key_store.lookup(k.key).user_id == 1
    assert k.id > 7, "a new id is never one a deleted row had"
    api_key_store.set_active(3, False)
    api_key_store.delete(k.id)
    _fresh_process()
    assert api_key_store.lookup(k.key) is None
    assert api_key_store.lookup("sk-" + "a" * 64) is None
    again = api_key_store.create(1)
    assert again.id > k.id, "ids are not reused after a delete"


def test_last_used_is_written_at_most_every_few_minutes(migrated):
    k = api_key_store.lookup("sk-" + "a" * 64)
    api_key_store.touch(k)
    first = api_key_store.get_for_user(3, 1).last_used_at
    assert first
    api_key_store.touch(api_key_store.lookup("sk-" + "a" * 64))
    assert api_key_store.get_for_user(3, 1).last_used_at == first


def test_a_verification_token_works_once(migrated):
    assert verification_store.consume("tok-verify-1") == ("ok", 2)
    assert verification_store.consume("tok-verify-1") == ("invalid", None)
    _fresh_process()
    assert verification_store.consume("tok-verify-1") == ("invalid", None), "used token back after a reload"
    tok = verification_store.issue(1, hours=-1)
    assert verification_store.consume(tok) == ("expired", 1)
    assert verification_store.for_user(1) == []


def test_reissuing_replaces_the_previous_token(migrated):
    a = verification_store.issue(2)
    b = verification_store.issue(2)
    assert [r.token for r in verification_store.for_user(2)] == [b]
    assert verification_store.consume(a) == ("invalid", None)


def test_a_share_counts_its_downloads_and_stops_at_its_limit(migrated):
    async def go():
        s = await share_store.aactive_by_token("tok-share-1")
        for _ in range(3):
            assert await share_store.acount_access(s)
        assert not await share_store.acount_access(s), "the sixth download of a 5-download share"
        s = await share_store.aactive_by_token("tok-share-1")
        assert share_store.limit_reached(s) and s.access_count == 5
        assert await share_store.aactive_by_token("nope") is None
        await share_store.adeactivate(s)
        assert await share_store.aactive_by_token("tok-share-1") is None
    asyncio.run(go())


def test_a_limited_share_is_not_served_when_its_count_cannot_be_stored(migrated, monkeypatch):
    async def refused(self, k, row):
        raise Unavailable("relay did not store (test)")

    async def go():
        s = await share_store.aactive_by_token("tok-share-1")
        monkeypatch.setattr(doc_table.DocTable, "aput", refused)
        with pytest.raises(Unavailable):
            await share_store.acount_access(s)
    asyncio.run(go())


def test_an_expired_share_is_expired(migrated):
    async def go():
        s = await share_store.acreate(user_id=1, token="t2", file_path="x", filename="x",
                                      expires_at=datetime.utcnow() - timedelta(minutes=1), max_accesses=None)
        assert share_store.expired(await share_store.aactive_by_token("t2"))
        return s
    asyncio.run(go())


def test_mount_access_is_the_allowed_list(migrated):
    class U:
        def __init__(self, i):
            self.id = i
    m = asyncio.run(external_storage_store.aactive_by_mount_point("video"))
    assert external_storage_store.allows(m, U(2)) and not external_storage_store.allows(m, U(1))
    external_storage_store.forget_user(2)
    m = asyncio.run(external_storage_store.aactive_by_mount_point("video"))
    assert m.allowed_user_ids == []
    external_storage_store.save({**m, "is_active": False})
    assert asyncio.run(external_storage_store.aactive_by_mount_point("video")) is None


# ============================================================================ puppet identity

def test_the_alias_rule_survives_and_a_bare_handle_is_never_reused(migrated, monkeypatch):
    from app.services import fedi_bridge_identity as ident

    async def publish(port, ev, timeout=8.0):
        return True, ""
    monkeypatch.setattr(ident, "publish", publish)
    monkeypatch.setattr(ident, "_secret", lambda: b"bridge-secret-for-tests")
    ident._PUPPET_CACHE.clear()
    # carol is registered under /users/carol (from SQL); her profile URL is the same person -> same row
    p = asyncio.run(ident.ensure_puppet(None, 1, {"url": "https://m.example/@carol", "acct": "carol@m.example",
                                                  "username": "carol", "display_name": "carol"},
                                        profile_refresh=False))
    assert p["actor_uri"] == "https://m.example/users/carol"
    # a DIFFERENT server naming carol's handle is not carol
    q = asyncio.run(ident.ensure_puppet(None, 1, {"uri": "https://evil.example/u/carol", "acct": "carol@m.example",
                                                  "display_name": "carol"}))
    assert q["actor_uri"] == "https://evil.example/u/carol" and q["pubkey_hex"] != p["pubkey_hex"]
    _fresh_process()
    assert fedi_tables.puppet_by_uri("https://evil.example/u/carol").pubkey_hex == q["pubkey_hex"]


def test_the_ledger_records_once_and_prunes_on_retention(migrated):
    async def go():
        await fedi_tables.arecord(platform="activitypub-dm", instance_url="", note_id="k1", nostr_event_id="f" * 64)
        await fedi_tables.arecord(platform="activitypub-dm", instance_url="", note_id="k1", nostr_event_id="f" * 64)
        assert await fedi_tables.adelivered("activitypub-dm", "", "k1")
        assert len([1 for _k, r in await table_migrate.aview(fedi_tables.LEDGER)
                    if r["note_id"] == "k1"]) == 1
        k = fedi_tables.ledger_key("activitypub", "https://m.example", "https://m.example/n/1")
        row = await doc_table.DocTable(fedi_tables.LEDGER).aget(k)
        await doc_table.DocTable(fedi_tables.LEDGER).aput(k, {**row, "created_at": "2000-01-01T00:00:00"})
        assert await fedi_tables.aprune(30) == 1
        assert await fedi_tables.adelivered_by_uri("https://m.example/n/1") is None
        assert await fedi_tables.aprune(0) == 0
    asyncio.run(go())


# ============================================================================ no SQL on these paths

def test_none_of_these_paths_opens_a_sql_session(migrated, monkeypatch):
    import app.database

    def no_sql(*a, **k):
        raise AssertionError("a converted path opened a SQL session")
    monkeypatch.setattr(app.database, "SessionLocal", no_sql)
    from app.services.activitypub import dm, inbox, outbox
    assert api_key_store.lookup("sk-" + "a" * 64).user_id == 1
    assert inbox._delivered("https://m.example/n/1").nostr_event_id == "e1" * 32
    assert inbox._actor_of_puppet("c3" * 32) == "https://m.example/users/carol"
    assert asyncio.run(dm._done("nothing")) is False
    assert asyncio.run(outbox._puppet_row("c3" * 32)).acct == "carol@m.example"
    assert asyncio.run(outbox._mirrored_among(["e1" * 32, "zz"])) == {"e1" * 32}


def test_no_app_code_queries_these_models_outside_their_migration():
    """The SQL models stay (the rows are never dropped), but only the one-time copy's SQL readers
    (scripts/nostr_migration/, scripts/migrate_tables_to_nostr.py) may touch them: the app is relay-only."""
    import ast
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    models = {"FediPuppet", "FediBridgeDelivered", "ExternalStorage", "APIKey", "SharedFile", "VerificationToken"}
    bad = []
    for path in list((root / "app").rglob("*.py")) + list((root / "botframework").rglob("*.py")) + \
            list((root / "scripts").rglob("*.py")):
        if path.name == "models.py" or "nostr_migration" in path.parts or path.name == "migrate_tables_to_nostr.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        legacy_fns = {id(f) for c in ast.walk(tree) if isinstance(c, ast.ClassDef) and c.name.endswith("Legacy")
                      for f in ast.walk(c) if isinstance(f, (ast.FunctionDef, ast.AsyncFunctionDef))}
        for fn in ast.walk(tree):
            if not isinstance(fn, (ast.FunctionDef, ast.AsyncFunctionDef)) or fn.name.endswith("_from_sql") \
                    or id(fn) in legacy_fns:
                continue
            for node in ast.walk(fn):
                if isinstance(node, ast.Name) and node.id in models:
                    bad.append(f"{path.relative_to(root)}:{node.lineno} {fn.name} uses {node.id}")
        src = path.read_text(encoding="utf-8", errors="replace")
        for raw in ("from api_keys", "into api_keys", "update api_keys"):
            if raw in src.lower():
                bad.append(f"{path.relative_to(root)} raw SQL on api_keys")
    assert not bad, "\n".join(sorted(set(bad)))
