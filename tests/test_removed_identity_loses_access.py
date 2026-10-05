"""Removing an identity takes away the permissions it carried — the Remove button.

Run: venv-unified/bin/python -m pytest tests/test_removed_identity_loses_access.py

Asked for: "make sure that REMOVE ALL NOT IN PROFILE removes their permissions too" (that bulk action is
gone now -- a name IS membership, whatever the profile shows -- and Remove is the one way out). Taking a name off
the registry ends the NIP-05 entitlement at once, but grants WRITTEN ONTO THE ACCOUNT — the can_*
columns and the shared `blossom_whitelist` — outlived it until the access policy's next run, and that
policy is off unless an operator enables it. So a removed identity kept AI, Blossom and streaming.

Driven through the real admin endpoints, the real registry parser and a real (sqlite) accounts
table; only the relay write-through is stubbed, so its failure can be forced. Each rule:

  revoked       every per-account permission goes off and the account is marked revoked
  whitelist     the key's line leaves blossom_whitelist; every other line stays VERBATIM
  still member  a key that still holds another name here is not touched
  infra         an admin / bot / GPU peer is never revoked, even if its name was removed
  durable       the account is written through to the relay before it is committed; a failed
                write leaves the account unchanged and the endpoint SAYS the revoke failed
  scoped        an identity nobody removed keeps everything, whatever its profile shows
"""
import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Bot, User
from app.services import (blossom_service, nip05_registry, relay_access_policy, relay_blocklist,
                          settings_store, users_store)
from app.services.nostr import bip340, nostr_service

PK = {n: bip340.pubkey_from_seckey(bytes([i + 1]) * 32).hex() for i, n in enumerate(("alice", "ghost", "twice", "boss", "other"))}
FIELDS = relay_access_policy.REVOKE_FIELDS


def run(c):
    return asyncio.run(c)


@pytest.fixture
def world(monkeypatch):
    engine = create_engine("sqlite://")
    User.__table__.create(engine)
    Bot.__table__.create(engine)
    db = sessionmaker(bind=engine)()
    for name, pk in PK.items():
        u = User(username=name, email=f"{name}@x.example", password_hash="x", nostr_npub=nostr_service.npub_of(pk),
                 is_admin=(name == "boss"), access_revoked=False)
        for f in FIELDS:
            setattr(u, f, True)
        db.add(u)
    db.commit()

    vals = {
        nip05_registry.KEY: "\n".join([
            "# names", f"alice {PK['alice']}", f"ghost {PK['ghost']}",
            f"twice {PK['twice']}", f"twice2 {PK['twice']}", f"boss {PK['boss']}"]),
        "blossom_whitelist": "\n".join([nostr_service.npub_of(PK["ghost"]), PK["other"],
                                        nostr_service.npub_of(PK["alice"])]),
    }
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: vals.get(k, d))
    monkeypatch.setattr(settings_store, "put", lambda k, v, **kw: vals.__setitem__(k, v))
    monkeypatch.setattr(settings_store, "is_hydrated", lambda: True)
    writes = {"ok": True, "docs": []}

    async def write_through(db_, changes):
        writes["docs"].append(dict(changes))
        return len(changes) if writes["ok"] else 0
    monkeypatch.setattr(settings_store, "write_through", write_through)

    async def sync_user(db_, u, force=False):
        writes["docs"].append({"user": u.username, "revoked": u.access_revoked})
        return writes["ok"]
    monkeypatch.setattr(users_store, "sync_user", sync_user)
    monkeypatch.setattr(blossom_service, "invalidate_operator_cache", lambda: None)
    from app.services import nostr_dvm
    monkeypatch.setattr(nostr_dvm, "peer_pubkeys", lambda: set())
    import app.services.nostr_relay.thread as thread
    monkeypatch.setattr(thread, "trigger_nip05_reload", lambda: {})

    # alice publishes her address; ghost, twice and boss show none -- which decides nothing now.
    async def profiles(pks):
        return {PK["alice"]: {"name": "A", "nip05": "alice@poster.place"}}, True
    monkeypatch.setattr(relay_blocklist, "profiles", profiles)
    from app.routers import client
    monkeypatch.setattr(client, "_nip05_domain", lambda request, db_: "poster.place")
    yield db, vals, writes
    db.close()


def user(db, name):
    db.expire_all()
    return db.query(User).filter(User.username == name).one()


def granted(u):
    return {f: getattr(u, f) for f in FIELDS}


def remove(db, name):
    from app.routers import admin
    return run(admin.relay_identity_remove(admin.RelayIdentityRemoveReq(name=name), db=db, admin=None))


def test_removing_a_name_revokes_their_permissions(world):
    db, vals, writes = world
    r = remove(db, "ghost")
    assert r["ok"] and r["revoked"]["accounts"] == 1, r
    g = user(db, "ghost")
    assert not any(granted(g).values()) and g.access_revoked, granted(g)
    assert {"user": "ghost", "revoked": True} in writes["docs"], "the revoke never reached the relay"
    # Only ghost's whitelist line went; the others are byte-for-byte what they were.
    assert vals["blossom_whitelist"] == "\n".join([PK["other"], nostr_service.npub_of(PK["alice"])])
    assert r["revoked"]["whitelist"] == 1


def test_a_member_nobody_removed_keeps_everything(world):
    db, vals, _ = world
    remove(db, "ghost")
    a = user(db, "alice")
    assert all(granted(a).values()) and not a.access_revoked
    assert nostr_service.npub_of(PK["alice"]) in vals["blossom_whitelist"]


def test_a_key_that_still_holds_another_name_is_still_a_member(world):
    """`twice` holds two names; removing one leaves them a member through the other."""
    db, _, _ = world
    from app.routers import admin
    r = run(admin.relay_identity_remove(admin.RelayIdentityRemoveReq(name="twice"), db=db, admin=None))
    assert r["orphaned"] == [] and r["revoked"]["accounts"] == 0
    assert all(granted(user(db, "twice")).values())
    r = run(admin.relay_identity_remove(admin.RelayIdentityRemoveReq(name="twice2"), db=db, admin=None))
    assert r["orphaned"] == [PK["twice"]] and r["revoked"]["accounts"] == 1
    assert not any(granted(user(db, "twice")).values())


def test_an_admin_is_never_revoked(world):
    db, _, _ = world
    r = remove(db, "boss")
    assert r["ok"], "the name itself may go"
    assert PK["boss"] in r["revoked"]["protected"]
    b = user(db, "boss")
    assert all(granted(b).values()) and not b.access_revoked, "an admin lost their own permissions"


def test_a_failed_write_leaves_the_account_as_it_was_and_says_so(world):
    db, vals, writes = world
    writes["ok"] = False
    r = remove(db, "ghost")
    assert r["ok"], "the name is removed either way"
    assert r.get("revoke_error"), "a revoke that did not happen was reported as done"
    g = user(db, "ghost")
    assert all(granted(g).values()) and not g.access_revoked, "half-revoked account left behind"


def test_the_page_says_what_was_revoked():
    import json
    import subprocess
    from pathlib import Path
    js = Path(__file__).resolve().parents[1] / "static/js/admin-identities.js"
    out = subprocess.run(["node", "-e", """const {revokedText}=require(process.argv[1]);
      process.stdout.write(JSON.stringify([revokedText({revoked:{accounts:2,whitelist:1}}),
        revokedText({revoke_error:'x'}), revokedText({revoked:{accounts:0,whitelist:0}})]))""", str(js)],
                         capture_output=True, text=True, timeout=20)
    a, b, c = json.loads(out.stdout)
    assert "2 account" in a and "Blossom whitelist" in a
    assert "NOT" in b and c == ""
