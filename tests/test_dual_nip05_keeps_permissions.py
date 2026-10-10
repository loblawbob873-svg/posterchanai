"""A member with TWO NIP-05 names on this node keeps every permission ("WE HAVE TO MAKE SURE THAT THEIR
PERMISSIONS WON'T GET WIPED OUT ON DUAL-NIP05'S").

A kind-0 profile publishes ONE address. So a person holding `dual` and `dual2` always has one name their
profile does not show -- and every path that takes access away must read that as "still a member", not
"lapsed". The paths, each driven for real:

  reconcile   the 15-minute access policy (relay_access_policy.run) with the REAL membership checker
              -- the existing policy tests stub the checker out entirely
  remove      Admin -> Identities -> Remove, of either name
  modal       Admin -> profile -> Permissions: add a name, take one away

The profile publishes the SECOND name (`dual2`), not the alphabetically-first one the code treats as the
person's primary -- the case a first-name-only comparison would get wrong. And `own` holds two names
here while publishing an address of their OWN elsewhere: still a member ("the entire point was to
display both") -- membership is the name this node granted, never what the profile says. The rule
still bites on `gone`, who holds no name here at all.
"""
import asyncio
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import Bot, User
from app.services import (blossom_service, instance_membership, nip05_registry, relay_access_policy,
                          relay_blocklist, settings_store, users_store)
from app.services.instance_membership import MembershipChecker
from app.services.nostr import nostr_service
from app.services.nostr.event import build_event

SEC = {n: bytes([i + 1]) * 32 for i, n in enumerate(("dual", "solo", "own", "gone"))}
PK = {n: build_event(s, 0, "{}")["pubkey"] for n, s in SEC.items()}
FIELDS = relay_access_policy.GRANT_FIELDS
DOMAIN = "poster.place"


def run(c):
    return asyncio.run(c)


@pytest.fixture
def world(monkeypatch):
    engine = create_engine("sqlite://")
    for t in (User, Bot):
        t.__table__.create(engine)
    from tests.doc_table_mem import mem_tables
    mem_tables(monkeypatch)                  # the puppet registry is a DocTable now (#161): empty here
    db = sessionmaker(bind=engine)()
    for name, pk in PK.items():
        u = User(username=name, email=f"{name}@x.example", password_hash="x",
                 nostr_npub=nostr_service.npub_of(pk), access_revoked=False)
        for f in FIELDS:
            setattr(u, f, True)
        db.add(u)
    db.commit()
    vals = {
        "nostr_relay_nip05_domain": DOMAIN,
        nip05_registry.KEY: "\n".join([f"dual {PK['dual']}", f"dual2 {PK['dual']}", f"solo {PK['solo']}",
                                       f"own {PK['own']}", f"own2 {PK['own']}"]),
        "blossom_whitelist": "\n".join(nostr_service.npub_of(pk) for pk in PK.values()),
    }
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: vals.get(k, d if d is not None else ""))
    monkeypatch.setattr(settings_store, "put", lambda k, v, **kw: vals.__setitem__(k, v))
    monkeypatch.setattr(settings_store, "is_hydrated", lambda: True)
    monkeypatch.setattr(settings_store, "hydrate_from_db", lambda db_: None)

    async def write_through(db_, changes):
        return len(changes)
    monkeypatch.setattr(settings_store, "write_through", write_through)

    async def sync_user(db_, u, force=False):
        return True
    monkeypatch.setattr(users_store, "sync_user", sync_user)
    monkeypatch.setattr(blossom_service, "invalidate_operator_cache", lambda: None)
    from app.services import nostr_dvm
    monkeypatch.setattr(nostr_dvm, "peer_pubkeys", lambda: set())
    import app.services.nostr_relay.thread as thread
    monkeypatch.setattr(thread, "trigger_nip05_reload", lambda: {})
    monkeypatch.setattr(relay_blocklist, "is_blocked", lambda pk: False)

    # What each person's own signed kind-0 publishes: dual shows their SECOND name, own and gone show an
    # address elsewhere. None of it decides anything any more -- which is what these tests prove.
    published = {"dual": "dual2@" + DOMAIN, "solo": "solo@" + DOMAIN, "own": "someone@elsewhere.example",
                 "gone": "gone@elsewhere.example"}
    checker = MembershipChecker(configuration=lambda: [vals[nip05_registry.KEY], DOMAIN, ""])
    monkeypatch.setattr(instance_membership, "status", checker.status)
    monkeypatch.setattr(instance_membership, "_configuration", lambda: [vals[nip05_registry.KEY], DOMAIN, ""])

    async def profiles(pks):
        return {PK[n]: {"nip05": a} for n, a in published.items()}, True
    monkeypatch.setattr(relay_blocklist, "profiles", profiles)
    vals["_published"] = published               # a test may change what a profile says
    yield db, vals
    db.close()


def perms(db, name):
    db.expire_all()
    u = db.query(User).filter(User.username == name).one()
    return {f: getattr(u, f) for f in FIELDS}, u.access_revoked


def whitelisted(vals, name):
    return nostr_service.npub_of(PK[name]) in vals["blossom_whitelist"] or PK[name] in vals["blossom_whitelist"]


def test_the_reconcile_keeps_a_member_who_publishes_their_second_name(world):
    db, vals = world
    run(relay_access_policy.run(db))
    granted, revoked = perms(db, "dual")
    assert all(granted.values()) and not revoked, ("a dual-name member lost access in the reconcile", granted)
    assert whitelisted(vals, "dual"), "a dual-name member was dropped from the Blossom whitelist"
    assert all(perms(db, "solo")[0].values())
    # Two names here, an address of their own in the profile: a member, both addresses kept.
    granted, revoked = perms(db, "own")
    assert all(granted.values()) and not revoked, ("publishing your own NIP-05 cost you access", granted)
    assert whitelisted(vals, "own")
    # The rule still bites where it should: no name here -> not a member.
    granted, revoked = perms(db, "gone")
    assert not granted["can_ai"] and revoked and not whitelisted(vals, "gone")


def test_there_is_no_remove_all_not_in_profile(world):
    """The admin bulk action that stripped every name whose profile showed another address is gone with the
    rule it enforced: run against `own` it would have taken both of their names, and their access."""
    from app.routers import admin
    assert not hasattr(admin, "relay_identities_remove_unverified")
    assert not hasattr(nip05_registry, "remove_unverified")


@pytest.mark.parametrize("which", ["dual", "dual2"])
def test_removing_one_of_their_names_takes_no_permission(world, which):
    db, vals = world
    from app.routers import admin
    r = run(admin.relay_identity_remove(admin.RelayIdentityRemoveReq(name=which), db=db, admin=None))
    assert r["orphaned"] == [] and r["revoked"]["accounts"] == 0, r
    assert all(perms(db, "dual")[0].values()) and whitelisted(vals, "dual")


def test_the_permissions_modal_adds_and_removes_a_name_without_touching_access(world, monkeypatch):
    db, vals = world
    from app.routers import client
    monkeypatch.setattr(client, "_verify_admin_auth", lambda db_, auth, target, action: True)
    monkeypatch.setattr(client, "_nip05_domain", lambda request, db_: DOMAIN)

    async def no_welcome(*a, **k):
        return None
    import app.services.instance_welcome as welcome
    monkeypatch.setattr(welcome, "notify_approval", no_welcome)
    for body in ({"name": "dual3", "add": True}, {"name": "dual", "remove": True}):
        req = client.AdminNip05Req(target=PK["dual"], auth="x", **body)
        resp = run(client.admin_nip05(req, request=None, db=db))
        assert json.loads(resp.body)["ok"], resp.body
        assert all(perms(db, "dual")[0].values()) and whitelisted(vals, "dual")
    from app.services.nostr_relay.thread import _parse_nip05
    names, _ = _parse_nip05(vals[nip05_registry.KEY], "")
    assert sorted(n for n, k in names.items() if k == PK["dual"]) == ["dual2", "dual3"]
    # ...and the reconcile afterwards still finds them a member (their profile shows dual2).
    run(relay_access_policy.run(db))
    assert all(perms(db, "dual")[0].values()) and not perms(db, "dual")[1]


def test_a_member_revoked_over_letter_case_gets_everything_back_at_the_next_cleanup(world):
    """DreadPirate, on poster.place: granted 'dreadpirate', profile 'DreadPirate@poster.place', and the
    cleanup had taken AI, Blossom and streaming (access_revoked). The next run must find them a member and
    restore every permission and their Blossom line -- not merely stop revoking."""
    db, vals = world
    vals["_published"]["solo"] = "SOLO@Poster.Place"
    u = db.query(User).filter(User.username == "solo").one()
    for f in FIELDS:
        setattr(u, f, False)
    u.access_revoked = True
    db.commit()
    vals["blossom_whitelist"] = "\n".join(nostr_service.npub_of(PK[n]) for n in ("dual", "own"))
    run(relay_access_policy.run(db))
    granted, revoked = perms(db, "solo")
    assert all(granted.values()) and not revoked, ("a member publishing their name in capitals stayed revoked", granted)
    assert whitelisted(vals, "solo")
