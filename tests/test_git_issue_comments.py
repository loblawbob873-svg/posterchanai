"""NIP-22 comments on NIP-34 issues/patches — the relay must accept and keep them.

Current NIP-34 dropped kind-1622 replies: "Replies to a kind:1621 (issue) … should follow NIP-22
comment", i.e. kind 1111 with the root repeated in uppercase E/K/P tags at every depth. gitworkshop
publishes and renders exactly those — which is how the gap was found: every reply on this repo's 38
issues was a plain kind-1 (this client's old shape), so gitworkshop showed the issues and none of
the discussion.

The relay half has two gates and both used to drop a stranger's 1111: the WS write path
(server._on_event) and the firehose (thread.py). The exemption is deliberately narrow — the comment
itself carries no repo `a` tag, so scope is reached through the ROOT: uppercase K must name a git
kind, uppercase E must resolve to a stored issue/patch, and THAT event's `a` tag must name a known
repo. An ordinary community/article comment never enters the branch and stays WoT-gated.

Run: venv-unified/bin/python -m pytest tests/test_git_issue_comments.py
"""
import asyncio
import re
import unittest
from pathlib import Path

from app.services.nostr_relay.server import (RelayServer, _git_comment_root,
                                             _GIT_COMMENT_ROOT_KINDS)

ROOT_ID = "d" * 64
ISSUE_AUTHOR = "b" * 64
REPO_ADDR = "30617:" + ("a" * 64) + ":posterchanai"

SRC_SERVER = Path(__file__).resolve().parent.parent / "app" / "services" / "nostr_relay" / "server.py"
SRC_THREAD = Path(__file__).resolve().parent.parent / "app" / "services" / "nostr_relay" / "thread.py"


def _comment(root_kind="1621", root_id=ROOT_ID, extra=()):
    return {"id": "1" * 64, "pubkey": "c" * 64, "kind": 1111, "created_at": 1, "content": "hi",
            "tags": [["E", root_id], ["K", root_kind], ["P", ISSUE_AUTHOR],
                     ["e", root_id], ["k", root_kind], ["p", ISSUE_AUTHOR]] + list(extra),
            "sig": "0" * 128}


class GitCommentRoot(unittest.TestCase):
    """_git_comment_root: the shared classifier both gates key on."""

    def test_an_issue_comment_yields_its_root(self):
        for rk in _GIT_COMMENT_ROOT_KINDS:
            self.assertEqual(_git_comment_root(_comment(root_kind=rk)), ROOT_ID)

    def test_a_nested_reply_still_yields_the_root(self):
        # NIP-22 repeats the uppercase root verbatim at every depth; only the lowercase parent moves.
        ev = _comment()
        ev["tags"][3] = ["e", "2" * 64]   # parent = another comment, root unchanged
        self.assertEqual(_git_comment_root(ev), ROOT_ID)

    def test_an_ordinary_comment_is_not_a_git_comment(self):
        # An article thread (K=30023) — the bulk of real 1111 traffic — must never enter the branch,
        # or the WoT gate on ordinary comments is silently gone.
        self.assertIsNone(_git_comment_root(_comment(root_kind="30023")))
        self.assertIsNone(_git_comment_root({"kind": 1111, "tags": [["e", ROOT_ID]]}))

    def test_a_git_K_without_a_root_id_is_refused(self):
        ev = _comment()
        ev["tags"] = [t for t in ev["tags"] if t[0] != "E"]
        self.assertIsNone(_git_comment_root(ev))
        ev["tags"].append(["E", "not-64-hex"])
        self.assertIsNone(_git_comment_root(ev))


class _StubStore:
    """Only what _git_comment_for_known_repo touches: the root lookup + the announcement check."""

    def __init__(self, root_event=None, announced=frozenset()):
        self.root_event = root_event
        self.announced = announced

    async def query(self, filters, hard_cap=5000):
        f = filters[0]
        if self.root_event and self.root_event["id"] in f.get("ids", []) \
                and self.root_event["kind"] in f.get("kinds", []):
            return [self.root_event]
        return []

    async def is_repo_announced(self, owner_hex, repo_id):
        return f"30617:{owner_hex}:{repo_id}" in self.announced


def _srv(store):
    srv = RelayServer.__new__(RelayServer)
    srv.store = store
    return srv


def _known(root_event=None, announced=frozenset(), root_id=ROOT_ID):
    srv = _srv(_StubStore(root_event, announced))
    return asyncio.run(srv._git_comment_for_known_repo(root_id))


class KnownRepoLookup(unittest.TestCase):
    """_git_comment_for_known_repo: scope is the ROOT's repo, or nothing."""

    ISSUE = {"id": ROOT_ID, "pubkey": ISSUE_AUTHOR, "kind": 1621, "created_at": 1,
             "content": "bug", "tags": [["a", REPO_ADDR], ["subject", "bug"]], "sig": "0" * 128}

    def test_a_comment_on_a_known_repos_issue_is_accepted(self):
        self.assertTrue(_known(self.ISSUE, announced={REPO_ADDR}))

    def test_an_unknown_root_is_refused(self):
        # The root not being on this relay means we cannot scope the comment to any repo —
        # accepting it anyway would reopen the spam firehose the repo-scoping exists to close.
        self.assertFalse(_known(None, announced={REPO_ADDR}))

    def test_a_root_on_an_unannounced_repo_is_refused(self):
        self.assertFalse(_known(self.ISSUE, announced=frozenset()))

    def test_a_kind1_root_is_refused_even_when_stored(self):
        # A stranger must not smuggle past the WoT gate by E-tagging someone's NOTE and stamping
        # K=1621 on the comment — the stored root's real kind is what counts.
        note = dict(self.ISSUE, kind=1)
        self.assertFalse(_known(note, announced={REPO_ADDR}))


class BothGatesAreWired(unittest.TestCase):
    """The helpers above decide; these pin that BOTH ingest paths actually ask them. A helper test
    alone would stay green with the branch deleted — which is exactly a silent regression to
    'gitworkshop users' comments are dropped'."""

    def test_the_ws_write_path_has_the_branch(self):
        src = SRC_SERVER.read_text(encoding="utf-8")
        m = re.search(r"elif kind == 1111 and \(_groot := _git_comment_root\(ev\)\).*?"
                      r"_git_comment_for_known_repo\(_groot\)", src, re.S)
        self.assertTrue(m, "server._on_event lost its git-comment acceptance branch")

    def test_the_firehose_has_the_branch(self):
        src = SRC_THREAD.read_text(encoding="utf-8")
        m = re.search(r"elif _kind == 1111 and \(_groot := _git_comment_root\(ev\)\)", src)
        self.assertTrue(m, "the firehose lost its git-comment acceptance branch")
        self.assertIn("from .server import RelayServer, _git_comment_root", src)


if __name__ == "__main__":
    unittest.main()
