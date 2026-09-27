"""A private repository appears on its owner's Git page and can be browsed there.

Run: venv-unified/bin/python -m pytest tests/test_private_repos_on_git_page.py

Reported 2026-09-27 as "I don't see configs in my git page" -- and it was every private repo, always:
  * the relay withheld a private 30617 from an unauthenticated connection with an ordinary EOSE, which
    reads exactly like "no such repo", and the web client never authenticates for a repo listing;
  * even listed, a private repo's every browse call reached the git host unsigned and came back 404.
Each half is RUN here: the relay's `_on_req`, the shipped relay.js under node, git.js's read-token
helper, and the app routes that pass the viewer's credential through (and must never cache it).
"""
import asyncio
import json
import os
import re
import shutil
import subprocess

import pytest

from app.services.nostr_relay.server import RelayServer

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
NODE = shutil.which("node")
OWNER, STRANGER = "a" * 64, "c" * 64


# ============================== the relay says when it withheld something ============================

def _repo(d, private):
    tags = [["d", d]] + ([["private", "true"]] if private else [])
    return {"id": d.ljust(64, "0"), "kind": 30617, "pubkey": OWNER, "created_at": 1, "tags": tags,
            "content": "", "sig": "0" * 128}


def _req(filters, authed=()):
    s = object.__new__(RelayServer)
    conn = object()
    sent, challenged = [], []
    s._auth_pubkeys = {conn: set(authed)}
    s.cfg = {}
    s.subs = type("Subs", (), {"count": staticmethod(lambda c: 0), "add": staticmethod(lambda *a, **k: None)})()
    events = [_repo("configs", True), _repo("website", False)]

    class Store:
        async def query(self, fs):
            return list(events)
    s.store = Store()
    s._send = lambda c, m: sent.append(m)
    s._challenge = lambda c: challenged.append(c)
    asyncio.run(s._on_req(conn, "sub", filters))
    return sent, challenged


def test_asking_for_an_authors_repos_unauthenticated_is_told_to_authenticate():
    sent, challenged = _req([{"kinds": [30617], "authors": [OWNER]}])
    served = [m[2]["tags"][0][1] for m in sent if m[0] == "EVENT"]
    assert served == ["website"], "the private repo leaked, or the public one was held back"
    assert sent[-1][:2] == ["CLOSED", "sub"] and sent[-1][2].startswith("auth-required:"), sent[-1]
    assert challenged, "no AUTH challenge to answer"


def test_the_owner_authenticated_gets_both_and_an_ordinary_end():
    sent, _ = _req([{"kinds": [30617], "authors": [OWNER]}], authed=[OWNER])
    assert sorted(m[2]["tags"][0][1] for m in sent if m[0] == "EVENT") == ["configs", "website"]
    assert sent[-1] == ["EOSE", "sub"]


def test_a_broad_listing_never_asks_anybody_to_authenticate():
    sent, challenged = _req([{"kinds": [30617], "limit": 5000}])
    assert sent[-1] == ["EOSE", "sub"] and not challenged
    assert [m[2]["tags"][0][1] for m in sent if m[0] == "EVENT"] == ["website"]


# ============================== the client signs in for its own repos only ===========================

@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_the_client_signs_in_once_for_its_own_repos_and_never_for_anyone_elses():
    r = subprocess.run([NODE, os.path.join(ROOT, "tests/client/private_repo_auth_runtime.mjs")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0 and "ok" in r.stdout, r.stdout + r.stderr


def test_the_git_page_asks_for_the_owners_repos_bound_to_them():
    src = open(os.path.join(ROOT, "static/js/client/git.js"), encoding="utf-8").read()
    body = src[src.index("async function renderRepos()"):src.index("function repoCard(")]
    assert re.search(r"kinds:\[30617\],\s*authors:\[_me\]", body), \
        "the Git page no longer asks for the owner's own repos -- private ones cannot appear"


# ============================== browsing reads with the viewer's signature ===========================

@pytest.mark.skipif(not NODE, reason="node is not installed")
def test_one_read_token_serves_a_whole_repo_view_and_public_repos_sign_nothing():
    """A repo view makes a dozen reads at once; a remote signer must not be asked a dozen times."""
    from tests.client.test_git_client import _lift
    js = _lift(["_repoIsPrivate", "_rvReadAuth", "_rvFetch"]) + r"""
let _rvTok=null, signs=0, fetched=[];
const S={GUEST:false, ME:{pubkey:'a'.repeat(64)}};
async function sign(kind, content, tags){ signs++; await new Promise(r=>setTimeout(r,5));
  return {kind, content, tags, pubkey:S.ME.pubkey, id:'1'.repeat(64), sig:'2'.repeat(128), created_at:1}; }
async function fetch(u, opt){ fetched.push((opt&&opt.headers&&opt.headers['X-Git-Auth'])||''); return {}; }
let _rv={cloneUrl:'https://poster.place/git/npub1x/configs.git', private:true};
(async()=>{
  await Promise.all([1,2,3,4,5,6].map(()=>_rvFetch('/client/git/tree')));
  const privSigns=signs, headers=fetched.slice();
  const tok=JSON.parse(atob(headers[0].slice(6)));
  _rv={cloneUrl:'https://poster.place/git/npub1x/site.git', private:false}; fetched=[];
  await _rvFetch('/client/git/tree');
  console.log(JSON.stringify({privSigns, headers, tok, publicSigns:signs-privSigns, publicHeader:fetched[0],
    detected:[_repoIsPrivate({tags:[['private','TRUE']]}), _repoIsPrivate({tags:[['d','x']]})]}));
})();
"""
    r = subprocess.run([NODE, "-e", js], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    out = json.loads(r.stdout)
    assert out["privSigns"] == 1, f"{out['privSigns']} signatures for one repo view"
    assert all(h.startswith("Nostr ") for h in out["headers"]) and len(set(out["headers"])) == 1
    tags = dict((t[0], t[1]) for t in out["tok"]["tags"])
    assert out["tok"]["kind"] == 27235 and tags == {"u": "https://poster.place/git/npub1x/configs.git", "method": "GET"}
    assert out["publicSigns"] == 0 and out["publicHeader"] == "", "a public repo asked for a signature"
    assert out["detected"] == [True, False]


class _Req:
    def __init__(self, auth=None, query=None):
        self.headers = {"x-git-auth": auth} if auth else {}
        self.query_params = query or {}


def test_the_app_passes_the_viewers_credential_to_the_git_host(monkeypatch):
    from app.routers import client
    seen = []

    async def fake_json(u, timeout=10.0, headers=None):
        seen.append(headers or {})
        return {"entries": []}, None
    monkeypatch.setattr(client, "_grasp_json", fake_json)
    monkeypatch.setattr(client, "_grasp_host_target", lambda url: ("http://127.0.0.1:3053", "npub1x", "configs"))
    url = "https://poster.place/git/npub1x/configs.git"
    asyncio.run(client.git_tree(_Req("Nostr abc"), url=url))
    asyncio.run(client.git_refs(_Req(query={"auth": "Nostr def"}), url=url))
    asyncio.run(client.git_log(_Req(), url=url))
    assert seen == [{"Authorization": "Nostr abc"}, {"Authorization": "Nostr def"}, {}], seen


def test_a_signed_readme_is_never_cached_or_served_from_the_cache(monkeypatch):
    """The README cache is shared by every caller: a private README fetched with its owner's credential
    must never be handed to the next anonymous caller."""
    from app.routers import client
    url = "https://poster.place/git/npub1x/configs.git"
    client._readme_cache.pop(url, None)
    got = []

    async def fake_readme(u, headers=None):
        got.append(headers)
        return "# secret plans" if headers else None
    monkeypatch.setattr(client, "_grasp_readme", fake_readme)
    signed = json.loads(asyncio.run(client.git_readme(_Req("Nostr tok"), url=url)).body)
    assert signed["ok"] and "secret" in signed["markdown"]
    assert url not in client._readme_cache, "a signed README went into the shared cache"
    assert got[0] == {"Authorization": "Nostr tok"}
