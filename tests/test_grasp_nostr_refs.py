"""GRASP-01: `refs/nostr/<event-id>` — the transport a pull request's CODE travels over.

> "MUST accept pushes via this service to `refs/nostr/<event-id>` but SHOULD reject if event exists
> on relay listing a different tip … SHOULD delete and MAY garbage collect these refs if no
> corresponding git PR event or git PR update event, with a `c` tag that matches the ref tip, is
> accepted by relay with 20 minutes."

`grep -rn "refs/nostr" --include=*.py .` returned NOTHING, and `decide_push_ref` had exactly one
accept path for a non-delete ref: the ref must be named by the signed 30618 with a matching SHA. A PR
comes by definition from somebody who is NOT a maintainer and can never appear in a signed state, so
every such push died as "refs/nostr/<id> is not present in the signed 30618 state". Contribution did
not work at all — and this is the half that matters even once the relay accepts kind 1618, because a
pull request with no objects is a description of code nobody can fetch.

The decision is a SEPARATE function from the 30618 path on purpose: that path is the security crux
and is not loosened to make room for this one.
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

SK = (11).to_bytes(32, "big")
CONTRIB_SK = (77).to_bytes(32, "big")
OWNER = bip340.pubkey_from_seckey(SK).hex()
REPO = "demo"
SHA_A, SHA_B = "a" * 40, "b" * 40
EID = "e" * 64                      # a well-formed id no event has — for the name-shape tests
NREF = "refs/nostr/" + EID
UNCLAIMED_ID = "f" * 64


def pr(tips=(SHA_A,), kind=1618, sk=CONTRIB_SK):
    """A REALLY SIGNED PR event. Its id cannot be chosen — the reaper re-verifies every event it
    reads, so a fixture that overwrote `id` would be silently discarded and the test would "pass" by
    deleting the ref it meant to save. The ref name is derived from the event instead."""
    return build_event(sk, kind, "", tags=[["a", "30617:%s:%s" % (OWNER, REPO)]] +
                       [["c", t] for t in tips])


# ------------------------------------------------------------------ the ref name

def test_only_a_64_hex_event_id_is_a_nostr_ref():
    """A loose pattern would make `refs/nostr/*` a free-form namespace anybody can push anything
    into — the suffix IS the PR event's id and nothing else."""
    assert git_auth.nostr_ref_event_id(NREF) == EID
    for bad in ("refs/nostr/", "refs/nostr/xyz", "refs/nostr/" + "E" * 64,
                "refs/nostr/" + "e" * 63, "refs/nostr/%s/extra" % EID, "refs/heads/main"):
        assert git_auth.nostr_ref_event_id(bad) is None, bad


# ------------------------------------------------------------------ the push decision

def test_the_old_code_path_rejected_every_one_of_these():
    """The regression this fixes, stated as the contract it replaces: a refs/nostr push carries a SHA
    no 30618 will ever name."""
    ok, why = git_auth.decide_push_ref(NREF, "0" * 40, SHA_A, {OWNER}, [], pr_event=None)
    assert ok, why
    assert "30618" not in why


def test_a_push_with_no_PR_event_yet_is_accepted_provisionally():
    """Not laxity — ORDERING. ngit pushes the objects and publishes the event around the same
    moment; a server demanding the event first would deadlock against a client that pushes first.
    The 20-minute reaper is what makes the window bounded instead of open."""
    ok, why = git_auth.decide_nostr_ref(NREF, SHA_A, None)
    assert ok and "reaper" in why


def test_a_tip_the_PR_event_claims_is_accepted():
    ok, _ = git_auth.decide_nostr_ref(NREF, SHA_A, pr(tips=(SHA_A,)))
    assert ok


def test_a_tip_the_PR_event_does_NOT_claim_is_REJECTED():
    """"SHOULD reject if event exists on relay listing a different tip"."""
    ok, why = git_auth.decide_nostr_ref(NREF, SHA_B, pr(tips=(SHA_A,)))
    assert not ok and "c tags" in why


def test_a_PR_update_event_counts_too():
    ok, _ = git_auth.decide_nostr_ref(NREF, SHA_A, pr(tips=(SHA_A,), kind=1619))
    assert ok


def test_an_event_of_the_wrong_kind_authorizes_nothing():
    """The ref names an id; anybody can publish any kind under some id. Only 1618/1619 is a PR."""
    ok, why = git_auth.decide_nostr_ref(NREF, SHA_A, pr(tips=(SHA_A,), kind=1))
    assert not ok and "not a pull request" in why


def test_deleting_a_nostr_ref_is_always_allowed():
    ok, _ = git_auth.decide_nostr_ref(NREF, "0" * 40, pr(tips=(SHA_B,)))
    assert ok


def test_the_30618_PATH_IS_NOT_LOOSENED():
    """The whole reason this is a separate door. An ordinary branch still needs a maintainer-signed
    state naming its exact SHA, and a PR event must not be able to authorize one."""
    ok, why = git_auth.decide_push_ref("refs/heads/main", "0" * 40, SHA_A, {OWNER}, [],
                                       pr_event=pr(tips=(SHA_A,)))
    assert not ok and "30618" in why


def test_c_tags_are_read_as_ngit_v3_writes_them():
    """ngit v3: "PR events (kind 1618/1619) use `c` tags for commit IDs", matching NIP-34's
    ["c", "<current-commit-id>"]."""
    assert git_auth.pr_commit_tips(pr(tips=(SHA_A, SHA_B))) == {SHA_A, SHA_B}
    assert git_auth.pr_commit_tips({"tags": [["c", "not-a-sha"], ["e", SHA_A]]}) == set()


# ------------------------------------------------------------------ the reaper

class _Cur:
    def __init__(self, conn):
        self._conn, self._rows = conn, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        if "id = ANY" in sql:
            ids, kinds = params
            self._rows = [(json.dumps(e),) for e in self._conn.events
                          if e["id"] in ids and e["kind"] in kinds]
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self, events=()):
        self.events = list(events)
        self.autocommit = False

    def cursor(self):
        return _Cur(self)

    def close(self):
        pass


@pytest.fixture
def repo(tmp_path, monkeypatch):
    """A real bare repo with one commit and two refs/nostr refs pointing at it: one named after a
    REAL PR event's id, one after an id no event has."""
    import glob
    monkeypatch.setenv("GRASP_GIT_PROJECT_ROOT", str(tmp_path))
    assert ghs.create_repo(OWNER, REPO).get("ok")
    d = ghs.repo_dir(OWNER, REPO)
    for h in glob.glob(os.path.join(d, "hooks", "*")):
        os.remove(h)
    wt = tmp_path / "wt"
    subprocess.run(["git", "init", "-q", "-b", "main", str(wt)], check=True, capture_output=True)
    for a in (["config", "user.email", "t@e.st"], ["config", "user.name", "t"]):
        subprocess.run(["git", "-C", str(wt)] + a, check=True, capture_output=True)
    (wt / "f").write_text("x")
    subprocess.run(["git", "-C", str(wt), "add", "f"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(wt), "commit", "-qm", "one"], check=True, capture_output=True)
    subprocess.run(["git", "-C", str(wt), "push", "-q", d, "main"], check=True, capture_output=True)
    sha = subprocess.run(["git", "-C", str(wt), "rev-parse", "HEAD"],
                         check=True, capture_output=True, text=True).stdout.strip()
    claimed = pr(tips=(sha,))
    for eid in (claimed["id"], UNCLAIMED_ID):
        subprocess.run(["git", "--git-dir", d, "update-ref", "refs/nostr/" + eid, sha],
                       check=True, capture_output=True)
    return d, sha, claimed


def test_an_unclaimed_ref_past_the_grace_is_deleted(repo, monkeypatch):
    d, sha, _claimed = repo
    monkeypatch.setenv("GRASP_PG_DSN", "stub")
    r = ghs.reap_nostr_refs(conn=_Conn([]), grace=0)
    assert r["deleted"] == 2, r
    assert ghs.nostr_refs(OWNER, REPO) == {}


def test_a_CLAIMED_ref_survives(repo, monkeypatch):
    d, sha, claimed = repo
    monkeypatch.setenv("GRASP_PG_DSN", "stub")
    r = ghs.reap_nostr_refs(conn=_Conn([claimed]), grace=0)
    assert r["kept"] == 1 and r["deleted"] == 1, r
    assert list(ghs.nostr_refs(OWNER, REPO)) == ["refs/nostr/" + claimed["id"]]


def test_a_PR_event_naming_a_DIFFERENT_tip_does_not_save_the_ref(repo, monkeypatch):
    """"with a `c` tag that matches the ref tip" — an event about some other commit claims nothing."""
    d, sha, claimed = repo
    monkeypatch.setenv("GRASP_PG_DSN", "stub")
    # the SAME event id, but its `c` tag names a different commit
    other = build_event(CONTRIB_SK, 1618, "", tags=[["c", SHA_B]])
    subprocess.run(["git", "--git-dir", d, "update-ref", "refs/nostr/" + other["id"], sha],
                   check=True, capture_output=True)
    r = ghs.reap_nostr_refs(conn=_Conn([other]), grace=0)
    assert r["deleted"] == 3 and r["kept"] == 0, r


def test_a_ref_INSIDE_the_grace_is_never_touched(repo, monkeypatch):
    """The grace is the spec's 20 minutes and deleting early throws away a contribution whose PR
    event is still in flight."""
    monkeypatch.setenv("GRASP_PG_DSN", "stub")
    r = ghs.reap_nostr_refs(conn=_Conn([]), grace=ghs.NOSTR_REF_GRACE_SECONDS)
    assert r["deleted"] == 0 and r["kept"] == 2, r
    assert len(ghs.nostr_refs(OWNER, REPO)) == 2


def test_the_grace_is_the_specs_twenty_minutes():
    assert ghs.NOSTR_REF_GRACE_SECONDS == 20 * 60


def test_a_database_we_cannot_ask_KEEPS_every_ref(repo, monkeypatch):
    """FAIL-CLOSED HERE MEANS KEEP. We cannot tell an unclaimed ref from a claimed one without the
    relay, and deleting on "I could not ask" would throw away contributors' work every time Postgres
    blinked. Deletion needs positive evidence of absence — the same rule the folder-sync deletion
    guard and the Blossom store scan already state."""
    class _Boom:
        @staticmethod
        def connect(*a, **k):
            raise OSError("connection refused")

    monkeypatch.setenv("GRASP_PG_DSN", "stub")
    monkeypatch.setitem(sys.modules, "psycopg2", _Boom)
    r = ghs.reap_nostr_refs(grace=0)
    assert r["deleted"] == 0
    assert len(ghs.nostr_refs(OWNER, REPO)) == 2


def test_no_relay_dsn_keeps_every_ref(repo, monkeypatch):
    monkeypatch.delenv("GRASP_PG_DSN", raising=False)
    r = ghs.reap_nostr_refs(grace=0)
    assert r["deleted"] == 0
    assert len(ghs.nostr_refs(OWNER, REPO)) == 2


def test_ordinary_branches_are_never_swept(repo, monkeypatch):
    monkeypatch.setenv("GRASP_PG_DSN", "stub")
    ghs.reap_nostr_refs(conn=_Conn([]), grace=0)
    assert "refs/heads/main" in ghs.repo_refs(OWNER, REPO)


def test_ref_age_comes_from_the_commit_date_not_the_file_mtime(repo, monkeypatch):
    """A `git gc` rewrites packed-refs and would reset every mtime at once, silently granting the
    whole namespace a fresh 20 minutes. `%(creatordate:unix)` is the object's date, which nothing on
    this side rewrites."""
    d, sha, _claimed = repo
    subprocess.run(["git", "--git-dir", d, "pack-refs", "--all"], check=True, capture_output=True)
    ages = {n: age for n, (s, age) in ghs.nostr_refs(OWNER, REPO).items()}
    assert ages and all(a >= 0 for a in ages.values())
    monkeypatch.setenv("GRASP_PG_DSN", "stub")
    assert ghs.reap_nostr_refs(conn=_Conn([]), grace=0)["deleted"] == 2


def test_a_relay_that_dies_MID_SWEEP_still_keeps_every_ref(repo, monkeypatch):
    """The dangerous variant, and the one a connect-time test does not reach: the connection is fine
    and the QUERY fails. Swallowing that into "nothing claims this ref" deletes it — measured: with
    the failure caught inside the loop, an unclaimed-looking ref went `deleted=1, refs left=0`. The
    error has to escape to the outer guard, which keeps everything and says so."""
    class _Dies:
        autocommit = False

        def cursor(self):
            raise RuntimeError("relay unreachable mid-query")

        def close(self):
            pass

    monkeypatch.setenv("GRASP_PG_DSN", "stub")
    r = ghs.reap_nostr_refs(conn=_Dies(), grace=0)
    assert r["deleted"] == 0, r
    assert len(ghs.nostr_refs(OWNER, REPO)) == 2, "a mid-sweep failure deleted refs"
