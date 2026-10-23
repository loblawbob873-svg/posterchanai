"""EVERY admin route refuses a non-admin ON THE SERVER, and the list of admin routes is DISCOVERED.

A hidden button is not an access check. This walks the real application (`app.main.app`, every router it
mounts, prefixes included) and classifies every HTTP route by the gate it actually carries:

  dep      its dependency tree contains an admin gate: `auth.get_admin_user` (and client's `_admin_only`
           wrapper), Media Center's `get_media_admin`, or community's `get_bot_auth` (the bots' key or an
           ADMIN's API key).
  signed   the endpoint verifies a signed admin proof (`client._verify_admin_auth` / `_verify_admin_signer`):
           the client's profile ⋯ menu acts without a session, so the proof IS the credential.
  inline   the endpoint checks `is_admin` itself (listed in INLINE, each with its own driver).

Then every route is called three ways, through the real app:

  signed out   -> 401/403
  non-admin    -> 403   (a real member account with an npub, so the operator-repair branch runs too)
  admin        -> the gate ADMITS: the endpoint is reached (a 418 sentinel stands in for the handler body,
                  so no admin action runs against anything) or the request fails validation AFTER the gate
                  (422 -- FastAPI raises a dependency's HTTPException before reporting parameter errors, so
                  a 422 can only be seen once every dependency, the gate included, passed).

A route that LOOKS admin -- '/admin' in its path, an "admin-only" docstring, or living in a module that is
admin-only by nature -- but carries none of the gates fails `test_nothing_admin_shaped_is_ungated` unless it
is in EXEMPT with the reason it is not an admin action. That is what makes a new admin route without a check
fail here rather than ship.

Hermetic: no lifespan (TestClient is never entered), get_db yields None, every user/key lookup is a fixture,
and opening ANY socket fails the test -- a gate that went to the relay to decide would be caught, not served
by whatever relay happens to run on this machine.
"""
import base64
import inspect
import json
import re
import socket
import time
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from fastapi.routing import APIRoute, _IncludedRouter
from fastapi.testclient import TestClient

from app import auth as app_auth
from app.services.nostr import event as nostr_event
from app.services.nostr import nostr_service

import fastapi.routing as _fr

SENTINEL = 418          # "the gate let this caller through; the handler body was not run"
_REAL_RUN = _fr.run_endpoint_function

ADMIN_MODULES = {"app.routers.admin", "app.routers.admin_emoji", "app.routers.bots",
                 "app.routers.monero_wallet", "app.routers.community"}
ADMIN_WORDS = re.compile(r"admin[- ]only|admins only|only admins|admin[- ]gated|administrator access", re.I)

# Admin-SHAPED routes that are deliberately not admin actions. Each says why; a new entry needs the same.
EXEMPT = {
    ("GET", "/admin"): "the admin panel SHELL: static HTML holding no data; every value is fetched from "
                       "/api/admin/*, which is gated (and tested below)",
    ("POST", "/client/claim-admin"): "first-run setup: only possible while NO other key holds admin "
                                     "(test_claim_admin_cannot_take_over_a_configured_node)",
    ("GET", "/client/admin-nip05"): "public read of the node's own nostr.json registry",
    ("PUT", "/api/vmhost/iso/{ticket}"): "the credential is a single-use ticket an ADMIN minted over Nostr "
                                         "(iso.upload_ticket), consumed before the body is read and the admin "
                                         "re-checked -- tests/test_vmhost_phase2.py, test_vmhost_iso_limits.py",
}

# Endpoints whose body checks is_admin itself. Each driver: (setup(monkeypatch), method, path).
INLINE = {("GET", "/api/git/repos")}


# --------------------------------------------------------------------------------------------- discovery
def _flat(routes):
    for r in routes:
        if isinstance(r, _IncludedRouter):
            for c in r.effective_candidates():
                yield from (_flat([c]) if isinstance(c, _IncludedRouter) else [c])
        else:
            yield r


def _calls(dependant):
    out = []
    for sub in dependant.dependencies:
        out.append(sub.call)
        out += _calls(sub)
    return out


def _gate_funcs():
    from app.routers import client as client_router
    from app.routers import community, media_center
    return {"get_admin_user": app_auth.get_admin_user, "_admin_only": client_router._admin_only,
            "get_media_admin": media_center.get_media_admin, "get_bot_auth": community.get_bot_auth}


def _source(fn):
    try:
        return inspect.getsource(fn)
    except (OSError, TypeError):
        return ""


@pytest.fixture(scope="module")
def app_and_routes():
    from app.main import app
    gates = _gate_funcs()
    rows = []
    for c in _flat(app.router.routes):
        orig = getattr(c, "original_route", c)
        if not isinstance(orig, APIRoute):
            continue
        dependant = getattr(c, "dependant", None) or orig.dependant
        calls = _calls(dependant)
        gate = next((name for name, fn in gates.items() if fn in calls), None)
        src = _source(orig.endpoint)
        if gate is None and re.search(r"_verify_admin_(auth|signer)\(", src):
            gate = "signed"
        for m in sorted(orig.methods or ()):
            if m in ("HEAD", "OPTIONS"):
                continue
            g = gate or ("inline" if (m, c.path) in INLINE else None)
            rows.append(SimpleNamespace(method=m, path=c.path, gate=g, endpoint=orig.endpoint, src=src,
                                        module=orig.endpoint.__module__, route=orig))
    return app, rows


def _by(rows, *gates):
    return [r for r in rows if r.gate in gates]


# --------------------------------------------------------------------------------------------- the world
OP_SK, ADMIN_SK, MALLORY_SK, VICTIM_SK = (bytes([n]) * 32 for n in (0x31, 0x32, 0x33, 0x34))


def _hex(sk):
    return nostr_event.build_event(sk, 1, "x")["pubkey"]


OP, ADMIN, MALLORY, VICTIM = (_hex(s) for s in (OP_SK, ADMIN_SK, MALLORY_SK, VICTIM_SK))


def _user(uid, pk, admin):
    return SimpleNamespace(id=uid, username=("admin" if admin else "mallory"), email=None,
                           nostr_npub=nostr_service.npub_of(pk), nostr_nsec=None, is_admin=admin,
                           can_ai=True, can_image=True, can_music=True, can_video=True, can_media=True,
                           can_stream=True, can_torrent=True, can_blossom=True, is_blocked=False,
                           access_revoked=False, token_version=0)


ADMIN_USER = _user(1, ADMIN, True)
MALLORY_USER = _user(2, MALLORY, False)


@pytest.fixture
def world(monkeypatch, app_and_routes):
    """The app with its users and keys as fixtures, and the network switched off."""
    app, rows = app_and_routes
    from app.database import get_db
    from app.services import instance_membership, user_table
    from app.utils import auth_utils
    from app.services import bot_table

    def no_network(*a, **k):
        raise AssertionError("a gate tried to open a socket: %r" % (a,))
    monkeypatch.setattr(socket.socket, "connect", no_network)
    monkeypatch.setattr(socket, "create_connection", no_network)

    def _db():
        yield None
    app.dependency_overrides[get_db] = _db
    monkeypatch.setattr(app_auth, "_OPERATOR_PK", OP)          # the node's key is NOBODY's session here

    by_npub = {ADMIN_USER.nostr_npub: ADMIN_USER, MALLORY_USER.nostr_npub: MALLORY_USER}

    async def aby_npub(db, npub):
        return by_npub.get(npub)
    monkeypatch.setattr(user_table, "aby_npub", aby_npub)

    async def member(u):
        return u

    async def member_key(pk):
        return {"qualified": True, "pubkey": pk}
    monkeypatch.setattr(instance_membership, "require_user", member)
    monkeypatch.setattr(instance_membership, "require_member_user", member)
    monkeypatch.setattr(instance_membership, "require_pubkey", member_key)
    monkeypatch.setattr(instance_membership, "require_member_pubkey", member_key)

    keys = {"sk-admin": ADMIN_USER, "sk-mallory": MALLORY_USER}

    async def aquery(key):
        u = keys.get(key)
        return (key, u.id) if u else (None, None)
    monkeypatch.setattr(auth_utils, "aquery_api_key", aquery)
    monkeypatch.setattr(auth_utils, "get_user_from_api_key",
                        lambda db, uid: next((u for u in keys.values() if u.id == uid), None))

    async def no_bots(db):
        return []
    monkeypatch.setattr(bot_table, "aall_bots", no_bots)

    # The handler body never runs: once every dependency resolved, the endpoint is replaced by the sentinel.
    import fastapi.routing as fr

    async def reached(*a, **k):
        raise HTTPException(SENTINEL, "gate passed")
    monkeypatch.setattr(fr, "run_endpoint_function", reached)

    # A signed proof the real verifier ACCEPTED stops there too, before the action.
    from app.routers import client as client_router
    for name in ("_verify_admin_auth", "_verify_admin_signer"):
        real = getattr(client_router, name)

        async def gate(*a, _real=real, **k):
            who = await _real(*a, **k)
            if who:
                raise HTTPException(SENTINEL, "admin proof accepted")
            return who
        monkeypatch.setattr(client_router, name, gate)

    yield SimpleNamespace(app=app, rows=rows, client=TestClient(app, raise_server_exceptions=False))
    app.dependency_overrides.clear()


def _as(world, user):
    """Signed in as `user` (None: signed out). Both session readers, since Media Center reads the optional one."""
    for dep in (app_auth.get_current_user, app_auth.get_current_user_optional):
        world.app.dependency_overrides.pop(dep, None)
    if user is not None:
        world.app.dependency_overrides[app_auth.get_current_user] = lambda: user
        world.app.dependency_overrides[app_auth.get_current_user_optional] = lambda: user


def _url(path):
    return re.sub(r"\{[^}]+\}", "1", path)


def _call(world, row, headers=None, json_body=None):
    return world.client.request(row.method, _url(row.path), headers=headers or {}, json=json_body)


# --------------------------------------------------------------------------------------------- discovery tests
def test_the_admin_routes_are_found(app_and_routes):
    """The premise: discovery sees the admin surface at all (a walker that found nothing would pass
    every test below vacuously)."""
    _, rows = app_and_routes
    dep = _by(rows, "get_admin_user", "_admin_only")
    assert len(dep) >= 100, len(dep)
    assert any(r.path == "/api/admin/settings" and r.method == "PUT" for r in dep)
    assert len(_by(rows, "signed")) >= 10
    assert _by(rows, "get_media_admin") and _by(rows, "get_bot_auth")
    assert {(r.method, r.path) for r in _by(rows, "inline")} == INLINE, "an INLINE entry no longer exists"


def test_nothing_admin_shaped_is_ungated(app_and_routes):
    _, rows = app_and_routes
    bad = []
    for r in rows:
        if r.gate or (r.method, r.path) in EXEMPT:
            continue
        doc = r.endpoint.__doc__ or ""
        if "/admin" in r.path or "admin-" in r.path or ADMIN_WORDS.search(doc) or r.module in ADMIN_MODULES:
            bad.append((r.method, r.path, r.module))
    assert not bad, ("admin-shaped routes with NO server-side admin gate", bad)


def test_every_exemption_still_exists(app_and_routes):
    _, rows = app_and_routes
    have = {(r.method, r.path) for r in rows}
    assert set(EXEMPT) <= have, set(EXEMPT) - have


# --------------------------------------------------------------------------------------------- session-gated
def _session_rows():
    from app.main import app
    gates = _gate_funcs()
    out = []
    for c in _flat(app.router.routes):
        orig = getattr(c, "original_route", c)
        if not isinstance(orig, APIRoute):
            continue
        calls = _calls(getattr(c, "dependant", None) or orig.dependant)
        if any(gates[g] in calls for g in ("get_admin_user", "_admin_only", "get_media_admin")):
            for m in sorted(orig.methods or ()):
                if m not in ("HEAD", "OPTIONS"):
                    out.append((m, c.path))
    return out


SESSION_ROUTES = _session_rows()


@pytest.mark.parametrize("method,path", SESSION_ROUTES, ids=["%s %s" % x for x in SESSION_ROUTES])
def test_a_session_gated_admin_route(world, method, path):
    row = SimpleNamespace(method=method, path=path)
    _as(world, None)
    r = _call(world, row)
    assert r.status_code in (401, 403), ("signed out was let in", method, path, r.status_code, r.text[:200])
    _as(world, MALLORY_USER)
    r = _call(world, row)
    assert r.status_code == 403, ("a signed-in NON-admin was not refused", method, path, r.status_code, r.text[:200])
    _as(world, ADMIN_USER)
    r = _call(world, row)
    assert r.status_code in (SENTINEL, 422), ("the admin was not admitted", method, path, r.status_code, r.text[:300])


# --------------------------------------------------------------------------------------------- bots' key / admin key
def _bot_rows():
    from app.main import app
    from app.routers import community
    out = []
    for c in _flat(app.router.routes):
        orig = getattr(c, "original_route", c)
        if isinstance(orig, APIRoute) and community.get_bot_auth in _calls(getattr(c, "dependant", None) or orig.dependant):
            out += [(m, c.path) for m in sorted(orig.methods or ()) if m not in ("HEAD", "OPTIONS")]
    return out


BOT_ROUTES = _bot_rows()


@pytest.mark.parametrize("method,path", BOT_ROUTES, ids=["%s %s" % x for x in BOT_ROUTES])
def test_a_community_route_takes_an_admin_key_not_a_members(world, method, path):
    row = SimpleNamespace(method=method, path=path)
    _as(world, None)
    assert _call(world, row).status_code in (401, 403)
    r = _call(world, row, headers={"X-API-Key": "sk-mallory"})
    assert r.status_code == 403, (method, path, r.status_code, r.text[:200])
    r = _call(world, row, headers={"X-API-Key": "sk-admin"})
    assert r.status_code in (SENTINEL, 422), (method, path, r.status_code, r.text[:200])


# --------------------------------------------------------------------------------------------- signed proofs
def _proof(sk, content, ptag=None, at=None):
    tags = [["p", ptag]] if ptag else []
    ev = nostr_event.build_event(sk, 27235, content, tags=tags, created_at=at)
    return base64.b64encode(json.dumps(ev).encode()).decode()


def _signed_rows():
    from app.main import app
    out = []
    for c in _flat(app.router.routes):
        orig = getattr(c, "original_route", c)
        if not isinstance(orig, APIRoute):
            continue
        src = _source(orig.endpoint)
        if not re.search(r"_verify_admin_(auth|signer)\(", src):
            continue
        calls = re.findall(r"_verify_admin_(?:auth|signer)\(([^)]*)\)", src)
        purposes = sorted({p for call in calls for p in re.findall(r'"([^"]+)"', call)})
        for m in sorted(orig.methods or ()):
            if m not in ("HEAD", "OPTIONS"):
                out.append((m, c.path, tuple(purposes)))
    return out


SIGNED_ROUTES = _signed_rows()


def _body(method, path, auth):
    from app.main import app
    route = next(getattr(r, "original_route", r) for r in _flat(app.router.routes)
                 if getattr(r, "path", None) == path and isinstance(getattr(r, "original_route", r), APIRoute)
                 and method in (getattr(r, "original_route", r).methods or ()))
    model = next(p.annotation for p in inspect.signature(route.endpoint).parameters.values()
                 if isinstance(p.annotation, type) and hasattr(p.annotation, "model_fields"))
    body = {}
    for name, f in model.model_fields.items():
        if name == "auth":
            body[name] = auth
        elif name == "target":
            body[name] = VICTIM
        elif name == "targets":
            body[name] = [VICTIM]
        elif f.is_required():
            body[name] = {} if "dict" in str(f.annotation) else "x"
    return body


@pytest.mark.parametrize("method,path,purposes", SIGNED_ROUTES, ids=["%s %s" % x[:2] for x in SIGNED_ROUTES])
def test_a_signed_admin_route(world, method, path, purposes, monkeypatch):
    import fastapi.routing as fr
    monkeypatch.setattr(fr, "run_endpoint_function", _REAL_RUN)   # the check lives in the body; the
    # verifier wrapper (world) is what stops an ACCEPTED proof before the action runs.
    assert purposes, ("no purpose string found -- the proof is not bound to an action", path)
    row = SimpleNamespace(method=method, path=path)
    # Signed out: no proof at all, and a proof that is not a signature.
    for auth in ("", base64.b64encode(b'{"kind":27235}').decode()):
        r = _call(world, row, json_body=_body(method, path, auth))
        assert r.status_code in (401, 403), (path, "no valid proof", r.status_code, r.text[:200])
    # A member who is NOT an admin signs a perfectly good proof for exactly this action.
    for p in purposes:
        r = _call(world, row, json_body=_body(method, path, _proof(MALLORY_SK, p, VICTIM)))
        assert r.status_code == 403, (path, "non-admin proof", p, r.status_code, r.text[:200])
    # The admin's proof is bound: another action's proof, and a stale one, are refused.
    r = _call(world, row, json_body=_body(method, path, _proof(ADMIN_SK, "some-other-action", VICTIM)))
    assert r.status_code == 403, (path, "an admin proof for ANOTHER action was accepted", r.status_code)
    stale = int(time.time()) - 3600
    for p in purposes:
        r = _call(world, row, json_body=_body(method, path, _proof(ADMIN_SK, p, VICTIM, at=stale)))
        assert r.status_code == 403, (path, "a stale admin proof was accepted", p, r.status_code)
    # And the admin, signing for this action, is admitted.
    got = [_call(world, row, json_body=_body(method, path, _proof(ADMIN_SK, p, VICTIM))).status_code for p in purposes]
    assert SENTINEL in got, (path, "the admin's own proof was refused", dict(zip(purposes, got)))


def test_a_targeted_proof_names_its_target(world, monkeypatch):
    """A proof for one account cannot be spent on another (the p-tag binds it)."""
    import fastapi.routing as fr
    monkeypatch.setattr(fr, "run_endpoint_function", _REAL_RUN)
    row = SimpleNamespace(method="POST", path="/client/block")
    body = {"target": VICTIM, "auth": _proof(ADMIN_SK, "block", MALLORY)}
    assert _call(world, row, json_body=body).status_code == 403
    body["auth"] = _proof(ADMIN_SK, "block", VICTIM)
    assert _call(world, row, json_body=body).status_code == SENTINEL


# --------------------------------------------------------------------------------------------- inline
def test_hosted_git_repo_list_is_admin_only(world, monkeypatch):
    from app.routers import git
    monkeypatch.setattr(git, "_require_enabled", lambda: None)
    monkeypatch.setattr(git, "_require_local_host", lambda: None)
    monkeypatch.setattr(git.ghs, "list_repos", lambda: [{"id": "private-thing"}])
    monkeypatch.setattr(git.ghs, "total_size_gb", lambda: 0.0)
    row = SimpleNamespace(method="GET", path="/api/git/repos")
    import fastapi.routing as fr
    # The check lives IN the endpoint body, so the body runs for real here (its I/O is stubbed above).
    monkeypatch.setattr(fr, "run_endpoint_function", _REAL_RUN)
    _as(world, None)
    assert _call(world, row).status_code in (401, 403)
    _as(world, MALLORY_USER)
    r = _call(world, row)
    assert r.status_code == 403 and "private-thing" not in r.text, (r.status_code, r.text[:200])
    _as(world, ADMIN_USER)
    r = _call(world, row)
    assert r.status_code == 200 and "private-thing" in r.text, (r.status_code, r.text[:200])



# --------------------------------------------------------------------------------------------- first-run claim
def test_claim_admin_cannot_take_over_a_configured_node(world, monkeypatch):
    from app.services import user_table
    import fastapi.routing as fr
    monkeypatch.setattr(fr, "run_endpoint_function", _REAL_RUN)

    async def admins(db):
        return [ADMIN_USER]
    monkeypatch.setattr(user_table, "aadmins", admins)
    wrote = []

    async def save(db, u):
        wrote.append(u)
    monkeypatch.setattr(user_table, "asave", save)
    r = world.client.post("/client/claim-admin",
                          json={"pubkey": MALLORY, "auth": _proof(MALLORY_SK, "claim-admin", MALLORY)})
    assert r.status_code == 409, (r.status_code, r.text[:200])
    assert not wrote and MALLORY_USER.is_admin is False


# --------------------------------------------------------------------------------------------- the admin UI's calls
ROOT = __import__("pathlib").Path(__file__).resolve().parents[1]
UI_FILES = sorted([*ROOT.glob("static/js/admin*.js"), ROOT / "templates/admin.html",
                   *ROOT.glob("templates/admin/tabs/*.html")])
_URL = re.compile(r"""['"`](/(?:api|client)/[A-Za-z0-9_/\-]+)""")


def test_every_endpoint_the_admin_panel_calls_exists_and_is_admin_gated(app_and_routes):
    """The other end of the inventory: every server address the Admin panel's buttons call. Each must be a
    route that exists (a button calling a removed route is a control that can only ever answer 'Not Found')
    and that carries an admin gate. A literal ending in '/' is a PREFIX the script completes
    ('/api/admin/models/' + kind + '/download'): some route must start with it and every one that does
    must be gated."""
    _, rows = app_and_routes
    regex = [(re.compile("^" + re.sub(r"\{[^}]+\}", "[^/]+", r.path) + "$"), r) for r in rows]
    missing, ungated = [], []
    for f in UI_FILES:
        for url in sorted(set(_URL.findall(f.read_text(encoding="utf-8")))):
            hits = ([r for r in rows if r.path.startswith(url)] if url.endswith("/")
                    else [r for rx, r in regex if rx.match(url)])
            if not hits:
                missing.append((f.name, url))
            else:
                ungated += [(f.name, url, r.method) for r in hits if not r.gate]
    assert not missing, ("the admin panel calls routes that do not exist", missing)
    assert not ungated, ("the admin panel calls routes with no admin gate", ungated)
