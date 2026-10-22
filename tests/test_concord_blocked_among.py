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
