"""Blocking a fediverse instance (or one account) removes its already-stored posts too.

"i blocked baraag.net but posts are still in posterchan": the block stopped NEW deliveries only; the 115
baraag.net accounts' 100 stored posts stayed in timelines, and "Purge now" never looked at this list.
"""
import ast
from pathlib import Path

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, FediPuppet
from app.services import fedi_blocklist

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def db():
    eng = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng, tables=[FediPuppet.__table__])
    s = sessionmaker(bind=eng)()
    rows = [("a@baraag.net", "a" * 64), ("b@media.baraag.net", "b" * 64), ("c@mastodon.social", "c" * 64),
            ("troll@mastodon.social", "d" * 64)]
    for i, (acct, pk) in enumerate(rows):
        s.add(FediPuppet(actor_uri=f"https://x/{i}", acct=acct, pubkey_hex=pk, nip05_name=f"p{i}"))
    s.commit()
    return s


def test_an_instance_line_covers_its_accounts_and_subdomains_and_an_account_line_one(db):
    got = set(fedi_blocklist.blocked_puppet_pubkeys(db, "baraag.net\ntroll@mastodon.social"))
    assert got == {"a" * 64, "b" * 64, "d" * 64}, got
    assert fedi_blocklist.blocked_puppet_pubkeys(db, "") == []


def test_saving_the_list_removes_their_stored_posts(db, monkeypatch):
    from app.routers import admin
    from app.services import settings_store
    import app.services.nostr_relay.thread as thread
    vals = {"fedi_bridge_blocked_domains": ""}
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: vals.get(k, d))
    monkeypatch.setattr(settings_store, "put", lambda k, v, **kw: vals.__setitem__(k, v))

    async def wt(_db, changes):
        return len(changes)
    monkeypatch.setattr(settings_store, "write_through", wt)
    removed = []
    monkeypatch.setattr(thread, "trigger_delete_author", lambda pks: removed.extend(pks) or {"ok": True})
    monkeypatch.setattr(thread, "trigger_block_reload", lambda: {})
    try:
        admin.update_settings(admin.SettingsUpdate(settings={"fedi_bridge_blocked_domains": "baraag.net"}), db=db, admin=None)
    except Exception:
        pass   # other save hooks may need a full app; the purge is triggered before them
    assert set(removed) == {"a" * 64, "b" * 64}, removed


def test_purge_now_includes_blocked_instances():
    src = (ROOT / "app/services/nostr_relay/thread.py").read_text()
    tree = ast.parse(src)
    fn = next(n for n in ast.walk(tree) if isinstance(n, ast.AsyncFunctionDef) and n.name == "_purge_blocks_now")
    calls = [ast.unparse(c) for c in ast.walk(fn) if isinstance(c, ast.Call)]
    assert any("_blocked_instance_puppets()" in c for c in calls), "Purge now does not include blocked instances"
