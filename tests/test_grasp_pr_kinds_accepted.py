"""GRASP-01: kinds 1618 (pull request) and 1619 (PR update) must reach the repo-scoped gate.

They used to fall past the git-collaboration branch in `_on_event` into the web-of-trust gate and
come back `blocked: not in web of trust`. That is the DEFAULT contribution path for every ngit v3
client (`ngit send` / `git push pr/<branch>` pick the PR kind whenever the repo has a GRASP server),
and ngit v3 fetches collaboration events exclusively from the relays a repository declares — so for
a repo naming us, a refused PR did not exist anywhere. Contribution was impossible.

These drive the SHIPPED `_on_event` with real signed events, because the bug was a missing member of
a tuple in a chain of `elif`s: a test that only read the tuple would have passed against a branch
wired to the wrong gate.
"""
from __future__ import annotations

import asyncio
import os

import pytest

from app.services.nostr.event import build_event
from app.services.nostr_relay.server import RelayServer
from app.services.nostr_relay import store as store_mod


OWNER = "a" * 64
REPO = "myrepo"
STRANGER_SK = bytes.fromhex("11" * 32)   # deliberately NOT a web-of-trust member


class FakeGate:
    """Nobody is a member — the WoT gate refuses every author it is asked about, so any event that
    reaches it is refused. That is exactly the pre-fix behaviour we are proving is gone."""

    def is_member(self, pk):        return False
    def is_operator(self, pk):      return False
    def is_blocked(self, pk):       return False
    def is_puppet_event(self, ev):  return False
    def mark_bridged(self, pk):     pass
    def mark_bridged_identity(self, pk): pass


class FakeStore:
    def __init__(self, announced=True):
        self.announced = announced
        self.added = []

    async def is_repo_announced(self, owner, repo_id):
        return self.announced and owner == OWNER and repo_id == REPO

    async def add_event(self, ev, origin="direct"):
        self.added.append((ev, origin))
        return True

    async def has_event(self, eid):
        return False

    async def query(self, filters, hard_cap=None):
        return []


class FakeSubs:
    def fanout(self, ev, send, can_serve):
        pass


def relay(announced=True):
    s = object.__new__(RelayServer)
    s.gate = FakeGate()
    s.store = FakeStore(announced=announced)
    s.subs = FakeSubs()
    s.cfg = {"wot_enabled": True}
    s.private_cb = None
    s.outbox_cb = None
    s._auth_pubkeys = {}
    s._call_seen = {}
    s._bridge_pubkeys = set()
    s.sent = []
    s._send = lambda conn, msg: s.sent.append(msg)
    s._refuse = lambda conn, eid, ev, why: s.sent.append(["OK", eid, False, why])
    return s


def pr_event(kind, sk=STRANGER_SK, a_tag=f"30617:{OWNER}:{REPO}"):
    tags = [["c", "b" * 40]]
    if a_tag:
        tags.insert(0, ["a", a_tag])
    return build_event(sk, kind, "please merge", tags)


def deliver(s, ev):
    asyncio.run(s._on_event(object(), ev))
    return s.sent[-1] if s.sent else None


@pytest.mark.parametrize("kind", [1618, 1619])
def test_a_pull_request_from_a_non_member_is_accepted_for_an_announced_repo(kind):
    """The pre-fix failure verbatim: OK=false, `blocked: not in web of trust`."""
    s = relay()
    ok = deliver(s, pr_event(kind))
    assert ok[2] is True, ok
    assert s.store.added, "the PR must be stored, not merely acknowledged"


@pytest.mark.parametrize("kind", [1618, 1619])
def test_a_pull_request_for_an_unknown_repo_is_still_refused(kind):
    """The repo scope is what keeps the WoT exemption from being an open spam firehose. It must
    still bind — accepting 1618/1619 unconditionally would be a different bug."""
    s = relay(announced=False)
    ok = deliver(s, pr_event(kind))
    assert ok[2] is False
    assert "unknown repo" in ok[3]


@pytest.mark.parametrize("kind", [1618, 1619])
def test_a_pull_request_with_no_repo_reference_is_refused(kind):
    s = relay()
    ok = deliver(s, pr_event(kind, a_tag=None))
    assert ok[2] is False


def test_a_patch_is_unaffected():
    """Do not let the 1618/1619 addition move the kinds that already worked."""
    s = relay()
    assert deliver(s, pr_event(1617))[2] is True


def test_an_ordinary_note_from_a_non_member_is_still_web_of_trust_gated():
    """The guard against 'fixed it by widening the gate': kind 1 must still be refused."""
    s = relay()
    ok = deliver(s, build_event(STRANGER_SK, 1, "hello"))
    assert ok[2] is False and "web of trust" in ok[3]


@pytest.mark.parametrize("kind", [1618, 1619])
def test_a_pull_request_is_never_expirable(kind):
    """_GIT_KINDS feeds _NEVER_EXPIRE_KINDS. 1618/1619 were absent from it while the COMMENTS on a
    PR were already shielded (_GIT_COMMENT_ROOT_KINDS carries '1618'), so a stray NIP-40 tag could
    delete the one event carrying a contribution's commit ids while its discussion was kept."""
    assert kind in store_mod._GIT_KINDS
    assert kind in store_mod._NEVER_EXPIRE_KINDS
    assert kind not in store_mod._PRUNABLE_KINDS


@pytest.mark.parametrize("kind", ["1618", "1619"])
def test_the_firehose_mirrors_pull_requests(kind):
    """The ingest allowlist default and the firehose's own repo-scoped set. Without the kind in
    `ingest_kinds` the firehose never subscribes to it, so the gate below is never reached and a PR
    published to an upstream relay is invisible here."""
    src = open(os.path.join(os.path.dirname(__file__), "..", "app", "services",
                            "nostr_relay", "thread.py")).read()
    line = [l for l in src.splitlines() if '"ingest_kinds"' in l][0]
    assert f",{kind}," in line, line
    from app.services.nostr_relay.thread import _GIT_COLLAB_KINDS
    assert int(kind) in _GIT_COLLAB_KINDS
