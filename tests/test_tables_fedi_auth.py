"""#161 batch d: fedi_puppets, fedi_bridge_delivered, external_storage, api_keys, shared_files and
verification_tokens leave Postgres for operator DocTables -- run against the SHIPPED RelayServer over a real
socket with the real PosterChanDB store behind it (the harness from tests/test_doc_table.py).

What is pinned, each the reason the rule exists:
  * MIGRATION copies every SQL row, verifies by a STRICT re-read (count and content), writes the marker, is
    idempotent, and REFUSES -- no marker -- when the relay's copy differs; until then the table is
    Unavailable, never empty (an unmigrated api_keys read as "no keys" is a 401 for every real key);
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
import json
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
    table_migrate._ready.clear()


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


async def _relay_has(k, row, table="api_keys"):
    """Put a document on the relay directly (what an earlier, interrupted copy left there) -- not through the
    table, which before its marker writes SQL first."""
    from app.services import doc_table_bulk
    await doc_table_bulk.abulk_put(table, {k: row})


def _migrate(db, name):
    from app.services import fedi_auth_tables
    return asyncio.run(fedi_auth_tables.migrate_one(db, name))


# ============================================================================ migration

def test_an_unmigrated_table_is_unavailable_not_empty(relay):
    with pytest.raises(Unavailable):
        api_key_store.lookup("sk-" + "a" * 64)
    with pytest.raises(Unavailable):
        fedi_tables.puppet_by_uri("https://m.example/users/carol")


def test_every_table_is_copied_verified_and_marked(relay, sql):
    for name in ALL:
        res = _migrate(sql, name)
        assert res["status"] == "migrated", res
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
    marks = doc_table.DocTable(table_migrate.MARKERS).all()
    assert {n: marks[n]["rows"] for n in ALL} == {"fedi_puppets": 1, "fedi_bridge_delivered": 1,
                                                 "external_storage": 1, "api_keys": 2, "shared_files": 1,
                                                 "verification_tokens": 1}
    # and SQL is untouched
    assert sql.query(APIKey).count() == 2 and sql.query(FediBridgeDelivered).count() == 3


def test_migration_is_idempotent(relay, sql):
    assert _migrate(sql, "api_keys")["status"] == "migrated"
    assert _migrate(sql, "api_keys")["status"] == "already"
    _fresh_process()
    assert _migrate(sql, "api_keys")["status"] == "already"
    assert len(api_key_store.for_user(1)) == 1


def test_an_interrupted_migration_resumes(relay, sql):
    """Half the rows already on the relay and no marker (the process died mid-copy): the rest are copied."""
    rows = api_key_store.rows_from_sql(sql)
    asyncio.run(_relay_has("3", rows["3"]))
    res = _migrate(sql, "api_keys")
    assert (res["status"], res["copied"], res["rows"]) == ("migrated", 1, 2)


def test_a_relay_copy_that_differs_is_replaced_by_sql_s(relay, sql):
    """#161 wave 1: until the marker exists SQL is the store of record (the app reads and writes it), so a relay
    copy that differs -- a write that reached SQL while the relay half failed -- is overwritten with SQL's row."""
    rows = api_key_store.rows_from_sql(sql)
    asyncio.run(_relay_has("3", {**rows["3"], "is_active": False}))
    res = _migrate(sql, "api_keys")
    assert res["status"] == "migrated"
    _fresh_process()
    assert api_key_store.lookup("sk-" + "a" * 64).id == 3, "the stale relay copy won over SQL"


def test_a_relay_that_keeps_holding_something_else_is_refused_and_not_marked(relay, sql, monkeypatch):
    from app.services import doc_table_bulk
    real_put = doc_table_bulk.abulk_put

    async def lossy(table, rows, **kw):
        if table == "api_keys" and "3" in rows:
            rows = dict(rows, **{"3": {**rows["3"], "is_active": False}})
        return await real_put(table, rows, **kw)
    monkeypatch.setattr(doc_table_bulk, "abulk_put", lossy)
    with pytest.raises(table_migrate.MigrationMismatch) as e:
        _migrate(sql, "api_keys")
    assert "sk-" not in str(e.value), "a refusal must not print key material"
    _fresh_process()
    assert doc_table.DocTable(table_migrate.MARKERS).get("api_keys") is None
    with pytest.raises(Unavailable):
        api_key_store.lookup("sk-" + "a" * 64)       # not marked, and this process has no SQL session: unknown


def test_an_unexpected_extra_row_is_removed(relay, sql):
    asyncio.run(_relay_has("99", {"id": 99, "user_id": 1, "key": "sk-x", "is_active": True}))
    assert _migrate(sql, "api_keys")["status"] == "migrated"
    _fresh_process()
    assert api_key_store.lookup("sk-x") is None, "a row SQL never had survived the copy"


def test_the_script_migrates_one_table(relay, sql, monkeypatch, capsys):
    import importlib.util
    import pathlib
    spec = importlib.util.spec_from_file_location(
        "mig", pathlib.Path(__file__).resolve().parents[1] / "scripts" / "migrate_tables_to_nostr.py")
    mig = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mig)
    import app.database
    monkeypatch.setattr(app.database, "SessionLocal", lambda: sql)
    monkeypatch.setattr(sql, "close", lambda: None)
    from app.services import settings_store
    monkeypatch.setattr(settings_store, "hydrate_from_db", lambda db: 0)
    assert mig.main(["--table", "shared_files"], session_factory=lambda: sql) == 0
    out = capsys.readouterr().out
    assert json.loads(out.strip())["status"] == "migrated" and "tok-share-1" not in out


# ============================================================================ could not ask

@pytest.fixture
def down(tmp_path, monkeypatch):
    """No relay listening at all."""
    from app.services import keystore
    monkeypatch.setattr(keystore, "get_operator_nsec", lambda: bech32.encode("nsec", SK))
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    monkeypatch.setenv("POSTERCHANAI_RELAY_PORT", str(port))
    _fresh_process()
    table_migrate._ready.update(ALL)       # even a table known to be migrated: the READ is what fails
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
    for name in ALL:
        _migrate(sql, name)
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
    """The SQL models stay (the rows are never dropped), but only the `*_from_sql` migration readers -- and the
    `*Legacy` classes that read and write SQL until a table's marker exists (#161 wave 1) -- may touch them."""
    import ast
    import pathlib
    root = pathlib.Path(__file__).resolve().parents[1]
    models = {"FediPuppet", "FediBridgeDelivered", "ExternalStorage", "APIKey", "SharedFile", "VerificationToken"}
    bad = []
    for path in list((root / "app").rglob("*.py")) + list((root / "botframework").rglob("*.py")) + \
            list((root / "scripts").rglob("*.py")):
        if path.name == "models.py":
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
