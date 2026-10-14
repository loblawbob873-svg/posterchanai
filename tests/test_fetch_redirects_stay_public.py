"""A public URL that REDIRECTS into this node's own network is refused at the hop (security review #5).

`/client/proxy-image`, `/client/xdc`, `/client/lnurl` and `/client/nip05` are public. They checked the URL
they were given and then followed redirects blindly, so `https://evil.example/r` answering
`302 http://127.0.0.1:9997/v3/config/global/get` read MediaMTX's unauthenticated API -- whose config holds
the stream-hook secret -- and handed the body back to the caller. LAN hosts (router.lan, nas.lan) the same.

Real routes, real httpx clients with their own redirect handling; only the network is a MockTransport, and the
name lookups are a table (evil.example is public, everything RFC1918/loopback is not).
"""
import httpx
import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.routers import client as client_router

SECRET = b'{"authHTTPAddress":"","runOnReadyHook":"stream-hook-secret-123","lnurl":"x","callback":"x","names":{"_":"' + b"a" * 64 + b'"}}'
INTERNAL = "http://127.0.0.1:9997/v3/config/global/get"


@pytest.fixture
def api(monkeypatch):
    asked = []

    def handler(request: httpx.Request):
        asked.append(str(request.url))
        if request.url.host == "evil.example":
            return httpx.Response(302, headers={"location": INTERNAL})
        return httpx.Response(200, content=SECRET, headers={"content-type": "image/png"})

    real = httpx.AsyncClient.__init__

    def init(self, *a, **kw):
        kw["transport"] = httpx.MockTransport(handler)
        real(self, *a, **kw)
    monkeypatch.setattr(httpx.AsyncClient, "__init__", init)

    from app.services import rss_service, search_service
    public = lambda u: "evil.example" in str(u)          # noqa: E731
    monkeypatch.setattr(rss_service, "is_safe_host", public)
    monkeypatch.setattr(client_router, "_is_public_host", lambda h: h == "evil.example")
    monkeypatch.setattr(search_service, "is_safe_url", lambda u: (True, "") if public(u) else (False, "private"))
    from app.routers import chat
    monkeypatch.setattr(chat, "is_safe_url", lambda u: (True, "") if public(u) else (False, "private"))

    app = FastAPI()
    app.include_router(client_router.router)
    return TestClient(app, raise_server_exceptions=False), asked


@pytest.mark.parametrize("path", [
    "/client/proxy-image?url=https://evil.example/x.png",
    "/client/xdc?url=https://evil.example/app.xdc",
    "/client/lnurl?url=https://evil.example/.well-known/lnurlp/bob",
    "/client/nip05?domain=evil.example&name=_",
])
def test_a_redirect_into_this_node_is_never_followed(api, path):
    c, asked = api
    r = c.get(path)
    assert any("evil.example" in u for u in asked), ("the route never fetched the public URL", asked)
    assert INTERNAL not in asked, ("the route followed a redirect to this node's own API", path, asked)
    assert b"stream-hook-secret-123" not in r.content, (path, r.status_code, r.content[:200])

