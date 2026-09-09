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
    # `git_project_root()` is LAZY and reads GRASP_GIT_PROJECT_ROOT FIRST — it has to, so the
    # hook subprocesses resolve the same root the server served from. Assigning the module
    # attribute does NOTHING, and a store test that does it writes into the LIVE repo store.
    monkeypatch.setenv("GRASP_GIT_PROJECT_ROOT", str(tmp_path))
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


# --------------------------------------------------------------------- NIP-98 to GRASP-08 spec
#
# GRASP-08 spells the read credential out: repository-scoped, `method` tag GET, ONE credential
# covering every endpoint of a Smart HTTP operation, `created_at` within 60 seconds. Two of those
# three are now enforced; the third (`u` equal to the canonical repository URL) is deliberately NOT,
# and the last test in this block says why.

import base64                                          # noqa: E402
import json as _json                                   # noqa: E402
import time as _time                                   # noqa: E402

READER_SK = (55).to_bytes(32, "big")
READER_HEX = bip340.pubkey_from_seckey(READER_SK).hex()
_URL = "https://example.test/git/%s/privrepo.git/info/refs" % OWNER


def _nip98(method="GET", age=0, url=_URL, sk=READER_SK, basic=False):
    ev = build_event(sk, git_auth.NIP98_KIND, "", tags=[["u", url], ["method", method]],
                     created_at=int(_time.time()) - age)
    tok = base64.b64encode(_json.dumps(ev).encode()).decode()
    if basic:
        # The same signed token as the PASSWORD half of HTTP Basic, which is the only envelope
        # libgit2 can produce (it runs credential helpers, which return username/password).
        return "Basic " + base64.b64encode(("npub:" + tok).encode()).decode()
    return "Nostr " + tok


@pytest.fixture
def private_repo(repo_store, monkeypatch, cfg):
    ghs.create_repo(OWNER, "privrepo", private=True, readers=[READER_HEX])
    monkeypatch.setattr(gh._Handler, "_announced_private", lambda self, o, r_: False)

    def _gate(auth, **conf):
        gh._CONFIG.update(conf)
        h = _handler()
        h.headers = {"Authorization": auth}
        return h._read_gate_ok(OWNER, "privrepo")

    return _gate


def test_a_credential_whose_method_tag_is_not_GET_is_refused(private_repo):
    """GRASP-08 names the method: GET. It was not checked at all before (require_method=False), so a
    header minted for the write route — `<id>.git/edit`, method POST, whose `u` contains the read
    needle — was accepted as a read credential."""
    assert private_repo(_nip98(method="POST")) is False
    assert private_repo(_nip98(method="GET")) is True


def test_the_method_tag_is_compared_to_GET_not_to_this_REQUEST_s_verb(private_repo):
    """This is what makes the check safe to turn on, and it is GRASP-08's "one credential covering
    all endpoints of a Smart HTTP operation": a clone sends ONE static header for the info/refs GET
    and the upload-pack POST. Compared to the request's own verb, the second half of every clone
    would 401 — which is exactly why the check used to be off."""
    gh._priv_cache.clear()
    h = _handler()
    h.command = "POST"                      # the upload-pack POST, carrying the clone's GET token
    h.headers = {"Authorization": _nip98(method="GET")}
    gh._CONFIG.update({"read_skew": 60, "read_require_method": True})
    assert h._read_gate_ok(OWNER, "privrepo") is True


def test_the_freshness_window_is_60_seconds(private_repo):
    """GRASP-08's number. The old 300s window predates `scripts/git-credential-nostr`, which mints a
    fresh token per request and so never needed it."""
    assert private_repo(_nip98(age=30), read_skew=60) is True
    assert private_repo(_nip98(age=120), read_skew=60) is False


def test_60_seconds_is_the_DEFAULT_when_the_key_is_absent(repo_store, monkeypatch, cfg):
    """The subprocess's own fallback has to agree with `_read_config`'s default, or a host started
    from an older sidecar quietly keeps the 300s window."""
    ghs.create_repo(OWNER, "privrepo", private=True, readers=[READER_HEX])
    monkeypatch.setattr(gh._Handler, "_announced_private", lambda self, o, r_: False)
    gh._CONFIG.pop("read_skew", None)
    gh._CONFIG.pop("read_require_method", None)
    h = _handler()
    h.headers = {"Authorization": _nip98(age=120)}
    assert h._read_gate_ok(OWNER, "privrepo") is False


def test_an_operator_can_widen_the_window_without_patching_the_host(private_repo):
    """The looser values were not arbitrary: docs/GIT_OVER_NOSTR.md documents a HAND-MADE
    `http.extraHeader` reused across several commands as the working https read path, and 60s makes
    that a one-minute token. `git_server_read_skew` is the way back, so nobody edits the host."""
    assert private_repo(_nip98(age=120), read_skew=300) is True


def test_an_operator_can_also_turn_the_method_check_off(private_repo):
    assert private_repo(_nip98(method="POST"), read_require_method=False) is True


def test_the_BASIC_envelope_still_works(private_repo):
    """libgit2 only attempts a scheme the server advertises and gives up on `Nostr` alone rather
    than calling a credential helper — a constraint of the TRANSPORT, not of any ngit version, so a
    v3 client on it behaves the same. The "password" is the same signed NIP-98 event and every check
    on this path still applies to it; an ordinary password fails (tests/test_git_push_auth.py)."""
    assert private_repo(_nip98(basic=True)) is True
    assert private_repo(_nip98(method="POST", basic=True)) is False, "the envelope weakens nothing"


def test_the_u_tag_is_still_matched_as_a_substring_and_that_is_deliberate(private_repo):
    """NOT tightened to the canonical URL, and this test exists so the reason is recorded rather than
    rediscovered. Three legitimate readers an equality check refuses: the maintainer-alias path,
    where ngit derives one clone URL per maintainer key and the owner segment is NOT the hosting
    owner; a proxy node (or any node reached by another hostname), where `public_base` is empty or
    different; and the owner segment being accepted as npub OR hex. The binding is per-repo either
    way, and the ACL is per-repo, so this grants nothing across owners."""
    other_host = "http://nas.lan:3053/%s/privrepo.git/git-upload-pack" % OWNER
    assert private_repo(_nip98(url=other_host)) is True
    assert private_repo(_nip98(url="https://example.test/git/%s/other.git" % OWNER)) is False
