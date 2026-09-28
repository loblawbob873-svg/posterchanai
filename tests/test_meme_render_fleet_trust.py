"""A Meme Builder render forwarded to another node is actually RENDERED there.

Run: venv-unified/bin/python -m pytest tests/test_meme_render_fleet_trust.py

The render balancer (`_meme_lb_forward`) hands every other job to a peer, and the peer used to
re-check the user's instance membership against ITS OWN NIP-05 registry and relay. The peer is a
different node, so a poster.place member's forwarded render was refused there — measured in
production: server1 forwarded a render to nas at 2026-09-26 15:01:20, nas answered 503, and server1
rendered it itself. The balancer "worked" (it forwarded) and balanced nothing.

A forward now carries the fleet's shared secret (`lb_auth`, the same proof the image/music/video
balancers use, fail-closed), and a peer that sees it skips ONLY the registry question — the user's
own signature is still verified on the peer. Without a valid secret nothing changes: the membership
check runs exactly as before, so a stranger setting the routing header gets nothing.

The end-to-end tests run the real router as TWO nodes (origin + peer) over ASGI, with the render
itself stubbed (ffmpeg is not what is under test) and each node's membership answer controlled
separately.
"""
import asyncio
import base64
import contextvars
import json
import os

import httpx
import pytest
from fastapi import FastAPI, HTTPException

from app.routers import client as C
from app.services import instance_membership, meme_builder_service, stats_service, video_factory
from app.services.nostr.event import build_event
from app.utils import lb_auth

SECRET = "fleet-secret-for-tests"
ON_PEER = contextvars.ContextVar("on_peer", default=False)


def _auth():
    ev = build_event(os.urandom(32), 27235, "")
    return ev["pubkey"], base64.b64encode(json.dumps(ev).encode()).decode()


class _Req:
    def __init__(self, headers):
        self.headers = {k.lower(): v for k, v in headers.items()}


# ---- the trust rule ----------------------------------------------------------------------------

@pytest.mark.parametrize("secret,headers,trusted", [
    (SECRET, "SIGNED+FWD", True),
    (SECRET, {"x-pcai-meme-fwd": "1"}, False),                                   # routing header alone
    (SECRET, {"x-pcai-meme-fwd": "1", lb_auth.FLAG_HEADER_NAME: "true",
              lb_auth.AUTH_HEADER_NAME: "wrong"}, False),                          # wrong secret
    ("", {"x-pcai-meme-fwd": "1", lb_auth.FLAG_HEADER_NAME: "true",
          lb_auth.AUTH_HEADER_NAME: ""}, False),                                   # none configured: fail closed
    (SECRET, "SIGNED", False),                                                    # a peer call that is not a render forward
])
def test_only_a_secret_bearing_render_forward_skips_the_registry(monkeypatch, secret, headers, trusted):
    monkeypatch.setattr(lb_auth, "shared_secret", lambda: secret)
    if headers == "SIGNED+FWD":          # built HERE: lb_auth.headers() reads the secret when called
        headers = lb_auth.headers({"x-pcai-meme-fwd": "1"})
    elif headers == "SIGNED":
        headers = lb_auth.headers()
    assert C._is_fleet_forward(_Req(headers)) is trusted

    asked = []

    async def membership(pk):
        asked.append(pk)
    monkeypatch.setattr(instance_membership, "require_pubkey", membership)
    asyncio.run(C._require_member_unless_fleet_forward(_Req(headers), "ab" * 32))
    assert asked == ([] if trusted else ["ab" * 32])


def test_every_forwarded_render_route_uses_the_fleet_gate():
    """render/effect/apply-effect/talk/magic-erase are the routes `_meme_lb_forward` sends to a peer. A route
    left on the bare membership check is the same no-op balancer again, for that route only."""
    import inspect
    for fn in (C.meme_render, C.meme_effect, C.meme_apply_effect, C.meme_talk, C.meme_magic_erase):
        src = inspect.getsource(fn)
        assert "_require_member_unless_fleet_forward(request, pk)" in src, fn.__name__
        assert "await require_pubkey(pk)" not in src, fn.__name__


# ---- two real nodes ------------------------------------------------------------------------------

def _node():
    app = FastAPI()
    app.include_router(C.router)
    return app


@pytest.fixture
def fleet(monkeypatch):
    """origin + peer; membership passes on the origin and fails like nas's did on the peer."""
    peer = _node()

    async def peer_asgi(scope, receive, send):
        token = ON_PEER.set(True)
        try:
            await peer(scope, receive, send)
        finally:
            ON_PEER.reset(token)

    Real = httpx.AsyncClient

    class ToPeer(Real):
        def __init__(self, *a, **k):
            k["transport"] = httpx.ASGITransport(peer_asgi)
            super().__init__(*a, **k)

    state = {"membership": []}

    async def membership(pk):
        state["membership"].append("peer" if ON_PEER.get() else "origin")
        if ON_PEER.get():
            raise HTTPException(503, "Instance profile verification is temporarily unavailable")

    def render(edit, sources):
        return ((b"GIF89a-rendered-on-" + (b"peer" if ON_PEER.get() else b"origin")), "image/gif")

    monkeypatch.setattr(instance_membership, "require_pubkey", membership)
    monkeypatch.setattr(meme_builder_service, "render", render)
    monkeypatch.setattr(stats_service, "bump", lambda *a, **k: None)
    monkeypatch.setattr(video_factory, "parse_video_server_urls", lambda raw: ["http://peer:3051"])
    monkeypatch.setattr(C, "_meme_rr", [1])            # the rotation's next turn is the peer's
    monkeypatch.setattr(C, "_meme_busy", [0])
    monkeypatch.setattr(httpx, "AsyncClient", ToPeer)   # _meme_lb_forward's client reaches the peer
    state["origin"] = _node()
    state["client"] = lambda: Real(transport=httpx.ASGITransport(state["origin"]), base_url="http://origin")
    return state


def _render(fleet):
    pk, auth = _auth()
    body = {"pubkey": pk, "auth": auth, "edit": {"w": 64, "h": 64, "fps": 10, "duration": 1, "layers": []}}

    async def go():
        async with fleet["client"]() as c:
            return await c.post("/client/meme/render", json=body)
    return asyncio.run(go())


def test_with_the_fleet_secret_the_peer_renders_the_forward(monkeypatch, fleet):
    monkeypatch.setattr(lb_auth, "shared_secret", lambda: SECRET)
    r = _render(fleet)
    assert r.status_code == 200
    assert r.content == b"GIF89a-rendered-on-peer", "the forwarded render must run on the PEER"
    assert fleet["membership"] == ["origin"], \
        "membership is checked once, on the node the user asked — the peer must not re-derive it"


def test_without_the_secret_the_peer_still_checks_and_the_origin_renders(monkeypatch, fleet):
    """Fail closed: no shared secret → the peer runs the ordinary gate (503 here, as nas did) and
    the origin renders it itself — exactly the behaviour before this change, never an error."""
    monkeypatch.setattr(lb_auth, "shared_secret", lambda: "")
    r = _render(fleet)
    assert r.status_code == 200
    assert r.content == b"GIF89a-rendered-on-origin"
    assert fleet["membership"] == ["origin", "peer"]


def test_a_stranger_cannot_use_the_routing_header_to_skip_membership(monkeypatch, fleet):
    """Straight to a node with the forward header and no secret: the registry check still runs."""
    monkeypatch.setattr(lb_auth, "shared_secret", lambda: SECRET)
    pk, auth = _auth()
    body = {"pubkey": pk, "auth": auth, "edit": {"w": 64, "h": 64, "fps": 10, "duration": 1, "layers": []}}
    refused = {"n": 0}

    async def membership(pk):
        refused["n"] += 1
        raise HTTPException(403, "not a member")
    monkeypatch.setattr(instance_membership, "require_pubkey", membership)

    async def go():
        async with fleet["client"]() as c:
            return await c.post("/client/meme/render", json=body, headers={"x-pcai-meme-fwd": "1"})
    r = asyncio.run(go())
    assert r.status_code == 403 and refused["n"] == 1
