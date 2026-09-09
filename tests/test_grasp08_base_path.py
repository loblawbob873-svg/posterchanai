"""GRASP-08: a GRASP service URL may carry a NON-ROOT BASE PATH, and everything must preserve it.

We are the case that would find this: our git host is served at `poster.place/git/…`, never at the
root, so a route or a URL builder that assumes the repo sits at `/<npub>/<id>.git` is broken here
and only here. The v3 note names four places. What each of them is on this deployment:

  (1) THE 30617 CLONE URL — built from `git_server_public_base`, which IS the base path. Pinned by
      test_the_announced_clone_url_keeps_the_base_path, end to end through the real host.
  (2) `nostr://` RELAY HINTS — we EMIT none. `nostr://` is a remote form that `git-remote-nostr`
      (upstream's binary, /usr/local/bin, not in this repo) parses client-side; the only hint we
      publish is the 30617 `relays` tag. That tag is NOT derived from the git base path and this
      test does not pin it — see the note at the bottom of this file, which is a deployment fact,
      not a code defect.
  (3) GIT PUSH/FETCH SMART-HTTP ROUTES — `_parse_repo_path` finds the first `<seg>.git` segment and
      takes the segment before it as the owner, so any prefix works. Pinned below.
  (4) GRASP-06 PR ENDPOINTS — we have none. Not applicable; there is nothing to preserve a path in.

Plus the proxy hop, which is ours and not in the v3 list but has the same failure mode.
"""
from __future__ import annotations

import base64
import json
import os
import sys
import threading
import time
from http.server import ThreadingHTTPServer


_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import git_host_main as gh                             # noqa: E402
from app.services import git_auth, git_host_service as ghs   # noqa: E402
from app.services.nostr import bip340, nostr_service   # noqa: E402
from app.services.nostr.event import build_event       # noqa: E402

OWNER_SK = (11).to_bytes(32, "big")
OWNER = bip340.pubkey_from_seckey(OWNER_SK).hex()
BASE = "https://poster.place/git"          # the real shape: a non-root base path


def test_the_smart_http_router_tolerates_any_base_path():
    """(3) A clone of `https://poster.place/git/<npub>/<id>.git` arrives at the host as
    `/git/<npub>/<id>.git/info/refs` — nginx does not strip the prefix. Anchoring the parse at the
    root would 404 every request this deployment has ever served."""
    npub = nostr_service.npub_of(OWNER)
    for prefix in ("", "/git", "/some/deep/mount", "/git/"):
        path = "%s/%s/demo.git/info/refs" % (prefix.rstrip("/"), npub)
        parsed = gh._parse_repo_path(path)
        assert parsed is not None, path
        owner_hex, repo_id, rest = parsed
        assert (owner_hex, repo_id, rest) == (OWNER, "demo", "info/refs"), path


def test_a_repo_id_is_never_read_out_of_the_base_path():
    """The scan takes the FIRST `<seg>.git` segment, so a mount point that happens to end in .git
    must not be mistaken for the repository."""
    npub = nostr_service.npub_of(OWNER)
    owner_hex, repo_id, rest = gh._parse_repo_path("/git/%s/demo.git/git-upload-pack" % npub)
    assert repo_id == "demo" and rest == "git-upload-pack"


def test_the_proxy_hop_keeps_the_hosting_node_s_base_path():
    """Ours, not in the v3 list, same failure. A proxy node forwards to `git_server_proxy_url`,
    which may itself be a non-root base (`https://nas.lan/git`); dropping it 404s on the far side."""
    src = open(os.path.join(_ROOT, "app/services/git_proxy.py")).read()
    assert 'target = "%s/%s" % (base, repo_path.lstrip("/"))' in src
    assert 'return base.rstrip("/")' in src, "the base must be kept whole, path and all"


def test_the_announced_clone_url_keeps_the_base_path(tmp_path, monkeypatch):
    """(1) THE ONE THAT ACTUALLY REACHES OTHER PEOPLE. The clone URL in a 30617 is what every other
    client — ngit, gitworkshop, a plain `git clone` — uses to reach the repo, so a base path dropped
    here is a repo nobody outside this node can fetch. Driven through the real host over HTTP."""
    # `git_project_root()` is LAZY and reads GRASP_GIT_PROJECT_ROOT FIRST — it has to, so the
    # hook subprocesses resolve the same root the server served from. Assigning the module
    # attribute does NOTHING, and a store test that does it writes into the LIVE repo store.
    monkeypatch.setenv("GRASP_GIT_PROJECT_ROOT", str(tmp_path))
    npub = nostr_service.npub_of(OWNER)
    monkeypatch.setattr(gh, "_CONFIG", {
        "pg_dsn": "", "public_base": BASE, "allowlist": npub, "relay_url": "",
        "write_skew": 120, "read_skew": 60, "port": 0}, raising=False)

    class _S(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    httpd = _S(("127.0.0.1", 0), gh._Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
    try:
        import urllib.request
        # The host is reached at a DIFFERENT address from the one it announces — which is the normal
        # case (nginx in front) and the reason the announced URL cannot be derived from the request.
        url = "http://127.0.0.1:%d/git/%s/demo.git/create" % (port, npub)
        ev = build_event(OWNER_SK, git_auth.NIP98_KIND, "",
                         tags=[["u", url], ["method", "POST"]], created_at=int(time.time()))
        req = urllib.request.Request(url, data=b"{}", method="POST")
        req.add_header("Authorization",
                       "Nostr " + base64.b64encode(json.dumps(ev).encode()).decode())
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=20) as r:
            out = json.loads(r.read())
    finally:
        httpd.shutdown()

    assert out.get("ok"), out
    assert out["clone"] == "%s/%s/demo.git" % (BASE, npub), out["clone"]
    tags = {t[0]: t[1] for t in out["announce_tags_30617"] if len(t) >= 2}
    assert tags["clone"] == "%s/%s/demo.git" % (BASE, npub), tags


def test_the_relays_tag_is_not_derived_from_the_git_base_path_and_that_is_a_DEPLOYMENT_fact():
    """(2), recorded rather than "fixed". GRASP models one base URL serving both git and the relay,
    so a strict client derives `wss://poster.place/git` from a git base of `https://poster.place/git`.
    Ours are split — git at /git, relay at /relay — and `_relay_from_public_base` encodes exactly
    that, dropping the path and substituting /relay.

    This is a REAL divergence and it is already the known cause of ngit's push failure ("state event
    failed to reach any git server relay"): ngit derives the relay for its 30618 from the git server
    base and does NOT read the announcement's `relays` tag. The fix is an nginx change on router.lan
    — route websocket upgrades on /git/ to the relay, making poster.place/git a real GRASP base —
    and only THEN re-announce with `relays = wss://poster.place/git`. Changing the derivation first
    would announce a relay URL that nothing answers, breaking every push that works today. So this
    asserts the CURRENT behaviour deliberately; whoever makes the nginx change updates it here.
    """
    from app.services.git_http_service import _relay_from_public_base
    assert _relay_from_public_base("https://poster.place/git") == "wss://poster.place/relay"
    assert _relay_from_public_base("http://nas.lan:3051/git") == "ws://nas.lan:3051/relay"
    assert _relay_from_public_base("not a url") == "", "an unusable base must yield no relays tag"


def test_we_expose_no_grasp06_pull_request_endpoints():
    """(4). Nothing to preserve a base path in — recorded so the next reader does not go looking."""
    import subprocess
    # ASK GIT WHAT IS OURS, AND ONLY WHERE AN ENDPOINT CAN BE DEFINED.
    #
    # This is an audit of the routes this node SERVES, so it reads the code that can serve one. Two
    # widenings were both wrong: a recursive grep of the checkout also read venv-unified/, so it
    # failed the day huggingface_hub exported `merge_pull_request` — a dependency's API deciding
    # whether our endpoint audit passes — and grepping every tracked .py then failed on the phrase
    # appearing as PROSE in another test's assertion message. Neither is an endpoint. A test that
    # goes red for a word in a sentence teaches people to widen the exclusion list, which is how an
    # audit quietly stops auditing.
    where = ["app", "git_host_main.py"]
    tracked = subprocess.run(["git", "ls-files", "-z", "--"] + where,
                             capture_output=True, text=True, cwd=_ROOT).stdout.split("\0")
    tracked = [f for f in tracked if f.endswith(".py")]
    assert tracked, "the audit found no source to read — it would pass about nothing"
    hits = subprocess.run(["grep", "-niE", "grasp-?06|pull_request", "--"] + tracked,
                          cwd=_ROOT, capture_output=True, text=True).stdout
    assert not hits.splitlines(), hits
