"""#161 wave 3: the ACCOUNTS (and the Exodus wallet backups) leave Postgres for relay DocTables.

Against the SHIPPED RelayServer over a real socket with the real PosterChanDB store (tests/wave2_harness.py), and
an in-memory SQLite database standing in for the legacy tables. The rules pinned, each on the code path a user
actually goes through:

  * a session survives a FRESH PROCESS with no SQL account row: sign in with a Nostr key, then a new process
    (empty memory, empty database) still knows who the bearer is;
  * "could not ask" is never "unknown user": an unreachable relay answers Unavailable (503) to a session, an API
    key's owner lookup and a Nostr sign-in -- never 401, never a fresh account minted on the strength of it;
  * a point lookup during a restart's load reads ONE document (by id, or one index document + the account),
    without loading the table, and an index entry left behind by a rename is never an answer;
  * username / npub / Telegram chat / email are unique under the writer, and a rename frees the old value;
  * two saves of the same account that change different fields keep both;
  * the migration copies (ids kept), verifies row for row, marks; resumes; refuses when SQL changed under it; and
    waits until every table that refers to users has moved;
  * no code outside the migration readers queries the converted models (a static scan).

The call-site tests import only modules that exist before this wave, so they run -- and fail -- against the old
code too (user_table is imported inside the tests that need it).
"""
import ast
import asyncio
import os
import socket
from types import SimpleNamespace

import pytest

from app.services import doc_table, doc_table_bulk, table_gate
from app.services.relay_reader import Unavailable
from tests.wave2_harness import _isolate, _reset, fresh_process_view, forget_markers, shared_relay  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WAVE3 = ("users", "user_index", "exodus_wallets", "exodus_wallet_records")
NPUB_A = "npub1" + "a" * 58


def run(coro):
    return asyncio.run(coro)


def make_sql():
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.database import Base
    from app import models as M
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[M.User.__table__, M.Conversation.__table__, M.Message.__table__,
                                          M.UserSetting.__table__, M.Bot.__table__, M.APIKey.__table__,
                                          M.VerificationToken.__table__, M.ExodusWallet.__table__,
                                          M.ExodusWalletRecord.__table__])
    return sessionmaker(autocommit=False, autoflush=False, bind=eng)


def _fixture(monkeypatch, port, *, migrated):
    factory = make_sql()
    _isolate(monkeypatch, port, factory, migrated=migrated)
    try:
        from app.services import user_table
        user_table._bg["loading"] = False
    except ImportError:
        pass
    if migrated:
        for name in WAVE3:
            doc_table_bulk._seen.add(name)
    return SimpleNamespace(port=port, Session=factory)


@pytest.fixture
def relay(shared_relay, monkeypatch):
    try:
        yield _fixture(monkeypatch, shared_relay.port, migrated=True)
    finally:
        _reset()


@pytest.fixture
def sqlmode(shared_relay, monkeypatch):
    try:
        yield _fixture(monkeypatch, shared_relay.port, migrated=False)
    finally:
        _reset()


@pytest.fixture
def down(monkeypatch):
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    try:
        yield _fixture(monkeypatch, port, migrated=True)
    finally:
        _reset()


def _request():
    from starlette.requests import Request
    return Request({"type": "http", "method": "POST", "path": "/", "headers": [], "query_string": b"",
                    "scheme": "http", "server": ("test", 80), "client": ("127.0.0.1", 1)})


def _nostr_login(monkeypatch, db, pubkey_hex):
    from fastapi import Response
    from app.routers import auth as A
    from app.services import registration_service
    monkeypatch.setattr(A, "_verify_nostr_auth", lambda *_a, **_k: True)
    monkeypatch.setattr(registration_service, "enabled", lambda: True)
    monkeypatch.setenv("POSTERCHANAI_AUTO_ADMIN", "0")
    return run(A.nostr_login(A.NostrLogin(pubkey=pubkey_hex, auth="x"), Response(), _request(), db))


def _session_user(db, token):
    from fastapi.security import HTTPAuthorizationCredentials
    from app.auth import get_current_user
    return get_current_user(_request(), HTTPAuthorizationCredentials(scheme="Bearer", credentials=token), db)


# =========================================================================== the auth path
def test_a_nostr_sign_in_is_still_a_session_in_a_fresh_process_with_no_sql_row(relay, monkeypatch):
    pk = "ab" * 32
    out = _nostr_login(monkeypatch, relay.Session(), pk)
    assert out["user"]["new"] is True
    # a NEW process: nothing in memory, and the database it is given has never seen this account
    for name in ("users", "user_index"):
        fresh_process_view(name)
    forget_markers()
    for name in WAVE3:                       # the markers themselves are on the relay in production
        doc_table_bulk._seen.add(name)
    empty = make_sql()()
    user = _session_user(empty, out["access_token"])
    assert user.id == out["user"]["id"] and user.username == out["user"]["username"]
    # and signing in again finds the SAME account -- no second one is minted
    again = _nostr_login(monkeypatch, empty, pk)
    assert again["user"]["id"] == out["user"]["id"] and again["user"]["new"] is False


def test_an_unreachable_relay_is_503_never_an_unknown_user(down):
    from fastapi import HTTPException
    from app.auth import create_access_token
    db = down.Session()
    token = create_access_token({"sub": "4242"})
    try:
        _session_user(db, token)
    except HTTPException as e:              # the old code: 401 "User not found" -- the session is logged out
        pytest.fail("a relay outage logged the session out: %s %s" % (e.status_code, e.detail))
    except Unavailable:
        pass
    else:
        pytest.fail("a session was answered without its account")
    from app.utils.auth_utils import get_user_from_api_key
    with pytest.raises(Unavailable):        # the old code: None -> "Invalid API key"
        get_user_from_api_key(db, 4242)


def test_a_sign_in_during_an_outage_never_mints_an_account(down, monkeypatch):
    from app.models import User
    db = down.Session()
    with pytest.raises(Unavailable):
        _nostr_login(monkeypatch, db, "cd" * 32)
    assert db.query(User).count() == 0     # nothing written to SQL either


def test_a_lookup_during_the_load_reads_one_document_and_ignores_stale_index_entries(relay):
    from app.services import user_table
    db = relay.Session()
    u = user_table.create(db, username="dora", password_hash="h", nostr_npub=NPUB_A,
                          telegram_chat_id="777", telegram_enabled=True, email="d@x.y")
    u.username = "dora2"
    user_table.save(db, u)
    for name in ("users", "user_index"):
        fresh_process_view(name)
    assert run(user_table.aget(db, u.id)).username == "dora2"
    assert run(user_table.aby_username(db, "dora2")).id == u.id
    assert run(user_table.aby_username(db, "dora")) is None           # the old name is free, not "dora2"
    assert run(user_table.aby_telegram_chat(db, 777)).id == u.id
    assert run(user_table.aby_npub(db, [NPUB_A, "ab" * 32])).id == u.id
    assert not doc_table.DocTable("users").loaded                      # none of that loaded the table
    assert user_table.by_email(db, "d@x.y").id == u.id                 # sync, off the loop: one document too


def test_unique_fields_are_refused_and_a_rename_frees_the_old_value(relay):
    from app.services import user_table
    db = relay.Session()
    a = user_table.create(db, username="ann", password_hash="h", nostr_npub=NPUB_A, telegram_chat_id="5",
                          email="a@x.y")
    for field, value in (("username", "ann"), ("nostr_npub", NPUB_A), ("telegram_chat_id", "5"), ("email", "a@x.y")):
        with pytest.raises(user_table.UserConflict) as e:
            user_table.create(db, password_hash="h", **dict({"username": "other-" + field}, **{field: value}))
        assert e.value.field == field
    b = user_table.create(db, username="bob", password_hash="h")
    b.telegram_chat_id = "5"
    with pytest.raises(user_table.UserConflict):
        user_table.save(db, b)
    a.username = "ann2"
    user_table.save(db, a)
    assert user_table.create(db, username="ann", password_hash="h").username == "ann"
    assert len(user_table.filter_users(db, lambda u: u.nostr_npub == NPUB_A)) == 1


def test_two_saves_of_one_account_keep_both_fields(relay):
    from app.services import user_table
    db = relay.Session()
    u = user_table.create(db, username="eve", password_hash="h")
    first, second = user_table.get(db, u.id), user_table.get(db, u.id)
    first.theme = "dark"
    second.can_ai = True
    user_table.save(db, first)
    user_table.save(db, second)
    fresh_process_view("users")
    got = user_table.get(db, u.id)
    assert got.theme == "dark" and got.can_ai is True


def test_before_the_marker_sql_answers_and_a_write_lands_in_both(sqlmode):
    from app.models import User
    from app.services import user_table
    db = sqlmode.Session()
    db.add(User(username="legacy", password_hash="h"))
    db.commit()
    assert user_table.by_username(db, "legacy") is not None             # SQL, not the (empty) relay
    u = user_table.create(db, username="fay", password_hash="h", nostr_npub=NPUB_A)
    assert db.query(User).filter_by(username="fay").one().id == u.id     # the SQL sequence gave the id
    u.theme = "matrix"
    user_table.save(db, u)
    db.expire_all()
    assert db.query(User).get(u.id).theme == "matrix"
    assert fresh_process_view("users").get(str(u.id))["theme"] == "matrix"


# =========================================================================== migration
def _seed(factory, n=3):
    from app.models import User
    db = factory()
    for i in range(n):
        db.add(User(username="u%d" % i, password_hash="hash%d" % i, nostr_npub="npub1x%d" % i,
                    telegram_chat_id=str(100 + i) if i else None))
    db.commit()
    ids = [u.id for u in db.query(User).order_by(User.id).all()]
    db.close()
    return ids


def _mark_dependents():
    from app.services import wave3_migration
    for name in wave3_migration.dependents():
        run(doc_table.DocTable(doc_table_bulk.MARKER_TABLE).aput(name, {"verified": True}))


def test_the_migration_copies_verifies_marks_and_keeps_ids(sqlmode):
    from app.services import user_table, wave3_migration
    ids = _seed(sqlmode.Session)
    with pytest.raises(wave3_migration.NotYet):          # conversations, api keys ... have not moved yet
        run(wave3_migration.migrate_users(sqlmode.Session))
    assert run(doc_table_bulk.amarker("users")) is None
    _mark_dependents()
    rep = run(wave3_migration.migrate_users(sqlmode.Session))
    assert rep["verified"] and rep["rows"] == 3
    forget_markers()
    fresh_process_view("users")
    assert run(table_gate.arelay_mode("users"))
    db = make_sql()()                                    # SQL is not read any more
    assert [u.id for u in user_table.all_users(db)] == ids
    assert user_table.by_telegram_chat(db, "101", enabled_only=False).id == ids[1]
    assert user_table.get(db, ids[2]).password_hash == "hash2"
    assert run(wave3_migration.migrate_users(sqlmode.Session))["skipped"]       # idempotent


def test_the_migration_resumes_and_removes_stale_documents(sqlmode):
    from app.services import user_table, wave3_migration
    ids = _seed(sqlmode.Session)
    _mark_dependents()
    rows = user_table.sql_rows(sqlmode.Session())
    run(doc_table_bulk.abulk_put("users", {str(ids[0]): rows[str(ids[0])], "999999": {"username": "ghost"}}))
    rep = run(wave3_migration.migrate_users(sqlmode.Session))
    assert rep["verified"] and rep["written"] == 2 and rep["removed_stale"] == 1


def test_the_migration_refuses_when_sql_changed_during_the_copy(sqlmode, monkeypatch):
    from app.models import User
    from app.services import wave3_migration
    _seed(sqlmode.Session)
    _mark_dependents()
    real = doc_table_bulk.abulk_put

    async def write_then_change(table, rows, **kw):
        n = await real(table, rows, **kw)
        if table == "users":                             # a sign-up lands while the accounts are being copied
            db = sqlmode.Session()
            db.add(User(username="late", password_hash="h"))
            db.commit()
            db.close()
        return n
    monkeypatch.setattr(doc_table_bulk, "abulk_put", write_then_change)
    with pytest.raises(doc_table_bulk.MigrationMismatch):
        run(wave3_migration.migrate_users(sqlmode.Session))
    assert run(doc_table_bulk.amarker("users")) is None
    monkeypatch.setattr(doc_table_bulk, "abulk_put", real)
    assert run(wave3_migration.migrate_users(sqlmode.Session))["rows"] == 4     # the next pass takes it


# =========================================================================== exodus wallet backups
def test_a_wallet_backup_is_inserted_once_and_read_back_strictly(relay):
    from app.services import exodus_tables
    db = relay.Session()
    run(exodus_tables.ainsert_record(db, 7, "default", b"\x01\x02"))
    with pytest.raises(exodus_tables.Exists):
        run(exodus_tables.ainsert_record(db, 7, "default", b"\x09"))     # never replaces a seed
    fresh_process_view(exodus_tables.RECORDS)
    assert run(exodus_tables.arecord(db, 7, "default")).document_enc == b"\x01\x02"
    run(exodus_tables.aput_wallet(db, 7, seed_enc=b"\xaa", label="L", address_index=2, backed_up_at=None))
    fresh_process_view(exodus_tables.WALLETS)
    w = run(exodus_tables.awallet(db, 7))
    assert w.seed_enc == b"\xaa" and w.address_index == 2


def test_an_unreadable_wallet_backup_is_never_no_backup(down):
    from app.services import exodus_tables
    with pytest.raises(Unavailable):
        run(exodus_tables.awallet(down.Session(), 7))


def test_an_instance_application_lives_on_the_relay_once(relay):
    from app.services import instance_welcome as W
    db = relay.Session()
    row, created = run(W.apply(db, "ef" * 32))
    assert created
    fresh_process_view(W.TABLE)
    row2, created2 = run(W.apply(db, "ef" * 32))
    assert not created2 and row2.created_at == row.created_at
    assert run(W._claim(db, "ef" * 32, "admin_retry_at", 1000)) is True
    assert run(W._claim(db, "ef" * 32, "admin_retry_at", 1000)) is False     # claimed: no second DM


# =========================================================================== static: nobody else queries them
CONVERTED = {"User", "ExodusWallet", "ExodusWalletRecord", "InstanceApplication"}
# file -> why it may still name a converted model in a query (the SQL side of a gated store, or a migration read)
ALLOWED = {
    "app/services/user_table.py": "the SQL side, read/written only until the users marker",
    "app/services/users_store.py": "the legacy SQL hydrate (_apply), only before the users marker",
    "app/services/settings_store.py": "bootstrap: the operator key once lived in the admin's SQL row",
    "app/services/exodus_tables.py": "the SQL side, only until the exodus markers",
    "app/services/instance_welcome.py": "the SQL side, only until the instance_applications marker",
    "app/services/wave2_migration.py": "the read-only messages census",
    "app/services/wave3_migration.py": "the migration reader",
}


def _queries(path):
    tree = ast.parse(open(path, encoding="utf-8").read(), path)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("query", "get"):
            for a in node.args:
                name = a.id if isinstance(a, ast.Name) else a.attr if isinstance(a, ast.Attribute) else None
                if name in CONVERTED or (isinstance(a, ast.Attribute) and isinstance(a.value, ast.Name)
                                         and a.attr in {"id"} and a.value.id in CONVERTED):
                    hits.append(node.lineno)
        if isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute) and node.func.attr in ("join", "outerjoin"):
            if any(isinstance(a, ast.Name) and a.id in CONVERTED for a in node.args):
                hits.append(node.lineno)
    return hits


def test_no_code_outside_the_migration_readers_queries_a_converted_model():
    bad = []
    for top in ("app", "botframework"):
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, top)):
            for f in files:
                if f.endswith(".py"):
                    path = os.path.join(dirpath, f)
                    rel = os.path.relpath(path, ROOT)
                    if rel in ALLOWED:
                        continue
                    bad += ["%s:%d" % (rel, ln) for ln in _queries(path)]
    assert not bad, "converted models queried outside the migration readers: %s" % bad


def test_the_static_scan_can_fail():
    import tempfile
    with tempfile.NamedTemporaryFile("w", suffix=".py", delete=False) as fh:
        fh.write("def f(db):\n    return db.query(User).filter(User.id == 1).first() or db.get(User, 1)\n")
    try:
        assert _queries(fh.name) == [2, 2]
    finally:
        os.unlink(fh.name)
