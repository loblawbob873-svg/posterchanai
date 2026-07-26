#!/usr/bin/env python3
"""Serving + supervisor-gate verification for the GRASP git host.

Proves, WITHOUT Postgres:
  1. the supervisor spawns NOTHING when git_server_enabled is false;
  2. git_host_main serves GET /<npub>/<id>.git/info/refs?service=git-upload-pack for a real bare repo
     by exec'ing git-http-backend (a PUBLIC repo clones anonymously);
  3. a PRIVATE repo: anonymous upload-pack -> 401 (no refs leaked); a valid NIP-98 header from an
     allowlisted reader -> 200; a non-allowlisted signer -> 401.

Uses a temp GIT_PROJECT_ROOT (no DB); the private read gate is exercised with pg_dsn="" so the
readers allowlist alone gates (production injects the DSN + folds in maintainers).
"""

import base64
import json
import os
import subprocess
import sys
import tempfile
import threading
import time
import urllib.request
import urllib.error
from http.server import ThreadingHTTPServer

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from app.services import git_host_service as ghs
from app.services import git_auth
from app.services.nostr import bip340
from app.services.nostr.event import build_event

_results = []


def check(name, cond):
    ok = bool(cond)
    _results.append(ok)
    print("  [%s] %s" % ("PASS" if ok else "FAIL", name))


def _populate_bare(gitdir):
    """Add refs/heads/main to a bare repo via plumbing (no receive-pack/hooks)."""
    env = dict(os.environ, GIT_DIR=gitdir)
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], input=b"hello\n",
                          capture_output=True, env=env).stdout.strip()
    tree_in = ("100644 blob %s\tREADME\n" % blob.decode()).encode()
    tree = subprocess.run(["git", "mktree"], input=tree_in, capture_output=True, env=env).stdout.strip()
    env2 = dict(env, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                GIT_COMMITTER_EMAIL="t@t")
    commit = subprocess.run(["git", "commit-tree", tree.decode(), "-m", "init"],
                            capture_output=True, env=env2).stdout.strip()
    subprocess.run(["git", "update-ref", "refs/heads/main", commit.decode()], env=env)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], env=env)
    return commit.decode()


def test_supervisor_gate():
    print("1) supervisor gate (git_server_enabled=false -> no spawn)")
    from app.services import git_http_service as svc
    orig = svc._read_config
    try:
        svc._read_config = lambda: {"enabled": False}
        svc.start_git_http()
        check("disabled: no subprocess spawned", svc._host.proc is None)
    finally:
        svc._read_config = orig
        try:
            svc.stop_git_http()
        except Exception:
            pass


def _serve_in_thread(config):
    import git_host_main
    git_host_main._CONFIG = config

    class _S(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True

    httpd = _S(("127.0.0.1", 0), git_host_main._Handler)
    port = httpd.server_address[1]
    t = threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True)
    t.start()
    return httpd, port


def _get(url, header=None):
    req = urllib.request.Request(url)
    if header:
        req.add_header("Authorization", header)
    try:
        with urllib.request.urlopen(req, timeout=10) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:
        return 0, str(e).encode()


def nip98_header(sk, method, url):
    ev = build_event(sk, 27235, "", tags=[["u", url], ["method", method]])
    return "Nostr " + base64.b64encode(json.dumps(ev).encode()).decode()


def main():
    if not os.path.exists("/usr/libexec/git-core/git-http-backend"):
        print("git-http-backend missing — cannot run serving test")
        return 1

    test_supervisor_gate()

    tmp = tempfile.mkdtemp(prefix="grasp_test_")
    ghs.GIT_PROJECT_ROOT = tmp   # redirect repo root for this test (module attr read at call time)

    owner_sk = (11).to_bytes(32, "big")
    reader_sk = (22).to_bytes(32, "big")
    rando_sk = (33).to_bytes(32, "big")
    owner_hex = bip340.pubkey_from_seckey(owner_sk).hex()
    reader_hex = bip340.pubkey_from_seckey(reader_sk).hex()
    from app.services.nostr import nostr_service
    npub = nostr_service.npub_of(owner_hex)

    config = {"pg_dsn": "", "repo_root": _ROOT, "repo_max_mb": 512, "allow_force": True,
              "nip98_push": True, "public_base": "", "read_skew": 300, "port": 0}
    httpd, port = _serve_in_thread(config)
    time.sleep(0.3)

    # --- PUBLIC repo: anonymous clone works ---------------------------------
    print("2) public repo: anonymous info/refs?service=git-upload-pack")
    r = ghs.create_repo(owner_hex, "pubrepo", private=False)
    _populate_bare(r["path"])
    url = "http://127.0.0.1:%d/%s/pubrepo.git/info/refs?service=git-upload-pack" % (port, npub)
    code, body = _get(url)
    print("   -> HTTP %d, %d bytes; advertises upload-pack=%s, main=%s"
          % (code, len(body), b"git-upload-pack" in body, b"refs/heads/main" in body))
    check("public: anonymous upload-pack info/refs -> 200", code == 200)
    check("public: advertises the service + refs/heads/main", b"git-upload-pack" in body and b"refs/heads/main" in body)

    # --- PRIVATE repo: read gate --------------------------------------------
    print("3) private repo: NIP-98 read gate")
    rp = ghs.create_repo(owner_hex, "privrepo", private=True, readers=[reader_hex])
    _populate_bare(rp["path"])
    purl = "http://127.0.0.1:%d/%s/privrepo.git/info/refs?service=git-upload-pack" % (port, npub)
    code_anon, body_anon = _get(purl)
    print("   anon -> HTTP %d (%d bytes)" % (code_anon, len(body_anon)))
    check("private: anonymous clone -> 401", code_anon == 401)
    check("private: 401 leaks NO refs", b"refs/heads/main" not in body_anon)

    h_reader = nip98_header(reader_sk, "GET", purl)
    code_r, body_r = _get(purl, header=h_reader)
    print("   allowlisted reader -> HTTP %d (%d bytes, main=%s)"
          % (code_r, len(body_r), b"refs/heads/main" in body_r))
    check("private: allowlisted reader NIP-98 -> 200", code_r == 200)
    check("private: reader sees refs/heads/main", b"refs/heads/main" in body_r)

    h_rando = nip98_header(rando_sk, "GET", purl)
    code_x, _ = _get(purl, header=h_rando)
    print("   non-allowlisted signer -> HTTP %d" % code_x)
    check("private: non-allowlisted signer -> 401", code_x == 401)

    httpd.shutdown()
    import shutil
    shutil.rmtree(tmp, ignore_errors=True)

    passed, total = sum(_results), len(_results)
    print("\n%d/%d checks passed" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(main())
