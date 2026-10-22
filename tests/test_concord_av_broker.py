"""The CORD-07 call broker (app/routers/concord_av.py) and the LiveKit config it serves (livekit_service).

The broker is BLIND: it never learns who calls, only that the caller holds the room key. So every rule it has is
what stands between "anyone" and "a member": the grant must be signed BY the room key, FOR this exact URL, fresh,
once. Each test below is a grant a forger or a replayer could send, and the exact field that stops it. The token
is decoded and checked claim by claim, because a token valid for more than its one room is the same hole.
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from pathlib import Path

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import concord_av
from app.services import livekit_service
from app.services.nostr.event import build_event

ROOT = Path(__file__).resolve().parents[1]
HOST = "poster.place"


def keypair():
    sk = secrets.token_bytes(32)
    pk = build_event(sk, 1, "", [])["pubkey"]
    return sk, pk


def grant(sk, url, *, at=None, method="GET", nonce=None, kind=27235, extra=()):
    tags = [["u", url], ["method", method], ["nonce", nonce or secrets.token_hex(16)], *extra]
    ev = build_event(sk, kind, "", tags, created_at=int(at if at is not None else time.time()))
    return "Concord " + base64.b64encode(json.dumps(ev).encode()).decode()


def url_for(room):
    return f"https://{HOST}/.well-known/concord/av/{room}"


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(livekit_service, "available", lambda: True)
    monkeypatch.setattr(livekit_service, "credentials", lambda cfg=None: ("PCkey", "s" * 48))
    monkeypatch.setattr(livekit_service, "_cfg", lambda: {})
    concord_av._nonces.clear(); concord_av._hits.clear()
    app = FastAPI(); app.include_router(concord_av.router)
    return TestClient(app, base_url=f"https://{HOST}")


def decode(token, secret="s" * 48):
    h, c, s = token.split(".")
    pad = lambda x: x + "=" * (-len(x) % 4)
    want = hmac.new(secret.encode(), f"{h}.{c}".encode(), hashlib.sha256).digest()
    assert base64.urlsafe_b64decode(pad(s)) == want, "token signature does not verify with the API secret"
    assert json.loads(base64.urlsafe_b64decode(pad(h))) == {"alg": "HS256", "typ": "JWT"}
    return json.loads(base64.urlsafe_b64decode(pad(c)))


def test_the_probe_says_whether_this_node_hosts_calls(client, monkeypatch):
    r = client.get("/.well-known/concord/av")
    assert r.status_code == 204 and r.headers["access-control-allow-origin"] == "*"
    monkeypatch.setattr(livekit_service, "available", lambda: False)
    assert client.get("/.well-known/concord/av").status_code == 404, "a node with no SFU must not claim to broker"


def test_a_grant_signed_by_the_room_key_gets_a_token_for_that_room_only(client):
    sk, room = keypair()
    r = client.get(f"/.well-known/concord/av/{room}", headers={"Authorization": grant(sk, url_for(room))})
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["url"] == f"wss://{HOST}/livekit"
    claims = decode(body["token"])
    assert claims["iss"] == "PCkey" and claims["sub"] == body["identity"]
    assert claims["video"] == {"room": room, "roomJoin": True, "canPublish": True, "canSubscribe": True,
                               "canPublishData": True}, "the token grants more than one room's call"
    assert 0 < claims["exp"] - time.time() <= concord_av.TOKEN_TTL
    assert r.headers["access-control-allow-origin"] == "*"


@pytest.mark.parametrize("case", ["other_key", "other_url", "other_host", "stale", "future", "post", "kind", "tampered",
                                  "no_nonce", "garbage", "missing"])
def test_a_forged_or_misdirected_grant_is_refused(client, case):
    sk, room = keypair()
    osk, _other = keypair()
    h = {
        "other_key": lambda: grant(osk, url_for(room)),
        "other_url": lambda: grant(sk, url_for("a" * 64)),
        "other_host": lambda: grant(sk, f"https://armada.buzz/.well-known/concord/av/{room}"),
        "stale": lambda: grant(sk, url_for(room), at=time.time() - 120),
        "future": lambda: grant(sk, url_for(room), at=time.time() + 120),
        "post": lambda: grant(sk, url_for(room), method="POST"),
        "kind": lambda: grant(sk, url_for(room), kind=27236),
        "no_nonce": lambda: grant(sk, url_for(room), nonce="x"),
        "garbage": lambda: "Concord !!notbase64!!",
        "missing": lambda: "",
    }
    if case == "tampered":
        ev = json.loads(base64.b64decode(grant(sk, url_for(room)).split(" ", 1)[1]))
        ev["tags"].append(["x", "y"])
        auth = "Concord " + base64.b64encode(json.dumps(ev).encode()).decode()
    else:
        auth = h[case]()
    r = client.get(f"/.well-known/concord/av/{room}", headers={"Authorization": auth})
    assert r.status_code == 401, (case, r.status_code, r.text)
    assert "token" not in r.text


def test_a_grant_works_once(client):
    sk, room = keypair()
    auth = grant(sk, url_for(room))
    assert client.get(f"/.well-known/concord/av/{room}", headers={"Authorization": auth}).status_code == 200
    again = client.get(f"/.well-known/concord/av/{room}", headers={"Authorization": auth})
    assert again.status_code == 401 and "already used" in again.text


def test_a_bad_room_and_a_flood_are_answered_without_work(client):
    assert client.get("/.well-known/concord/av/NOTHEX").status_code == 400
    sk, room = keypair()
    codes = [client.get(f"/.well-known/concord/av/{room}", headers={"Authorization": grant(sk, url_for(room))}).status_code
             for _ in range(concord_av.RATE_PER_MINUTE + 3)]
    assert codes.count(429) == 3, codes


def test_no_sfu_means_no_token(client, monkeypatch):
    monkeypatch.setattr(livekit_service, "available", lambda: False)
    sk, room = keypair()
    r = client.get(f"/.well-known/concord/av/{room}", headers={"Authorization": grant(sk, url_for(room))})
    assert r.status_code == 404


def test_other_origins_can_call_the_broker_through_the_app_cors_layer():
    """The app's credentialed CORS middleware 400s a preflight from an origin it does not list. Armada and the APK
    call the broker cross-origin, so it must be one of the paths that answer CORS themselves."""
    import ast
    tree = ast.parse((ROOT / "app/main.py").read_text())
    own = next(ast.literal_eval(n.value) for n in ast.walk(tree)
               if isinstance(n, ast.Assign) and any(getattr(t, "id", "") == "_OWN_CORS" for t in n.targets))
    assert any("/.well-known/concord/av/x".startswith(p) for p in own), own


def test_the_livekit_config_is_one_udp_port_loopback_signalling_and_our_turn():
    cfg = {"turn_public_ip": "203.0.113.7", "turn_enabled": "true", "turn_shared_secret": "abc", "turn_domain": "turn.poster.place"}
    text = livekit_service.render_config(cfg, "PCkey", "sec")
    assert 'bind_addresses: ["127.0.0.1"]' in text and "udp_port: 7882" in text and "tcp_port: 7881" in text
    assert "port_range_start" not in text, "a port range switches LiveKit's UDP mux off"
    assert "node_ip: 203.0.113.7" in text and "use_external_ip: false" in text
    assert "host: turn.poster.place" in text and 'secret: "abc"' in text
    assert "level: warn" in text
    off = livekit_service.render_config({"turn_public_ip": "203.0.113.7"}, "PCkey", "sec")
    assert "turn_servers" not in off, "TURN advertised while the relay is off"
    evil = livekit_service.render_config({"turn_public_ip": "1.2.3.4\nkeys:\n  x: y"}, "PCkey", "sec")
    assert "node_ip" not in evil and "x: y" not in evil, "a setting injected YAML"


def test_the_installer_pins_and_checks_the_binary():
    text = (ROOT / "scripts/install/livekit.sh").read_text()
    assert 'LIVEKIT_VERSION_PINNED="v' in text and "sha256sum" in text and "checksum mismatch" in text
    for arch in ("AMD64", "ARM64", "ARMV7"):
        assert len(text.split(f'LIVEKIT_SHA256_{arch}="', 1)[1].split('"', 1)[0]) == 64


def test_the_docker_image_and_the_installer_pin_the_same_livekit():
    sh = (ROOT / "scripts/install/livekit.sh").read_text()
    dockerfile = (ROOT / "Dockerfile").read_text()
    ver = sh.split('LIVEKIT_VERSION_PINNED="v', 1)[1].split('"', 1)[0]
    assert f"ARG LIVEKIT_VERSION={ver}" in dockerfile
    for arch in ("AMD64", "ARM64", "ARMV7"):
        want = sh.split(f'LIVEKIT_SHA256_{arch}="', 1)[1].split('"', 1)[0]
        assert f"ARG LIVEKIT_SHA256_{arch}={want}" in dockerfile, arch
    assert "COPY --from=streamdl /livekit-server /app/livekit/livekit-server" in dockerfile
    assert "setup_livekit_server" in (ROOT / "scripts/install/update.sh").read_text(), "an upgrade must bring LiveKit"


def test_an_empty_placeholder_binary_is_not_installed(tmp_path, monkeypatch):
    fake = tmp_path / "livekit-server"
    fake.write_bytes(b"")
    monkeypatch.setattr(livekit_service, "_BIN", fake)
    assert not livekit_service.installed()
    assert not livekit_service.wanted({"livekit_enabled": "true", "turn_public_ip": "203.0.113.7"})
    fake.write_bytes(b"\x7fELF")
    assert livekit_service.installed()
