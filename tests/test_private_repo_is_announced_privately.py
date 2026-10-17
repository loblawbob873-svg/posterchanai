"""A private repo made in the web UI is ANNOUNCED -- tagged ["private","true"], to this node's relay only -- so it
is listed under Git -> Mine.

"Git -> Create repo, not listed under Mine. Do I have to commit files first?" (2026-10-17): no. The host and the
client still followed the pre-GRASP-08 rule "private = never announced", so the repo had no announcement at all,
and Mine lists announcements. Every other private repo here (configs, admintools) is announced with the tag; the
relay serves it only to maintainers and readers.
"""
import base64
import json
import threading
import time
from http.server import ThreadingHTTPServer
from pathlib import Path

from tests.test_grasp08_base_path import BASE, OWNER, OWNER_SK, gh, git_auth, nostr_service, build_event

ROOT = Path(__file__).resolve().parents[1]


def _create(tmp_path, monkeypatch, body):
    monkeypatch.setenv("GRASP_GIT_PROJECT_ROOT", str(tmp_path))
    npub = nostr_service.npub_of(OWNER)
    monkeypatch.setattr(gh, "_CONFIG", {"relay_port": 0, "public_base": BASE, "allowlist": npub,
                                        "relay_url": "wss://poster.place/relay", "write_skew": 120,
                                        "read_skew": 60, "port": 0}, raising=False)

    class _S(ThreadingHTTPServer):
        allow_reuse_address = True
        daemon_threads = True
    httpd = _S(("127.0.0.1", 0), gh._Handler)
    threading.Thread(target=httpd.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True).start()
    try:
        import urllib.request
        url = "http://127.0.0.1:%d/git/%s/secret.git/create" % (httpd.server_address[1], npub)
        ev = build_event(OWNER_SK, git_auth.NIP98_KIND, "", tags=[["u", url], ["method", "POST"]],
                         created_at=int(time.time()))
        req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST")
        req.add_header("Authorization", "Nostr " + base64.b64encode(json.dumps(ev).encode()).decode())
        req.add_header("Content-Type", "application/json")
        with urllib.request.urlopen(req, timeout=20) as r:
            return json.loads(r.read())
    finally:
        httpd.shutdown()


def test_the_host_hands_back_a_private_announcement(tmp_path, monkeypatch):
    out = _create(tmp_path, monkeypatch, {"private": True, "name": "Secret"})
    assert out.get("ok") and out.get("private") is True, out
    tags = out.get("announce_tags_30617") or []
    assert ["private", "true"] in tags, ("a private repo got no announcement to sign", out)
    assert ["d", "secret"] in tags and ["maintainers", OWNER] in tags, tags
    assert git_auth.event_says_private({"tags": tags}), "the relay and the host would not read it as private"


def test_a_public_repo_is_not_tagged_private(tmp_path, monkeypatch):
    out = _create(tmp_path, monkeypatch, {"private": False})
    assert out.get("ok") and ["private", "true"] not in (out.get("announce_tags_30617") or []), out


def test_the_client_sends_a_private_announcement_to_this_node_only():
    """The wider relay list would show a private repo to everybody: the client publishes it to Relay._home."""
    src = (ROOT / "static/js/client/git.js").read_text()
    start = src.index("async function _doCreateRepo")
    body = src[start:src.index("\n  }\n", start)]
    priv = body[body.index("if(priv &&"):body.index("if(!priv &&")]
    assert "Relay.publishTo([home]" in priv and "Relay._home" in priv, priv
    assert "publish(30617" not in priv, "a private announcement must not go through the general publish"
