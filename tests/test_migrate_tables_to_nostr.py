"""The ONE-TIME copy of a node's old Postgres rows to the relay (#161): scripts/migrate_tables_to_nostr.py.

Driven against an in-memory SQLite database holding the legacy tables (the models the script alone may import) and
the SHIPPED relay (tests/wave2_harness.py). Pinned:

  * it copies with ids KEPT, and the relay-only app then reads exactly those rows (an account by id, by npub
    through the copied lookup index, before the table is loaded; a bot; a setting; a conversation; a wallet
    backup; an instance application);
  * it verifies row for row and refuses (exit 1, no marker) when the relay holds something else;
  * it resumes: a re-run writes only what differs and removes documents no SQL row accounts for; a third run
    writes nothing;
  * its output is counts and keys, never a row's content (password hashes, sealed seeds);
  * nothing under app/ or botframework/ imports the SQLAlchemy models any more (a static scan).
"""
import ast
import asyncio
import json
import os
from datetime import datetime

import pytest

from tests.wave2_harness import fresh_process_view, relay_mode, shared_relay  # noqa: F401

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TABLES = ["bots", "user_settings", "conversations", "exodus_wallets", "exodus_wallet_records",
          "instance_applications", "user_index", "users"]
NPUB = "npub1" + "q" * 58


def run(coro):
    return asyncio.run(coro)


def _script():
    import importlib.util
    spec = importlib.util.spec_from_file_location("migrate_tables_to_nostr",
                                                  os.path.join(ROOT, "scripts", "migrate_tables_to_nostr.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture
def legacy():
    """An in-memory SQLite database with the old tables, seeded with a node's worth of rows."""
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from app.database import Base
    from app import models as M
    from scripts.nostr_migration.wave23_sources import InstanceApplication
    eng = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(eng, tables=[M.User.__table__, M.Bot.__table__, M.UserSetting.__table__,
                                          M.Conversation.__table__, M.ExodusWallet.__table__,
                                          M.ExodusWalletRecord.__table__, InstanceApplication.__table__])
    S = sessionmaker(bind=eng, autoflush=False)
    db = S()
    db.add_all([M.User(id=1, username="owner", password_hash="$2b$secret-hash", is_admin=True, nostr_npub=NPUB,
                       telegram_chat_id="99", telegram_enabled=True, created_at=datetime(2026, 1, 2, 3, 4, 5)),
                M.User(id=7, username="bob", password_hash="$2b$other", email="b@x.y")])
    db.add(M.Bot(id=3, name="chessy", platform="nostr", modes="--chess", config='{"nostr_nsec": "nsec1x"}'))
    db.add_all([M.UserSetting(id=1, user_id=1, key="tz_name", value="Asia/Bangkok"),
                M.UserSetting(id=2, user_id=1, key="tz_name", value="Europe/Oslo")])     # duplicate: newest wins
    db.add(M.Conversation(id=12, user_id=7, title="plans"))
    db.add(M.ExodusWallet(id=1, user_id=7, seed_enc=b"\x00\x01sealed", label="main", address_index=2))
    db.add(M.ExodusWalletRecord(id=1, user_id=7, wallet_id="default", document_enc=b"\x02sealed"))
    db.add(InstanceApplication(pubkey="ef" * 32, created_at=1700000000))
    db.commit()
    db.close()
    return S


def _copy(legacy, capsys, tables=TABLES):
    code = _script().main(sum((["--table", t] for t in tables), []), session_factory=legacy)
    out = capsys.readouterr().out
    return code, [json.loads(line) for line in out.splitlines() if line.startswith("{")], out


def test_it_copies_with_ids_kept_and_the_app_reads_the_rows(relay_mode, legacy, capsys, monkeypatch):
    code, reps, out = _copy(legacy, capsys)
    assert code == 0, out
    assert {r["table"]: r["rows"] for r in reps}["users"] == 2
    assert "$2b$secret-hash" not in out and "sealed" not in out and "nsec1x" not in out
    from app.services import bot_table, conversation_table, exodus_tables, instance_welcome, user_settings_table
    from app.services import doc_table, user_table
    for name in TABLES:
        fresh_process_view(name)
    monkeypatch.setattr(doc_table.DocTable, "start_background_load", lambda self: None)
    monkeypatch.setattr(user_table, "_load_in_background", lambda: None)
    db = relay_mode.db()
    owner = run(user_table.aby_npub(db, NPUB))                    # through the copied index, nothing loaded
    assert owner.id == 1 and owner.is_admin and owner.password_hash == "$2b$secret-hash"
    assert owner.created_at == datetime(2026, 1, 2, 3, 4, 5)
    assert run(user_table.aby_telegram_chat(db, 99)).id == 1
    assert run(user_table.aget(db, 7)).email == "b@x.y"
    assert bot_table.get(db, 3).name == "chessy"
    assert user_settings_table.get(db, 1, "tz_name") == "Europe/Oslo"
    assert run(conversation_table.aget(db, 12, 7)).title == "plans"
    assert run(exodus_tables.awallet(db, 7)).seed_enc == b"\x00\x01sealed"
    assert run(exodus_tables.arecord(db, 7, "default")).document_enc == b"\x02sealed"
    assert run(instance_welcome.application(db, "ef" * 32)).created_at == 1700000000
    # a new account continues ABOVE the copied ids, never reusing one
    assert user_table.create(db, username="new", password_hash="h").id > 7


def test_it_resumes_and_a_third_run_writes_nothing(relay_mode, legacy, capsys):
    from app.services import doc_table_bulk
    from scripts.nostr_migration import wave23_sources
    rows = wave23_sources.users(legacy())
    run(doc_table_bulk.abulk_put("users", {"1": rows["1"], "555": {"username": "ghost"}}))
    code, reps, out = _copy(legacy, capsys, ["users"])
    assert code == 0, out
    assert (reps[0]["written"], reps[0]["removed_stale"]) == (1, 1)
    code, reps, out = _copy(legacy, capsys, ["users"])
    assert code == 0 and reps[0]["written"] == 0 and reps[0]["removed_stale"] == 0


def test_it_refuses_and_marks_nothing_when_the_relay_holds_something_else(relay_mode, legacy, capsys, monkeypatch):
    from app.services import doc_table_bulk
    real = doc_table_bulk.abulk_put

    async def corrupt(table, rows, **kw):
        rows = {k: (dict(v, username="tampered") if k == "7" else v) for k, v in rows.items()}
        return await real(table, rows, **kw)
    monkeypatch.setattr(doc_table_bulk, "abulk_put", corrupt)
    code, reps, out = _copy(legacy, capsys, ["users"])
    assert code == 1 and "did not verify" in out
    assert run(doc_table_bulk.amarker("users")) is None


def test_it_cannot_run_without_a_relay(legacy, capsys, monkeypatch):
    import socket
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    from tests.wave2_harness import use_relay
    use_relay(monkeypatch, port)
    code, _reps, _out = _copy(legacy, capsys, ["users"])
    assert code == 2


# =========================================================================== static: the app never imports the models
def _imports_models(path) -> list:
    tree = ast.parse(open(path, encoding="utf-8").read(), path)
    hits = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom) and node.module == "app.models":
            hits.append(node.lineno)
        elif isinstance(node, ast.ImportFrom) and node.module == "app" and any(a.name == "models" for a in node.names):
            hits.append(node.lineno)
        elif isinstance(node, ast.Import) and any(a.name == "app.models" for a in node.names):
            hits.append(node.lineno)
    return hits


def test_nothing_in_the_app_imports_the_sql_models():
    bad = []
    for top in ("app", "botframework"):
        for dirpath, _dirs, files in os.walk(os.path.join(ROOT, top)):
            for f in files:
                if f.endswith(".py"):
                    path = os.path.join(dirpath, f)
                    rel = os.path.relpath(path, ROOT)
                    if rel == "app/models.py":
                        continue
                    bad += ["%s:%d" % (rel, ln) for ln in _imports_models(path)]
    assert not bad, "runtime code imports the SQLAlchemy models (only the copy script may): %s" % bad


def test_the_static_scan_can_fail(tmp_path):
    p = tmp_path / "x.py"
    p.write_text("from app.models import User\nimport app.models\nfrom app import models\n")
    assert _imports_models(str(p)) == [1, 2, 3]
