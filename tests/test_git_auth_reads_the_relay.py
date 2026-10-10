"""#161: the git push ACL, the private-repo gate and the PR tips are read from THIS node's RELAY, not
from its Postgres `events`/`event_tags` tables -- and a relay that cannot be asked REFUSES.

The rule that matters, stated as what a person pushing sees:

  * relay up, real maintainer, matching signed 30618  -> the push lands;
  * relay down, SAME maintainer, SAME signed state    -> the push is refused, with a message that
    says the relay could not be asked (never "allowed", never "you are not a maintainer", never a
    crash). "Could not ask" is not "no maintainers", "no announcement" or "no state";
  * no database connection is opened on any of these paths: psycopg2 is made unimportable and the
    decision still comes out right, through the relay.

The hook is run as the REAL `git_hooks/pre_receive.py` process (it is a separate process in
production too), against a real websocket relay (tests/git_relay_fake.serve) that sends the NIP-42
challenge on connect and withholds GRASP-08 private repo events from an unauthenticated socket, as
nostr_relay/server.py does.
"""
from __future__ import annotations

import json
import os
import socket
import subprocess
import sys

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.services import git_auth, git_host_service as ghs, relay_reader   # noqa: E402
from app.services.nostr import bech32, bip340                               # noqa: E402
from app.services.nostr.event import build_event                            # noqa: E402
from tests import git_relay_fake                                            # noqa: E402
from tests.git_relay_fake import FakeRelay                                  # noqa: E402

OWNER_SK = (41).to_bytes(32, "big")
MAINT_SK = (42).to_bytes(32, "big")
RANDO_SK = (43).to_bytes(32, "big")
NODE_SK = (44).to_bytes(32, "big")
OWNER = bip340.pubkey_from_seckey(OWNER_SK).hex()
MAINT = bip340.pubkey_from_seckey(MAINT_SK).hex()
RANDO = bip340.pubkey_from_seckey(RANDO_SK).hex()
NODE = bip340.pubkey_from_seckey(NODE_SK).hex()
REPO = "relayread"
SHA = "1" * 40
ZERO = "0" * 40


def announcement(*, private=False, maintainers=(MAINT,), d=REPO):
    tags = [["d", d], ["maintainers", *maintainers]]
    if private:
        tags.append(["private", "true"])
    return build_event(OWNER_SK, git_auth.ANNOUNCE_KIND, "", tags=tags)


def state(sk=MAINT_SK, sha=SHA, *, private=False, d=REPO):
    tags = [["d", d], ["refs/heads/main", sha], ["HEAD", "ref: refs/heads/main"]]
    if private:
        tags.append(["private", "true"])
    return build_event(sk, git_auth.STATE_KIND, "", tags=tags)


def _closed_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


# ------------------------------------------------------------------ in process: the loaders

@pytest.fixture
def no_postgres(monkeypatch):
    """psycopg2 cannot be imported, and connecting would raise: any path that still reads the
    database fails loudly instead of quietly succeeding against a real one."""
    class _NoPG:
        def __getattr__(self, name):
            raise AssertionError("a git decision touched psycopg2.%s (#161: read the relay)" % name)
    monkeypatch.setitem(sys.modules, "psycopg2", _NoPG())


def test_a_maintainer_push_is_decided_through_the_relay_alone(no_postgres):
    relay = FakeRelay([announcement(), state()])
    maints = git_auth.load_maintainers(relay, OWNER, REPO)
    assert maints == {OWNER, MAINT}
    states = git_auth.load_state_events(relay, OWNER, REPO, maints)
    ok, why = git_auth.decide_push_ref("refs/heads/main", ZERO, SHA, maints, states)
    assert ok, why


@pytest.mark.parametrize("loader", ["maintainers", "announcement", "private", "state", "event", "prs"])
def test_a_relay_that_cannot_be_asked_RAISES_and_never_answers_empty(no_postgres, loader):
    """THE RULE. An empty set / None / False read off a failed query is a decision made on nothing:
    "no maintainers" refuses the wrong person, "not private" publishes a private repo."""
    down = FakeRelay([announcement(), state()], down=True)
    call = {
        "maintainers": lambda: git_auth.load_maintainers(down, OWNER, REPO),
        "announcement": lambda: git_auth.load_announcement(down, OWNER, REPO),
        "private": lambda: git_auth.load_announced_private(down, OWNER, REPO),
        "state": lambda: git_auth.load_state_events(down, OWNER, REPO, {OWNER, MAINT}),
        "event": lambda: git_auth.load_event_by_id(down, "ab" * 32),
        "prs": lambda: git_auth.load_pr_events_for_tips(down, ["ab" * 32]),
    }[loader]
    with pytest.raises(relay_reader.Unavailable):
        call()


def test_an_answer_that_FILLS_the_relays_limit_is_not_the_whole_answer(no_postgres):
    """The relay caps one filter at 5000 events. An author with that many announcements may have had
    the one that matters cut off -- treated as "could not ask", never as "they have none"."""
    class _Full:
        def query(self, filters):
            return [{"id": "%064x" % i, "pubkey": OWNER, "kind": 30617, "created_at": i, "tags": []}
                    for i in range(git_auth._RELAY_LIMIT)]
    with pytest.raises(relay_reader.Unavailable):
        git_auth.load_maintainers(_Full(), OWNER, REPO)


def test_the_repo_id_is_matched_case_insensitively_and_never_put_in_the_filter():
    """The relay's #d is EXACT; the directory id is lowercase and the announcement may say `RelayRead`."""
    relay = FakeRelay([announcement(d="RelayRead"), state(d="RELAYREAD")])
    maints = git_auth.load_maintainers(relay, OWNER, REPO)
    assert maints == {OWNER, MAINT}
    assert len(git_auth.load_state_events(relay, OWNER, REPO, maints)) == 1
    assert not any("#d" in f for f in relay.filters), relay.filters


def test_the_maintainer_walk_is_one_read_per_round_not_one_per_pubkey():
    relay = FakeRelay([announcement(maintainers=(MAINT, RANDO))])
    git_auth.load_maintainers(relay, OWNER, REPO)
    assert len(relay.filters) == 2, relay.filters          # owner, then {maint, rando} together
    assert sorted(relay.filters[1]["authors"]) == sorted([MAINT, RANDO])


def test_the_reaper_keeps_every_ref_when_the_relay_cannot_be_asked(no_postgres, tmp_path, monkeypatch):
    monkeypatch.setenv("GRASP_GIT_PROJECT_ROOT", str(tmp_path))
    assert ghs.create_repo(OWNER, REPO).get("ok")
    d = ghs.repo_dir(OWNER, REPO)
    env = dict(os.environ, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    tree = subprocess.run(["git", "--git-dir", d, "mktree"], input="", capture_output=True, text=True,
                          check=True).stdout.strip()
    sha = subprocess.run(["git", "--git-dir", d, "commit-tree", tree, "-m", "x"], env=env,
                         capture_output=True, text=True, check=True).stdout.strip()
    subprocess.run(["git", "--git-dir", d, "update-ref", "refs/nostr/" + "ab" * 32, sha], check=True)
    r = ghs.reap_nostr_refs(conn=FakeRelay(down=True), grace=0)
    assert r["deleted"] == 0 and len(ghs.nostr_refs(OWNER, REPO)) == 1


# ------------------------------------------------------------------ the relay serves the node

def test_the_relay_serves_a_private_repos_events_to_the_NODE_key_and_to_nobody_else():
    """The hook authenticates as the node key (the operator key that signs the 30618 witnesses). That
    node holds the repo's bytes on its disk; refusing it the metadata would refuse every push."""
    from app.services.nostr_relay.server import RelayServer
    srv = RelayServer.__new__(RelayServer)
    srv.cfg = {"node_pubkey": NODE}
    srv._auth_pubkeys = {"node": {NODE}, "rando": {RANDO}, "anon": set(), "maint": {MAINT}}
    ev = announcement(private=True)
    assert srv._can_serve_event("node", ev) is True
    assert srv._can_serve_event("maint", ev) is True       # a declared maintainer, as before
    assert srv._can_serve_event("rando", ev) is False
    assert srv._can_serve_event("anon", ev) is False
    srv.cfg = {}                                           # a relay that does not know its node key
    assert srv._can_serve_event("node", ev) is False


# ------------------------------------------------------------------ the REAL hook process

@pytest.fixture
def hook(tmp_path):
    """Run git_hooks/pre_receive.py as git would: its own process, GIT_DIR set, one ref line on
    stdin -- with psycopg2 made UNIMPORTABLE in that process."""
    root = tmp_path / "git_repos"
    root.mkdir()
    env0 = dict(os.environ, GRASP_GIT_PROJECT_ROOT=str(root))
    old = os.environ.get("GRASP_GIT_PROJECT_ROOT")
    os.environ["GRASP_GIT_PROJECT_ROOT"] = str(root)
    try:
        assert ghs.create_repo(OWNER, REPO).get("ok")
        gitdir = ghs.repo_dir(OWNER, REPO)
    finally:
        if old is None:
            os.environ.pop("GRASP_GIT_PROJECT_ROOT", None)
        else:
            os.environ["GRASP_GIT_PROJECT_ROOT"] = old
    nopg = tmp_path / "nopg"
    (nopg / "psycopg2").mkdir(parents=True)
    (nopg / "psycopg2" / "__init__.py").write_text(
        'raise ImportError("#161: the push hook must not touch Postgres")\n')
    keyfile = tmp_path / "keys.json"
    keyfile.write_text(json.dumps({"operator_nsec": bech32.encode("nsec", NODE_SK), "storage": {}}))

    def run(port, *, line=None, with_key=True):
        env = {k: v for k, v in env0.items() if not k.startswith("GRASP_PG")}
        env.update({
            "GIT_DIR": gitdir, "GRASP_REPO_ROOT": _ROOT, "GRASP_RELAY_PORT": str(port),
            "GRASP_NIP98_ENABLED": "0", "GRASP_ALLOW_FORCE": "1",
            "PYTHONPATH": str(nopg),
            "POSTERCHANAI_KEYFILE": str(keyfile if with_key else tmp_path / "absent.json"),
        })
        return subprocess.run([sys.executable, os.path.join(_ROOT, "git_hooks", "pre_receive.py")],
                              input=line or "%s %s refs/heads/main\n" % (ZERO, SHA), env=env,
                              capture_output=True, text=True, timeout=60, cwd=gitdir)
    return run


def test_HOOK_relay_up_maintainer_push_is_ALLOWED(hook):
    srv, port = git_relay_fake.serve([announcement(), state()])
    try:
        r = hook(port)
    finally:
        srv.shutdown()
    assert r.returncode == 0, r.stderr
    assert "authorized" in r.stderr, r.stderr


def test_HOOK_relay_DOWN_the_same_push_is_REFUSED_and_says_why(hook):
    r = hook(_closed_port())
    assert r.returncode != 0, "a push was accepted with no relay to authorize it"
    assert "relay could not be asked" in r.stderr, r.stderr
    assert "Traceback" not in r.stderr, r.stderr


def test_HOOK_a_strangers_state_is_still_refused_through_the_relay(hook):
    srv, port = git_relay_fake.serve([announcement(), state(RANDO_SK)])
    try:
        r = hook(port)
    finally:
        srv.shutdown()
    assert r.returncode != 0 and "relay could not be asked" not in r.stderr, r.stderr


def test_HOOK_a_PRIVATE_repo_is_read_by_authenticating_as_the_node(hook):
    """GRASP-08: the relay withholds a private repo's 30617/30618 from an unauthenticated socket. The
    hook signs in as the node key and the push lands; without the key it is REFUSED (auth-required is
    "could not ask"), never decided on an announcement-less, state-less answer."""
    events = [announcement(private=True), state(private=True)]
    srv, port = git_relay_fake.serve(events, private_readers={NODE})
    try:
        ok = hook(port)
        nokey = hook(port, with_key=False)
    finally:
        srv.shutdown()
    assert ok.returncode == 0, ok.stderr
    assert nokey.returncode != 0 and "relay could not be asked" in nokey.stderr, nokey.stderr


def test_HOOK_with_no_relay_configured_refuses(hook):
    r = hook("")
    assert r.returncode != 0 and "no relay configured" in r.stderr, r.stderr


def test_HOOK_a_refs_nostr_PR_tip_is_read_from_the_relay(hook):
    contrib = (45).to_bytes(32, "big")
    pr = build_event(contrib, 1618, "", tags=[["c", "2" * 40]])
    srv, port = git_relay_fake.serve([announcement(), pr])
    try:
        good = hook(port, line="%s %s refs/nostr/%s\n" % (ZERO, "2" * 40, pr["id"]))
        bad = hook(port, line="%s %s refs/nostr/%s\n" % (ZERO, "3" * 40, pr["id"]))
    finally:
        srv.shutdown()
    assert good.returncode == 0, good.stderr
    assert bad.returncode != 0 and "not among the PR event's c tags" in bad.stderr, bad.stderr


# ------------------------------------------------------------------ the web editor's write gate

def _edit_attempt(monkeypatch, relay_port, signer_sk):
    """Run the shipped `_serve_edit` gate with a NIP-98 header from `signer_sk`; return the first
    refusal (code, message), or None if the request got PAST authorization."""
    import base64
    import git_host_main as gh
    monkeypatch.setattr(gh, "_CONFIG", {"relay_port": relay_port, "write_skew": 120}, raising=False)
    url = "http://127.0.0.1/git/%s/%s.git/edit" % (OWNER, REPO)
    ev = build_event(signer_sk, 27235, "", tags=[["u", url], ["method", "POST"]])
    h = object.__new__(gh._Handler)
    h.headers = {"Authorization": "Nostr " + base64.b64encode(json.dumps(ev).encode()).decode(),
                 "Content-Length": "0"}
    got = []
    monkeypatch.setattr(gh._Handler, "_deny",
                        lambda self, code, msg, auth=False: got.append((code, msg)))
    h._serve_edit(OWNER, REPO)
    return got[0] if got else None


def test_WEB_EDIT_relay_down_refuses_a_maintainer_and_SAYS_the_relay_could_not_be_asked(monkeypatch):
    code, msg = _edit_attempt(monkeypatch, _closed_port(), MAINT_SK)
    assert code == 503 and "relay could not be asked" in msg, (code, msg)


def test_WEB_EDIT_relay_up_lets_the_same_maintainer_past_the_gate(monkeypatch):
    srv, port = git_relay_fake.serve([announcement()])
    try:
        code, msg = _edit_attempt(monkeypatch, port, MAINT_SK)
    finally:
        srv.shutdown()
    # Past authorization, the next check is the (deliberately empty) body.
    assert code == 413 and "body" in msg, (code, msg)


def test_WEB_EDIT_a_stranger_is_refused_as_a_stranger_not_as_an_outage(monkeypatch):
    srv, port = git_relay_fake.serve([announcement()])
    try:
        code, _msg = _edit_attempt(monkeypatch, port, RANDO_SK)
    finally:
        srv.shutdown()
    assert code == 401
