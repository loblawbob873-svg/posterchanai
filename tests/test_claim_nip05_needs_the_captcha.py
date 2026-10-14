"""A NEW instance name needs the signup captcha (security review #6).

A name this node grants IS membership (AI, image/music generation, Blossom uploads, Mail, Git, the Concord
tunnel). /client/claim-nip05 granted one to any fresh keypair with a self-signed proof while registration was
open, so a loop of keypairs was a loop of members; the captcha only guarded /client/signup-follow, which the
client calls first. The real handler, a stubbed name registry.
"""
import asyncio
import base64
import json
from types import SimpleNamespace

import pytest

from app.routers import client as client_router
from app.services import registration_service, settings_store
from app.services.nostr.event import build_event

SK = (0xC0FFEE).to_bytes(32, "big")


def _proof():
    pk = build_event(SK, 27235, "x")["pubkey"]
    ev = build_event(SK, 27235, "claim-nip05", tags=[["p", pk]])
    return pk, base64.b64encode(json.dumps(ev).encode()).decode()


@pytest.fixture
def registry(monkeypatch):
    saved = {}
    monkeypatch.setattr(registration_service, "enabled", lambda: True)
    monkeypatch.setattr(settings_store, "get", lambda k, d=None: saved.get(k, d or ""))
    monkeypatch.setattr(client_router, "_nip05_domain", lambda req, db: "node.test")
    from app.services.activitypub import actors

    async def free(name, pk):
        return False
    monkeypatch.setattr(actors, "name_is_someone_elses_handle", free)
    monkeypatch.setattr(client_router, "_captcha_passed", {})
    return saved


def _claim(pk, auth):
    data = SimpleNamespace(pubkey=pk, name="bot", auth=auth)
    return asyncio.run(client_router.claim_nip05(data, request=None, db=None))


def test_a_key_that_never_solved_the_captcha_gets_no_name(registry):
    pk, auth = _proof()
    r = _claim(pk, auth)
    assert r.status_code == 403 and json.loads(r.body)["error"] == "captcha", r.body


def test_the_signup_flow_still_gets_its_name(registry, monkeypatch):
    pk, auth = _proof()
    client_router._captcha_passed_note(pk)                 # what /signup-follow does after the captcha
    monkeypatch.setattr(settings_store, "put", lambda k, v: registry.__setitem__(k, v))   # never the real store
    from app.services.nostr_relay import thread
    monkeypatch.setattr(thread, "trigger_nip05_reload", lambda: None)
    body = json.loads(_claim(pk, auth).body)
    assert body.get("ok") and body["nip05"] == "bot@node.test", body
    assert registry["nostr_relay_nip05_names"].startswith("bot npub1"), registry
