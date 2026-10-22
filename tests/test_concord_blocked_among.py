"""POST /client/concord/blocked-among: members learn which of the authors in front of them this node blocked."""
import asyncio

from app.routers import concord_tunnel as R
from app.services import relay_blocklist

BAD, GOOD = 'a' * 64, 'b' * 64


def _ask(pks, monkeypatch):
    monkeypatch.setattr(relay_blocklist, "is_blocked", lambda pk: pk == BAD)
    return asyncio.run(R.blocked_among(R._AmongReq(pubkeys=pks), user=None))


def test_only_the_blocked_ones_come_back(monkeypatch):
    assert _ask([BAD, GOOD], monkeypatch) == {"blocked": [BAD]}


def test_junk_is_ignored_and_the_question_is_bounded(monkeypatch):
    assert _ask(["../etc", "A" * 64, "", BAD.upper()], monkeypatch) == {"blocked": [BAD]}
    assert _ask([GOOD] * 499 + [BAD] + [BAD] * 10, monkeypatch) == {"blocked": [BAD]}
    assert _ask([GOOD] * 500 + [BAD], monkeypatch) == {"blocked": []}, "more than 500 at a time must not be asked"


def test_it_is_members_only():
    import inspect
    sig = inspect.signature(R.blocked_among)
    dep = sig.parameters["user"].default
    assert getattr(dep, "dependency", None) is R._member


def test_over_http_a_stranger_is_refused_and_a_member_is_answered(monkeypatch):
    """The real route and its real dependency: no session is no answer (who is blocked is moderation state)."""
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    app = FastAPI()
    app.include_router(R.router)
    c = TestClient(app)
    r = c.post("/client/concord/blocked-among", json={"pubkeys": [BAD]})
    assert r.status_code in (401, 403), r.status_code
    monkeypatch.setattr(relay_blocklist, "is_blocked", lambda pk: pk == BAD)
    app.dependency_overrides[R._member] = lambda: object()
    r = c.post("/client/concord/blocked-among", json={"pubkeys": [BAD, GOOD]})
    assert r.status_code == 200 and r.json() == {"blocked": [BAD]}
