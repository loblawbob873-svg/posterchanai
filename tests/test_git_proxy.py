#!/usr/bin/env python3
"""Reverse-proxy verification: a PROXY node (git_server_proxy_url set) forwards smart-HTTP git
requests to a HOSTING node, preserving the Authorization/NIP-98 header — mirroring the Blossom
storage proxy. No DB needed (public repo + private read gate with readers-only ACL).

  1. proxy disabled (empty git_server_proxy_url) -> /git/... 404 (not a proxy node)
  2. public repo: proxied info/refs matches hitting the host directly (advertisement + refs)
  3. private repo: anonymous proxied clone -> 401 (host's read gate, forwarded verdict)
  4. private repo: proxied clone WITH a valid NIP-98 header -> 200 (header forwarded, host authorizes)
"""

import asyncio
import base64
import json
import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
from http.server import ThreadingHTTPServer

_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

import httpx
from fastapi import FastAPI

from app.services import git_host_service as ghs
from app.services import settings_store
from app.services.nostr import bip340, nostr_service
from app.services.nostr.event import build_event
from app.routers.git import smart_router

_results = []


def check(name, cond):
    _results.append(bool(cond))
    print("  [%s] %s" % ("PASS" if cond else "FAIL", name))


def _populate_bare(gitdir):
    env = dict(os.environ, GIT_DIR=gitdir)
    blob = subprocess.run(["git", "hash-object", "-w", "--stdin"], input=b"hi\n",
                          capture_output=True, env=env).stdout.strip()
    tree = subprocess.run(["git", "mktree"], input=("100644 blob %s\tR\n" % blob.decode()).encode(),
                          capture_output=True, env=env).stdout.strip()
    env2 = dict(env, GIT_AUTHOR_NAME="t", GIT_AUTHOR_EMAIL="t@t", GIT_COMMITTER_NAME="t",
                GIT_COMMITTER_EMAIL="t@t")
    commit = subprocess.run(["git", "commit-tree", tree.decode(), "-m", "i"],
                            capture_output=True, env=env2).stdout.strip()
    subprocess.run(["git", "update-ref", "refs/heads/main", commit.decode()], env=env)
    subprocess.run(["git", "symbolic-ref", "HEAD", "refs/heads/main"], env=env)


def _host_server(config):
    import git_host_main
    git_host_main._CONFIG = config

    class _S(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True
    httpd = _S(("127.0.0.1", 0), git_host_main._Handler)
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
    return httpd, port


def nip98_header(sk, method, url):
    ev = build_event(sk, 27235, "", tags=[["u", url], ["method", method]])
    return "Nostr " + base64.b64encode(json.dumps(ev).encode()).decode()


async def run():
    if not os.path.exists("/usr/libexec/git-core/git-http-backend"):
        print("git-http-backend missing"); return 1

    tmp = tempfile.mkdtemp(prefix="grasp_proxy_")
    ghs.GIT_PROJECT_ROOT = os.path.join(tmp, "git_repos")
    os.makedirs(ghs.GIT_PROJECT_ROOT, exist_ok=True)

    owner_sk = (11).to_bytes(32, "big")
    reader_sk = (22).to_bytes(32, "big")
    rando_sk = (33).to_bytes(32, "big")
    owner_hex = bip340.pubkey_from_seckey(owner_sk).hex()
    reader_hex = bip340.pubkey_from_seckey(reader_sk).hex()
    npub = nostr_service.npub_of(owner_hex)

    host_cfg = {"pg_dsn": "", "repo_root": _ROOT, "repo_max_mb": 512, "allow_force": True,
                "nip98_push": False, "public_base": "", "read_skew": 300, "port": 0}
    httpd, hport = _host_server(host_cfg)
    time.sleep(0.3)
    ghs.create_repo(owner_hex, "pubrepo", private=False)
    _populate_bare(ghs.repo_dir(owner_hex, "pubrepo"))
    ghs.create_repo(owner_hex, "privrepo", private=True, readers=[reader_hex])
    _populate_bare(ghs.repo_dir(owner_hex, "privrepo"))

    # Proxy front = a FastAPI app mounting smart_router; drive it via ASGITransport.
    app = FastAPI()
    app.include_router(smart_router)
    transport = httpx.ASGITransport(app=app)

    try:
        # (1) proxy disabled -> 404
        settings_store.put("git_server_proxy_url", "", write_relay=False)
        async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as c:
            r = await c.get("/git/%s/pubrepo.git/info/refs?service=git-upload-pack" % npub)
        check("proxy disabled -> /git 404", r.status_code == 404)

        # Point the proxy at the hosting node.
        settings_store.put("git_server_proxy_url", "http://127.0.0.1:%d" % hport, write_relay=False)

        # (2) public: proxied info/refs == direct
        direct = httpx.get("http://127.0.0.1:%d/%s/pubrepo.git/info/refs?service=git-upload-pack" % (hport, npub))
        async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as c:
            r = await c.get("/git/%s/pubrepo.git/info/refs?service=git-upload-pack" % npub)
        print("   proxied public: HTTP %d, %d bytes; matches-direct=%s"
              % (r.status_code, len(r.content), r.content == direct.content))
        check("public: proxied info/refs -> 200", r.status_code == 200)
        check("public: proxied body == direct host body", r.content == direct.content)
        check("public: advertises upload-pack + refs/heads/main",
              b"git-upload-pack" in r.content and b"refs/heads/main" in r.content)

        # (3) private anon via proxy -> 401 (host verdict forwarded), no refs
        purl = "/git/%s/privrepo.git/info/refs?service=git-upload-pack" % npub
        async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as c:
            r = await c.get(purl)
        print("   proxied private anon: HTTP %d" % r.status_code)
        check("private: anon proxied clone -> 401", r.status_code == 401)
        check("private: 401 leaks no refs", b"refs/heads/main" not in r.content)
        # The 401 challenge MUST survive the proxy hop, and BOTH schemes must survive it: a client
        # only authenticates against a scheme the server advertises, and ngit (libgit2) needs the
        # Basic one specifically. Collapsing them into one header silently broke private clones
        # through a proxy node while direct-to-host worked.
        ch = [v.lower() for v in r.headers.get_list("www-authenticate")]
        check("private: 401 forwards a www-authenticate challenge", len(ch) > 0)
        check("private: 401 forwards the Nostr challenge", any("nostr" in v for v in ch))
        check("private: 401 forwards the Basic challenge (ngit needs it)",
              any("basic" in v for v in ch))

        # (4) private via proxy WITH NIP-98 header -> forwarded -> 200
        full_url = "http://127.0.0.1:%d/%s/privrepo.git/info/refs" % (hport, npub)
        h_ok = nip98_header(reader_sk, "GET", full_url)
        async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as c:
            r = await c.get(purl, headers={"Authorization": h_ok})
        print("   proxied private + reader NIP-98: HTTP %d (main=%s)"
              % (r.status_code, b"refs/heads/main" in r.content))
        check("private: proxied reader NIP-98 forwarded -> 200", r.status_code == 200)
        check("private: reader sees refs/heads/main through proxy", b"refs/heads/main" in r.content)

        h_bad = nip98_header(rando_sk, "GET", full_url)
        async with httpx.AsyncClient(transport=transport, base_url="http://proxy") as c:
            r = await c.get(purl, headers={"Authorization": h_bad})
        check("private: proxied non-reader NIP-98 -> 401", r.status_code == 401)
    finally:
        settings_store.put("git_server_proxy_url", "", write_relay=False)
        httpd.shutdown()
        shutil.rmtree(tmp, ignore_errors=True)

    passed, total = sum(_results), len(_results)
    print("\n%d/%d checks passed" % (passed, total))
    return 0 if passed == total else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(run()))
