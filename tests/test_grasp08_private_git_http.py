"""GRASP-08 on the GIT HTTP side: the announcement decides privacy too, not only the disk flag.

Before GRASP-08, "private" on this host meant "created with private=true and therefore never
announced" — `app/routers/git.py` refuses to publish a 30617 for such a repo. GRASP-08 inverts that:
a private repo IS announced, tagged ["private","true"], and clients publish its events only to the
relays that announcement names. So the two models can disagree about one repository, and the disk
flag is the half that can be wrong: `POST /<id>.git/create` defaults `private` to false, so an ngit
v3 user who provisions a private repo here and announces it privately ends up with a repo that is
private in its announcement and WORLD-CLONABLE over HTTP. Nothing logs it, because from the host's
side every one of those reads is a legitimate read of a public repo.

The gate is therefore a UNION of the two signals. These tests run the shipped `_read_gate_ok` and
`_announced_private`, and the shipped `git_auth` loader against a stub cursor holding REAL signed
events.
"""
from __future__ import annotations

import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.services import git_auth                      # noqa: E402
from app.services.nostr import bip340                  # noqa: E402
from app.services.nostr.event import build_event       # noqa: E402

import git_host_main as gh                             # noqa: E402
from app.services import git_host_service as ghs       # noqa: E402

OWNER_SK = (11).to_bytes(32, "big")
RANDO_SK = (33).to_bytes(32, "big")
OWNER = bip340.pubkey_from_seckey(OWNER_SK).hex()
RANDO = bip340.pubkey_from_seckey(RANDO_SK).hex()
REPO = "demo"


def announcement(sk=OWNER_SK, *, private=True, repo=REPO):
    tags = [["d", repo]]
    if private:
        tags.append(["private", "true"])
    return build_event(sk, git_auth.ANNOUNCE_KIND, "", tags=tags)


class _Cur:
    def __init__(self, rows):
        self._rows = rows

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, *a):
        pass

    def fetchall(self):
        return self._rows


class _Conn:
    """Just enough of a psycopg2 connection for git_auth's one indexed read."""

    def __init__(self, events):
        import json
        self._rows = [(json.dumps(e),) for e in events]
        self.autocommit = False

    def cursor(self):
        return _Cur(self._rows)

    def close(self):
        pass


# --------------------------------------------------------------------- the tag predicate

def test_event_says_private_reads_the_grasp08_tag():
    assert git_auth.event_says_private(announcement()) is True
    assert git_auth.event_says_private(announcement(private=False)) is False


@pytest.mark.parametrize("value", ["TRUE", "True", " true "])
def test_the_private_marker_is_not_case_or_space_sensitive(value):
    """The tag is written by whichever client announced the repo, not by us, so its exact spelling
    is not ours to assume."""
    ev = {"kind": 30617, "pubkey": OWNER, "tags": [["private", value]]}
    assert git_auth.event_says_private(ev) is True


def test_a_non_true_private_tag_is_not_private():
    ev = {"kind": 30617, "pubkey": OWNER, "tags": [["private", "false"]]}
    assert git_auth.event_says_private(ev) is False


def test_the_relay_and_the_git_host_read_the_tag_through_the_SAME_predicate():
    """A repo whose bytes are refused while its metadata is served is not private. Two hand-written
    copies of "does this announcement say private" is how the two doors drift apart."""
    from app.services.nostr_relay.server import RelayServer
    ev = announcement()
    assert RelayServer._is_private_repo_event(ev) is git_auth.event_says_private(ev) is True


# --------------------------------------------------------------------- the Postgres loader

def test_load_announced_private_reads_the_owners_own_announcement():
    assert git_auth.load_announced_private(_Conn([announcement()]), OWNER, REPO) is True
    assert git_auth.load_announced_private(_Conn([announcement(private=False)]), OWNER, REPO) is False


def test_a_stranger_cannot_announce_someone_elses_repo_private():
    """Same ACL reasoning as load_maintainers: only `pubkey = owner` counts, because a 30617 from
    another key addresses a DIFFERENT coordinate. Otherwise anybody could take a repo offline."""
    assert git_auth.load_announced_private(_Conn([announcement(RANDO_SK)]), OWNER, REPO) is False


def test_a_tampered_announcement_is_not_trusted():
    """Never trust the DB row's mere presence — the signature is re-verified here."""
    bad = announcement()
    bad["tags"] = [["d", REPO]]        # id/sig now describe different tags
    assert git_auth.load_announced_private(_Conn([bad]), OWNER, REPO) is False


def test_no_announcement_at_all_is_not_private():
    assert git_auth.load_announced_private(_Conn([]), OWNER, REPO) is False


# --------------------------------------------------------------------- the union in the read gate

def _handler():
    h = object.__new__(gh._Handler)
    h.headers = {}
    return h


@pytest.fixture
def cfg(monkeypatch):
    monkeypatch.setattr(gh, "_CONFIG", {"pg_dsn": "", "read_skew": 60, "port": 0}, raising=False)
    gh._priv_cache.clear()
    yield
    gh._priv_cache.clear()


def test_a_repo_private_only_by_its_announcement_is_refused(monkeypatch, cfg):
    """THE BUG THIS EXISTS FOR: private=false on disk, ["private","true"] in the 30617."""
    monkeypatch.setattr(ghs, "repo_private_meta", lambda o, r: {"private": False, "readers": []})
    monkeypatch.setattr(gh._Handler, "_announced_private", lambda self, o, r: True)
    assert _handler()._read_gate_ok(OWNER, REPO) is False


def test_a_repo_private_only_by_its_disk_flag_is_still_refused(monkeypatch, cfg):
    """A union, never an intersection — an unannounced private repo must not become public because
    no 30617 says otherwise."""
    monkeypatch.setattr(ghs, "repo_private_meta", lambda o, r: {"private": True, "readers": []})
    monkeypatch.setattr(gh._Handler, "_announced_private", lambda self, o, r: False)
    assert _handler()._read_gate_ok(OWNER, REPO) is False


def test_a_public_repo_is_still_served_anonymously(monkeypatch, cfg):
    monkeypatch.setattr(ghs, "repo_private_meta", lambda o, r: {"private": False, "readers": []})
    monkeypatch.setattr(gh._Handler, "_announced_private", lambda self, o, r: False)
    assert _handler()._read_gate_ok(OWNER, REPO) is True


def test_a_locally_private_repo_costs_no_announcement_read(monkeypatch, cfg):
    """`or` short-circuits. The DB read is for repos the disk flag calls public."""
    monkeypatch.setattr(ghs, "repo_private_meta", lambda o, r: {"private": True, "readers": []})

    def _boom(self, o, r):
        raise AssertionError("the announcement must not be read for an already-private repo")

    monkeypatch.setattr(gh._Handler, "_announced_private", _boom)
    assert _handler()._read_gate_ok(OWNER, REPO) is False


# --------------------------------------------------------------------- _announced_private itself

def test_no_dsn_means_there_is_no_announcement_to_consult(cfg):
    """A node with no relay database holds no 30617 at all, so this is "nothing to read", not "a
    read that failed" — the same reading `_maintainers` takes when it falls back to {owner}."""
    assert _handler()._announced_private(OWNER, REPO) is False


def test_a_database_we_cannot_ask_is_answered_PRIVATE(monkeypatch, cfg):
    """Fail-closed, the stance `_read_gate_ok`/`_is_wot_member`/`repo_private_meta` already take:
    "I could not ask whether this repo is private" is not "this repo is public"."""
    monkeypatch.setitem(gh._CONFIG, "pg_dsn", "host=127.0.0.1 port=1 dbname=nope connect_timeout=1")
    assert _handler()._announced_private(OWNER, REPO) is True


def test_a_failed_read_is_never_cached(monkeypatch, cfg):
    """Caching the failure would turn a one-second database blip into a full TTL of 401s for every
    public repo on the node."""
    monkeypatch.setitem(gh._CONFIG, "pg_dsn", "host=127.0.0.1 port=1 dbname=nope connect_timeout=1")
    assert _handler()._announced_private(OWNER, REPO) is True
    assert not gh._priv_cache


def test_the_answer_is_cached_so_a_clone_costs_one_indexed_read(monkeypatch, cfg):
    calls = []

    class _FakePG:
        @staticmethod
        def connect(dsn, connect_timeout=None):
            calls.append(dsn)
            return _Conn([announcement()])

    monkeypatch.setitem(gh._CONFIG, "pg_dsn", "dsn")
    monkeypatch.setitem(sys.modules, "psycopg2", _FakePG)
    h = _handler()
    assert h._announced_private(OWNER, REPO) is True
    assert h._announced_private(OWNER, REPO) is True
    assert len(calls) == 1, "the second request re-queried Postgres"


# --------------------------------------------------------------------- the REAL on-disk shapes
#
# Fixtures below are the two grasp.json shapes actually hosted on this deployment, read off
# /var/lib/posterchanai/git_repos/774ae7f8…/ rather than invented:
#
#   privrepo.git  {"private": true,  "readers": ["421f5fc9…"], "announcement_addr": ""}
#   pubrepo.git   {"private": false, "readers": [],            "announcement_addr": "30617:774ae7f8…:pubrepo"}
#
# THE ONLY PRIVATE REPO WE HOST HAS NO KIND-30617 AT ALL. Our model makes a repo private by never
# announcing it — `app/routers/git.py` refuses to announce one and `create_repo` writes an EMPTY
# announcement_addr — which leaks strictly LESS than GRASP-08's (no name, no maintainer set, no
# activity on any relay), and lacks only discoverability, which is what kind 10318 is for. So the
# union must never read "no announcement" as "not private", and nothing here starts announcing:
# publishing a 30617 for a repo that has none today would CREATE the metadata this model withholds.

@pytest.fixture
def repo_store(tmp_path, monkeypatch):
    monkeypatch.setattr(ghs, "GIT_PROJECT_ROOT", str(tmp_path), raising=False)
    return tmp_path


READER = "421f5fc9a21065445c96fdb91c0c1e2f2431741c72713b4b99ddcb316f31e9fc"


def test_our_own_unannounced_private_repo_stays_private(repo_store, monkeypatch, cfg):
    """privrepo's real shape: private=true, a readers list, announcement_addr "". There is no 30617
    to consult, so the announcement half answers False and the DISK flag must still carry it."""
    r = ghs.create_repo(OWNER, "privrepo", private=True, readers=[READER])
    assert r.get("ok")
    meta = ghs.repo_private_meta(OWNER, "privrepo")
    assert meta["private"] is True and meta["readers"] == [READER], meta
    monkeypatch.setattr(gh._Handler, "_announced_private", lambda self, o, r_: False)
    assert _handler()._read_gate_ok(OWNER, "privrepo") is False


def test_the_readers_list_survives_the_union_and_still_admits_a_reader(repo_store, monkeypatch, cfg):
    """`readers` has no GRASP-08 equivalent — it is per-repo and finer grained than the maintainer
    set — and the union must leave it exactly where it was: additive to the access set."""
    import base64
    import json as _json
    reader_sk = (44).to_bytes(32, "big")
    reader_hex = bip340.pubkey_from_seckey(reader_sk).hex()
    ghs.create_repo(OWNER, "privrepo", private=True, readers=[reader_hex])
    ev = build_event(reader_sk, git_auth.NIP98_KIND, "",
                     tags=[["u", "https://x/git/%s/privrepo.git/info/refs" % OWNER], ["method", "GET"]])
    h = _handler()
    h.headers = {"Authorization": "Nostr " + base64.b64encode(_json.dumps(ev).encode()).decode()}
    monkeypatch.setattr(gh._Handler, "_announced_private", lambda self, o, r_: False)
    assert h._read_gate_ok(OWNER, "privrepo") is True


def test_our_own_announced_public_repo_is_still_anonymous(repo_store, monkeypatch, cfg):
    """pubrepo's real shape: private=false and a populated announcement_addr. A 30617 exists and
    says nothing about privacy, so nothing changes for it."""
    ghs.create_repo(OWNER, "pubrepo", private=False)
    assert ghs.repo_private_meta(OWNER, "pubrepo")["private"] is False
    monkeypatch.setattr(gh._Handler, "_announced_private", lambda self, o, r_: False)
    assert _handler()._read_gate_ok(OWNER, "pubrepo") is True


def test_unreadable_metadata_on_an_existing_repo_is_still_deny_by_default(repo_store, monkeypatch, cfg):
    """repo_private_meta's own fail-closed rule — dir exists, metadata indeterminate -> private —
    must come through the union untouched. It is the older guard and the union sits on top of it."""
    ghs.create_repo(OWNER, "pubrepo", private=False)
    monkeypatch.setattr(ghs, "repo_private_meta",
                        lambda o, r_: {"private": True, "readers": []})   # what an unreadable one returns
    monkeypatch.setattr(gh._Handler, "_announced_private", lambda self, o, r_: False)
    assert _handler()._read_gate_ok(OWNER, "pubrepo") is False
