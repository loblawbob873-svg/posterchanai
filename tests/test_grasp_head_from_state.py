"""GRASP-01: "MUST set repository HEAD per repo state announcement as soon as the git data related
to that branch has been received."

We never read it. `adopt_head_if_unborn` picked `main`, then `master`, then first-alphabetically BY
CONVENTION — so a project whose declared default branch is `develop` got `master`, a confident wrong
answer handed to every reader that asks a repo for its default: the web Git UI's browse ref, the
30618 witness we publish back, and `git clone`'s symref advertisement.

These build a real bare repo with real branches and run the shipped functions against a stub
Postgres holding REAL signed 30617/30618 events.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.services import git_auth, git_host_service as ghs   # noqa: E402
from app.services.nostr import bip340                        # noqa: E402
from app.services.nostr.event import build_event             # noqa: E402

OWNER_SK = (11).to_bytes(32, "big")
RANDO_SK = (33).to_bytes(32, "big")
OWNER = bip340.pubkey_from_seckey(OWNER_SK).hex()
REPO = "demo"


def state(sk=OWNER_SK, head="refs/heads/develop", refs=(), created_at=None):
    tags = [["d", REPO]]
    if head is not None:
        tags.append(["HEAD", "ref: " + head] if head else ["HEAD", ""])
    for name, sha in refs:
        tags.append([name, sha])
    tags.append(["a", "30617:%s:%s" % (OWNER, REPO)])
    return build_event(sk, git_auth.STATE_KIND, "", tags=tags, created_at=created_at)


class _Cur:
    def __init__(self, conn):
        self._conn, self._rows = conn, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if "FROM events" not in sql:
            self._rows = []
            return
        repo_id, kind, who = params
        if kind == git_auth.STATE_KIND:
            keep = [e for e in self._conn.events
                    if e["kind"] == kind and e["pubkey"] in set(who)]
        else:
            keep = [e for e in self._conn.events if e["kind"] == kind and e["pubkey"] == who]
        keep = [e for e in keep if any(t[:2] == ["d", repo_id] for t in e["tags"])]
        keep.sort(key=lambda e: -e["created_at"])
        self._rows = [(json.dumps(e),) for e in keep]

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self, events):
        self.events = list(events)
        self.autocommit = False

    def cursor(self):
        return _Cur(self)

    def close(self):
        pass


def _unhook(d):
    """Seed a repo WITHOUT going through push authorization. `create_repo` installs the pre-receive
    hook, which (correctly) refuses a push with no signed 30618 — this file is testing what HEAD
    becomes, not who may write, and the hook has its own tests."""
    import glob
    for h in glob.glob(os.path.join(d, "hooks", "*")):
        os.remove(h)


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real bare repo with `master` and `develop`, in an isolated store."""
    monkeypatch.setenv("GRASP_GIT_PROJECT_ROOT", str(tmp_path))
    assert ghs.create_repo(OWNER, REPO).get("ok")
    d = ghs.repo_dir(OWNER, REPO)
    _unhook(d)
    wt = tmp_path / "wt"
    run = lambda *a, **k: subprocess.run(a, check=True, capture_output=True, cwd=k.get("cwd"))
    run("git", "init", "-q", "-b", "master", str(wt))
    run("git", "-C", str(wt), "config", "user.email", "t@e.st")
    run("git", "-C", str(wt), "config", "user.name", "t")
    (wt / "f").write_text("x")
    run("git", "-C", str(wt), "add", "f")
    run("git", "-C", str(wt), "commit", "-qm", "one")
    run("git", "-C", str(wt), "branch", "develop")
    run("git", "-C", str(wt), "push", "-q", d, "master", "develop")
    return d


def use(conn, monkeypatch):
    monkeypatch.setenv("GRASP_PG_DSN", "stub")
    monkeypatch.setitem(sys.modules, "psycopg2",
                        type("_PG", (), {"connect": staticmethod(lambda *a, **k: conn)}))


def test_the_declared_HEAD_is_used_instead_of_the_convention(repo, monkeypatch):
    """THE BUG. `master` exists and is conventional; the signed state says `develop`."""
    use(_Conn([state()]), monkeypatch)
    assert ghs.adopt_head_if_unborn(OWNER, REPO) == "refs/heads/develop"
    assert ghs.repo_head(OWNER, REPO) == "refs/heads/develop"


def test_it_OVERRIDES_an_already_born_head(repo, monkeypatch):
    """A maintainer-signed state event is an instruction, not a hint — and it is the only way a
    project can change its default branch here, since no endpoint sets one."""
    assert ghs.repo_head(OWNER, REPO) == "refs/heads/master"
    use(_Conn([state()]), monkeypatch)
    assert ghs.adopt_head_if_unborn(OWNER, REPO) == "refs/heads/develop"


def test_a_declared_branch_we_do_NOT_have_is_ignored(repo, monkeypatch):
    """"as soon as the git data related to that branch has been received" is load-bearing: pointing
    HEAD at a branch we do not hold reproduces exactly the unborn-HEAD bug this module already fixed
    once, where every reader asking for the default got a dead ref."""
    use(_Conn([state(head="refs/heads/nope")]), monkeypatch)
    ghs.adopt_head_if_unborn(OWNER, REPO)
    assert ghs.repo_head(OWNER, REPO) == "refs/heads/master"


def test_a_STRANGERS_state_event_cannot_move_HEAD(repo, monkeypatch):
    """Read through the same primitives the push hook authorizes with: the recursive maintainer set,
    then `select_authorized_state`, which re-verifies the signature here."""
    use(_Conn([state(sk=RANDO_SK)]), monkeypatch)
    ghs.adopt_head_if_unborn(OWNER, REPO)
    assert ghs.repo_head(OWNER, REPO) == "refs/heads/master"


def test_a_tampered_state_event_cannot_move_HEAD(repo, monkeypatch):
    bad = state()
    bad["tags"] = [["d", REPO], ["HEAD", "ref: refs/heads/develop"], ["x", "y"]]
    use(_Conn([bad]), monkeypatch)
    ghs.adopt_head_if_unborn(OWNER, REPO)
    assert ghs.repo_head(OWNER, REPO) == "refs/heads/master"


def test_the_NEWEST_signed_state_wins(repo, monkeypatch):
    import time as _t
    now = int(_t.time())
    use(_Conn([state(head="refs/heads/master", created_at=now - 100),
               state(head="refs/heads/develop", created_at=now)]), monkeypatch)
    assert ghs.adopt_head_if_unborn(OWNER, REPO) == "refs/heads/develop"


def test_a_bare_refname_is_accepted_as_well_as_the_ref_prefix(repo, monkeypatch):
    """The tag is written by whichever client signed the state; NIP-34 spells it `ref: refs/heads/x`
    but a bare refname is the same instruction."""
    ev = state()
    ev = build_event(OWNER_SK, git_auth.STATE_KIND, "",
                     tags=[["d", REPO], ["HEAD", "refs/heads/develop"],
                           ["a", "30617:%s:%s" % (OWNER, REPO)]])
    use(_Conn([ev]), monkeypatch)
    assert ghs.adopt_head_if_unborn(OWNER, REPO) == "refs/heads/develop"


def test_a_HEAD_tag_that_is_not_a_branch_is_refused(repo, monkeypatch):
    """`symbolic-ref HEAD <anything>` would take a tag or a path; only refs/heads/* is a default
    branch, and `..` must never reach a git argument."""
    for bad in ("refs/tags/v1", "refs/heads/../../etc", "HEAD"):
        use(_Conn([state(head=bad)]), monkeypatch)
        ghs.adopt_head_if_unborn(OWNER, REPO)
        assert ghs.repo_head(OWNER, REPO) == "refs/heads/master", bad


def test_no_state_event_leaves_the_convention_in_charge(repo, monkeypatch):
    use(_Conn([]), monkeypatch)
    assert ghs.head_from_state(OWNER, REPO) == ""
    assert ghs.adopt_head_if_unborn(OWNER, REPO) == ""     # master is born; nothing to adopt


def test_an_unreachable_database_leaves_the_convention_in_charge(repo, monkeypatch):
    """HEAD is metadata. Refusing to serve a repo because we could not read a preference would be a
    far worse failure than a stale default — this is the one decision on the git host that is
    deliberately best-effort rather than fail-closed."""
    class _Boom:
        @staticmethod
        def connect(*a, **k):
            raise OSError("connection refused")

    monkeypatch.setenv("GRASP_PG_DSN", "stub")
    monkeypatch.setitem(sys.modules, "psycopg2", _Boom)
    assert ghs.head_from_state(OWNER, REPO) == ""


def test_the_unborn_HEAD_convention_still_works(tmp_path, monkeypatch):
    """The half this function is named for. A repo whose first push is `main` is left by
    `git init --bare` pointing at a `master` that does not exist."""
    monkeypatch.setenv("GRASP_GIT_PROJECT_ROOT", str(tmp_path))
    monkeypatch.delenv("GRASP_PG_DSN", raising=False)
    ghs.create_repo(OWNER, "fresh")
    d = ghs.repo_dir(OWNER, "fresh")
    _unhook(d)
    wt = tmp_path / "wt2"
    subprocess.run(["git", "init", "-q", "-b", "main", str(wt)], check=True, capture_output=True)
    for a in (["config", "user.email", "t@e.st"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(wt)] + a, check=True, capture_output=True)
    (wt / "f").write_text("x")
    subprocess.run(["git", "-C", str(wt), "add", "f"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-qm", "one"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(wt), "push", "-q", d, "main"], check=True, capture_output=True)
    assert ghs.adopt_head_if_unborn(OWNER, "fresh") == "refs/heads/main"
