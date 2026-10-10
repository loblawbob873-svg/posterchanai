"""A PRIVATE repo answers NOTHING without GRASP-08 auth — on the PUSH side as well as the clone side.

Run: venv-unified/bin/python -m pytest tests/test_grasp08_private_repo_push_side_is_gated.py

Found in a GRASP compliance review (2026-09-28): the read gate ran for `git-upload-pack` only. But
`GET info/refs?service=git-receive-pack` is ALSO a ref advertisement — git-http-backend answers it
with every ref and its sha, because `http.receivepack=true` — so an anonymous request printed
`<sha> refs/heads/secret` for a private repo. And the receive-pack POST reached the pre-receive
hook, which lets `refs/nostr/<event-id>` through before any auth: anyone could write refs and
objects into a private repo. GRASP-08: every GET and POST to a private repo carries NIP-98 auth or
gets a 401.

Real bare repo, real `git_host_main` handler over HTTP, real `git-http-backend`.
"""
import base64
import json
import os
import subprocess
import sys
import threading
import urllib.error
import urllib.request
from http.server import ThreadingHTTPServer

import pytest

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.services import git_host_service as ghs            # noqa: E402
from app.services.nostr import bip340, nostr_service         # noqa: E402
from app.services.nostr.event import build_event             # noqa: E402

BACKEND = "/usr/libexec/git-core/git-http-backend"
pytestmark = pytest.mark.skipif(not os.path.exists(BACKEND), reason="git-http-backend missing")

OWNER_SK, RANDO_SK = (11).to_bytes(32, "big"), (33).to_bytes(32, "big")
OWNER = bip340.pubkey_from_seckey(OWNER_SK).hex()


def _commit(gitdir, ref):
    env = dict(os.environ, GIT_DIR=gitdir, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t",
               GIT_COMMITTER_NAME="t", GIT_COMMITTER_EMAIL="t@t")
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], input=b"secret\n",
                          capture_output=True, env=env).stdout.strip().decode()
    tree = subprocess.run(["git", "mktree"], input=("100644 blob %s\tS\n" % blob).encode(),
                          capture_output=True, env=env).stdout.strip().decode()
    c = subprocess.run(["git", "commit-tree", tree, "-m", "s"], capture_output=True, env=env).stdout.strip().decode()
    subprocess.run(["git", "update-ref", ref, c], env=env, check=True)
    return c


@pytest.fixture
def host(tmp_path, monkeypatch):
    monkeypatch.setenv("GRASP_GIT_PROJECT_ROOT", str(tmp_path))
    import git_host_main
    monkeypatch.setattr(git_host_main, "_CONFIG", {"relay_port": 0, "repo_root": _ROOT, "repo_max_mb": 512,
                                                   "allow_force": True, "nip98_push": True,
                                                   "public_base": "", "read_skew": 300, "port": 0})
    priv = ghs.create_repo(OWNER, "privrepo", private=True)
    pub = ghs.create_repo(OWNER, "pubrepo", private=False)
    secret = _commit(priv["path"], "refs/heads/secret")
    public_sha = _commit(pub["path"], "refs/heads/main")

    class _S(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True
    httpd = _S(("127.0.0.1", 0), git_host_main._Handler)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()
    base = "http://127.0.0.1:%d/%s" % (httpd.server_address[1], nostr_service.npub_of(OWNER))
    yield base, secret, public_sha
    httpd.shutdown()


def _req(url, method="GET", auth=None, body=None, ctype=None):
    req = urllib.request.Request(url, data=body, method=method)
    if auth:
        req.add_header("Authorization", auth)
    if ctype:
        req.add_header("Content-Type", ctype)
    try:
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def _nip98(sk, url):
    ev = build_event(sk, 27235, "", tags=[["u", url], ["method", "GET"]])
    return "Nostr " + base64.b64encode(json.dumps(ev).encode()).decode()


def test_the_push_side_ref_advertisement_of_a_private_repo_needs_auth(host):
    base, secret, _ = host
    url = base + "/privrepo.git/info/refs?service=git-receive-pack"
    code, body = _req(url)
    assert code == 401, (code, body[:200])
    assert secret.encode() not in body and b"refs/heads/secret" not in body, "the private refs leaked"


def test_a_stranger_s_credential_is_refused_too(host):
    base, secret, _ = host
    url = base + "/privrepo.git/info/refs?service=git-receive-pack"
    code, body = _req(url, auth=_nip98(RANDO_SK, url))
    assert code == 401 and secret.encode() not in body


def test_an_anonymous_push_never_reaches_the_repo(host):
    """The POST that would carry refs/nostr/<id> + objects into the private repo."""
    base, _, _ = host
    code, _body = _req(base + "/privrepo.git/git-receive-pack", method="POST", body=b"0000",
                       ctype="application/x-git-receive-pack-request")
    assert code == 401


def test_the_owner_still_sees_the_push_advertisement(host):
    base, secret, _ = host
    url = base + "/privrepo.git/info/refs?service=git-receive-pack"
    code, body = _req(url, auth=_nip98(OWNER_SK, url))
    assert code == 200 and secret.encode() in body


def test_a_public_repo_push_advertisement_is_unchanged(host):
    """Pushes to PUBLIC repos are authorized by the pre-receive hook (maintainer-signed 30618), not
    by an HTTP credential — that must keep working anonymously, as ngit does it."""
    base, _, public_sha = host
    code, body = _req(base + "/pubrepo.git/info/refs?service=git-receive-pack")
    assert code == 200 and public_sha.encode() in body


def test_the_challenge_is_grasp08_shaped(host):
    """GRASP-08: a 401 with `WWW-Authenticate: Nostr … method="GET"` and an empty body."""
    base, _, _ = host
    req = urllib.request.Request(base + "/privrepo.git/info/refs?service=git-upload-pack")
    try:
        urllib.request.urlopen(req, timeout=15)
        raise AssertionError("a private repo answered anonymously")
    except urllib.error.HTTPError as e:
        schemes = e.headers.get_all("WWW-Authenticate")
        body = e.read()
    nostr = [v for v in schemes if v.lower().startswith("nostr")]
    assert nostr and 'method="GET"' in nostr[0], schemes
    assert any(v.lower().startswith("basic") for v in schemes), "ngit's libgit2 transport needs Basic offered"
    assert body == b""
