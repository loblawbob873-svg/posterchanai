"""GRASP-01: a repository exists because we ACCEPTED its announcement — the thing that made a stock
ngit v3 client unable to use us at all.

`ngit init` publishes the kind-30617 and then POLLS `info/refs` (`check_git_server_ready`,
ngit-cli 3.0.0 `src/lib/accept_maintainership.rs:401`) until the server answers, because accepting
the announcement IS the contract. There is no provisioning call anywhere in ngit — `grep -rn
provision src/` finds two comments. We only ever created repos from `POST …/create` + NIP-98 or a web
session, so `ngit init --grasp-server poster.place/git` sat at "waiting" and timed out for ever.

THE ACCEPTANCE POLICY IS THE HARD PART, and it is a policy, not a reading of the spec: GRASP-01
requires serving a repo for each accepted announcement and separately permits rejecting on quota /
payment / web-of-trust grounds, but says nothing about the resource question acceptance creates. It
leaves it to the operator to state in NIP-11 `repo_acceptance_criteria`. Accepting means allocating
disk to a remote party, so the default is narrow and every widening is a setting.

Everything below runs the SHIPPED handler against a temp repo store and a stub Postgres.
"""
from __future__ import annotations

import json
import os
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import git_host_main as gh                             # noqa: E402
from app.services import git_auth, git_host_service as ghs   # noqa: E402
from app.services.nostr import bip340, nostr_service   # noqa: E402
from app.services.nostr.event import build_event       # noqa: E402

OWNER_SK = (11).to_bytes(32, "big")
OWNER = bip340.pubkey_from_seckey(OWNER_SK).hex()
NPUB = nostr_service.npub_of(OWNER)
REPO = "demo"
BASE = "https://poster.place/git"


def announcement(clone=None, private=False, repo=REPO):
    tags = [["d", repo], ["clone", clone if clone is not None else "%s/%s/%s.git" % (BASE, NPUB, repo)]]
    if private:
        tags.append(["private", "true"])
    return build_event(OWNER_SK, git_auth.ANNOUNCE_KIND, "", tags=tags)


class _Cur:
    """Just enough Postgres: the announcement read, plus the two acceptance-policy lookups."""

    def __init__(self, conn):
        self._conn, self._rows = conn, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=None):
        self._conn.sql.append(sql)
        if "FROM events" in sql:
            _repo, _kind, pubkey = params
            self._rows = [(json.dumps(e),) for e in self._conn.events if e["pubkey"] == pubkey
                          and any(t[:2] == ["d", _repo] for t in e["tags"])]
        elif "FROM users" in sql:
            if self._conn.no_users_table:
                raise RuntimeError('relation "users" does not exist')
            self._rows = [(1,)] if params[0] in self._conn.local_npubs else []
        elif "FROM wot" in sql:
            self._rows = [(1,)] if params[0] in self._conn.wot else []
        else:
            self._rows = []

    def fetchall(self):
        return self._rows

    def fetchone(self):
        return self._rows[0] if self._rows else None


class _Conn:
    def __init__(self, events=(), wot=(), local_npubs=(), no_users_table=False):
        self.events, self.wot, self.local_npubs = list(events), set(wot), set(local_npubs)
        self.no_users_table, self.sql = no_users_table, []
        self.autocommit = False

    def cursor(self):
        return _Cur(self)

    def close(self):
        pass


@pytest.fixture
def host(tmp_path, monkeypatch):
    """A handler with a temp repo store; `probe()` runs the real `_autoprovision`."""
    # `git_project_root()` is LAZY and reads GRASP_GIT_PROJECT_ROOT first (it has to: the hook
    # subprocesses must resolve the same root the server served from). Setting a module attribute
    # does nothing — the store would be the real /var/lib one, which is how this fixture first
    # "passed" by finding a repo it never created.
    monkeypatch.setenv("GRASP_GIT_PROJECT_ROOT", str(tmp_path))
    monkeypatch.setattr(gh, "_CONFIG", {
        "pg_dsn": "stub", "public_base": BASE, "allowlist": "", "auto_provision": True,
        "accept_policy": "local-or-wot", "port": 0}, raising=False)
    gh._prov_deny.clear()
    state = {}

    def probe(conn, owner=OWNER, repo=REPO, **cfg):
        gh._CONFIG.update(cfg)
        gh._prov_deny.clear()
        state["conn"] = conn
        monkeypatch.setitem(sys.modules, "psycopg2",
                            type("_PG", (), {"connect": staticmethod(lambda *a, **k: conn)}))
        h = object.__new__(gh._Handler)
        h.headers = {}
        return h._autoprovision(owner, repo)

    yield probe
    gh._prov_deny.clear()


# ------------------------------------------------------------------ the happy path

def test_an_accepted_announcement_creates_the_bare_repo(host):
    """THE WHOLE POINT. This is the request ngit sits in a loop on."""
    assert ghs.repo_exists(OWNER, REPO) is False
    assert host(_Conn([announcement()], local_npubs=[NPUB])) is True
    assert ghs.repo_exists(OWNER, REPO) is True


def test_web_of_trust_membership_also_accepts(host):
    assert host(_Conn([announcement()], wot=[OWNER])) is True
    assert ghs.repo_exists(OWNER, REPO) is True


def test_provisioning_is_idempotent_and_never_clobbers(host):
    """`create_repo` re-applies private/readers config on EVERY call, so handing it an existing repo
    would flip a private repo public and wipe its readers — the same wipe `_serve_create` guards
    against, and the same class as the replaceable-list and blossom-whitelist bugs."""
    ghs.create_repo(OWNER, REPO, private=True, readers=["c" * 64])
    assert host(_Conn([announcement()], wot=[OWNER])) is True
    meta = ghs.repo_private_meta(OWNER, REPO)
    assert meta["private"] is True and meta["readers"] == ["c" * 64], meta


def test_a_private_announcement_provisions_a_private_repo(host):
    """GRASP-08 and GRASP-01 meeting: if the announcement we accepted says private, the repo we
    create from it must be, or we would serve its bytes to anyone until somebody noticed."""
    assert host(_Conn([announcement(private=True)], wot=[OWNER])) is True
    assert ghs.repo_private_meta(OWNER, REPO)["private"] is True


# ------------------------------------------------------------------ the acceptance policy

def test_a_stranger_gets_nothing(host):
    """The default is NOT "anybody who can publish a 30617 naming us" — that is an unauthenticated
    disk-allocation primitive, and the repo size caps are enforced at PUSH time, in the hook, so an
    empty-repo flood is bounded by nothing."""
    assert host(_Conn([announcement()])) is False
    assert ghs.repo_exists(OWNER, REPO) is False


def test_policy_wot_refuses_a_local_account_and_vice_versa(host):
    assert host(_Conn([announcement()], local_npubs=[NPUB]), accept_policy="wot") is False
    assert host(_Conn([announcement()], wot=[OWNER]), accept_policy="local") is False


def test_policy_any_accepts_a_stranger_because_an_operator_asked_for_that(host):
    assert host(_Conn([announcement()]), accept_policy="any") is True


def test_the_operator_allowlist_grants_under_every_policy(host):
    """A CI key or a fresh operator key is in no social graph and has no account; the allowlist is
    the only way such a key gets in at all."""
    assert host(_Conn([announcement()]), allowlist=NPUB, accept_policy="wot") is True


def test_a_missing_users_table_falls_back_to_wot_instead_of_failing(host):
    """The relay DSN need not be the app's database on every deployment. "This criterion cannot be
    evaluated" is not "denied" — the other half of the policy must still be able to answer."""
    assert host(_Conn([announcement()], wot=[OWNER], no_users_table=True)) is True


def test_auto_provisioning_can_be_turned_off_entirely(host):
    assert host(_Conn([announcement()], wot=[OWNER]), auto_provision=False) is False


# ------------------------------------------------------------------ what must NOT provision

def test_an_announcement_that_does_not_name_this_service_is_refused(host):
    """GRASP-01: "MUST reject git repository announcements that do not list the service in both
    `clone` and `relays` tags". Without this we would host a repository announced at somebody
    else's server, which is a mirror nobody asked us for."""
    other = "https://someone-else.example/git/%s/%s.git" % (NPUB, REPO)
    assert host(_Conn([announcement(clone=other)], wot=[OWNER])) is False


def test_the_clone_tag_is_matched_by_host_and_base_path_not_by_string_equality(host):
    """The announcement is written by the client from whatever base it was configured with, and ours
    is a non-root base path. A trailing slash or an extra path segment must not read as a different
    service."""
    assert host(_Conn([announcement(clone="%s/%s/%s.git/" % (BASE, NPUB, REPO))], wot=[OWNER])) is True


def test_a_node_that_does_not_know_its_own_address_provisions_nothing(host):
    """With no `public_base` we cannot tell whether an announcement names us, and "I cannot tell" is
    not "yes"."""
    assert host(_Conn([announcement()], wot=[OWNER]), public_base="") is False


def test_no_announcement_at_all_means_no_repository(host):
    assert host(_Conn([], wot=[OWNER])) is False


def test_a_tampered_announcement_provisions_nothing(host):
    bad = announcement()
    bad["tags"] = [["d", REPO], ["clone", "%s/%s/%s.git" % (BASE, NPUB, REPO)], ["x", "y"]]
    assert host(_Conn([bad], wot=[OWNER])) is False


def test_a_database_we_cannot_ask_provisions_nothing(host):
    """Fail closed, like every other decision in this file."""
    class _Boom:
        @staticmethod
        def connect(*a, **k):
            raise OSError("connection refused")

    gh._CONFIG.update({"pg_dsn": "stub"})
    gh._prov_deny.clear()
    sys.modules["psycopg2"] = _Boom
    h = object.__new__(gh._Handler)
    h.headers = {}
    assert h._autoprovision(OWNER, REPO) is False


def test_a_refusal_is_cached_so_a_polling_client_cannot_hammer_postgres(host):
    """ngit polls `info/refs` in a loop, and this path is reachable by any anonymous caller with any
    made-up name — it must not be three Postgres reads per request."""
    conn = _Conn([announcement()])
    assert host(conn) is False
    before = len(conn.sql)
    h = object.__new__(gh._Handler)
    h.headers = {}
    assert h._autoprovision(OWNER, REPO) is False
    assert len(conn.sql) == before, "the refusal was re-computed instead of cached"


def test_only_a_SUCCESS_is_uncached_because_the_repo_then_exists(host):
    """The yes is never cached and never needs to be: the next probe finds the repo on disk and
    never reaches this code."""
    assert host(_Conn([announcement()], wot=[OWNER])) is True
    assert (OWNER, REPO) not in gh._prov_deny


# ------------------------------------------------------ through the REAL request ngit sits waiting on

def _serve(conn, tmp_path, monkeypatch, **cfg):
    """The shipped handler on a real socket, with a stubbed Postgres. `check_git_server_ready` is an
    anonymous `remote.download(&[])` — i.e. exactly the request built below."""
    import threading
    import urllib.error
    import urllib.request
    from http.server import ThreadingHTTPServer

    monkeypatch.setenv("GRASP_GIT_PROJECT_ROOT", str(tmp_path))
    base_cfg = {"pg_dsn": "stub", "public_base": BASE, "allowlist": "", "auto_provision": True,
                "accept_policy": "local-or-wot", "read_skew": 60, "write_skew": 120, "port": 0}
    base_cfg.update(cfg)
    monkeypatch.setattr(gh, "_CONFIG", base_cfg, raising=False)
    monkeypatch.setitem(sys.modules, "psycopg2",
                        type("_PG", (), {"connect": staticmethod(lambda *a, **k: conn)}))
    gh._prov_deny.clear()
    gh._alias_cache.clear()

    class _S(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    httpd = _S(("127.0.0.1", 0), gh._Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()

    def get(path):
        try:
            with urllib.request.urlopen("http://127.0.0.1:%d/git/%s/%s" % (port, NPUB, path),
                                        timeout=20) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    return httpd, get


def test_the_info_refs_PROBE_itself_provisions_the_repository(tmp_path, monkeypatch):
    """THE CONTRACT, end to end. `ngit init` publishes the 30617 and then loops on this request
    (`check_git_server_ready`, accept_maintainership.rs:401) because there is no provisioning call
    in ngit at all. Before this, that loop ran until it timed out, for ever."""
    conn = _Conn([announcement()], wot=[OWNER])
    httpd, get = _serve(conn, tmp_path, monkeypatch)
    try:
        code, body = get("%s.git/info/refs?service=git-upload-pack" % REPO)
    finally:
        httpd.shutdown()
    assert code == 200, (code, body[:200])
    assert b"git-upload-pack" in body, "the advertisement is what the client is waiting to read"
    assert ghs.repo_exists(OWNER, REPO) is True


def test_a_refused_author_still_gets_a_plain_404(tmp_path, monkeypatch):
    """Not a 403, and nothing about why. An unaccepted repository is indistinguishable from one we
    do not host, which is what a client that is going to give up should see."""
    httpd, get = _serve(_Conn([announcement()]), tmp_path, monkeypatch)
    try:
        code, _ = get("%s.git/info/refs?service=git-upload-pack" % REPO)
    finally:
        httpd.shutdown()
    assert code == 404
    assert ghs.repo_exists(OWNER, REPO) is False


def test_a_BROWSE_route_can_never_allocate_disk(tmp_path, monkeypatch):
    """Scoped to the smart-HTTP endpoints deliberately: only a git client is ever waiting on the
    answer, and a stray GET from a crawler must not be able to make a repository."""
    conn = _Conn([announcement()], wot=[OWNER])
    httpd, get = _serve(conn, tmp_path, monkeypatch)
    try:
        code, _ = get("%s.git/tree/HEAD" % REPO)
    finally:
        httpd.shutdown()
    assert code == 404
    assert ghs.repo_exists(OWNER, REPO) is False, "a browse route provisioned a repository"
