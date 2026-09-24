"""The fediverse's reads of our public documents are built ONCE and served from memory.

Measured 2026-09-24: minutes after subscribing to a relay, ~37,000 fetches in three minutes -- each
recent post fetched ~1,200 times by the relay's subscribers -- and every fetch rebuilt the document on
the one app worker, so the web client stopped loading. These run the shipped middleware around a real
FastAPI app whose handler COUNTS its builds.
"""
import asyncio

import httpx
import pytest
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response

from app.services.activitypub import readcache


@pytest.fixture
def app():
    readcache.clear()
    builds = {"n": 0}
    a = FastAPI()

    @a.get("/ap/objects/{eid}")
    async def obj(eid: str, request: Request):
        builds["n"] += 1
        await asyncio.sleep(0.05)                      # a real build takes time; others arrive meanwhile
        if "text/html" in request.headers.get("accept", ""):
            return RedirectResponse(f"/note/{eid}", status_code=302)
        if eid == "gone":
            return Response(status_code=410)
        if eid == "boom":
            return PlainTextResponse("err", status_code=500)
        if eid == "cookie":
            r = JSONResponse({"id": eid})
            r.set_cookie("s", "1")
            return r
        return JSONResponse({"id": eid, "n": builds["n"]})

    @a.post("/ap/inbox")
    async def inbox():
        builds["n"] += 1
        return Response(status_code=202)

    @a.get("/client/thing")
    async def other():
        builds["n"] += 1
        return JSONResponse({})

    a.add_middleware(readcache.ActivityPubReadCache)
    a.state.builds = builds
    return a


def _run(app, calls):
    async def go():
        transport = httpx.ASGITransport(app=app)
        async with httpx.AsyncClient(transport=transport, base_url="https://poster.test") as c:
            return await asyncio.gather(*(c.request(m, u, headers=h or {}) for m, u, h in calls))
    return asyncio.run(go())


AP = {"Accept": "application/activity+json"}


def test_fifty_simultaneous_fetches_of_one_post_build_it_once(app):
    rs = _run(app, [("GET", "/ap/objects/p1", AP)] * 50)
    assert all(r.status_code == 200 for r in rs)
    assert app.state.builds["n"] == 1, "a relay burst still rebuilt the document per request"
    assert len({r.content for r in rs}) == 1
    _run(app, [("GET", "/ap/objects/p1", AP)] * 10)
    assert app.state.builds["n"] == 1                   # and later fetches are served from memory


def test_a_browser_and_a_server_get_their_own_answer(app):
    html, ap = _run(app, [("GET", "/ap/objects/p2", {"Accept": "text/html"}), ("GET", "/ap/objects/p2", AP)])
    assert html.status_code == 302 and ap.status_code == 200


def test_an_expired_entry_is_built_again(app, monkeypatch):
    _run(app, [("GET", "/ap/objects/p3", AP)])
    real = readcache.time.monotonic
    monkeypatch.setattr(readcache.time, "monotonic", lambda: real() + readcache.TTL_OK + 1)
    _run(app, [("GET", "/ap/objects/p3", AP)])
    assert app.state.builds["n"] == 2


def test_gone_is_cached_briefly_and_errors_are_never_cached(app):
    _run(app, [("GET", "/ap/objects/gone", AP)] * 3)
    assert app.state.builds["n"] == 1
    _run(app, [("GET", "/ap/objects/boom", AP)])
    _run(app, [("GET", "/ap/objects/boom", AP)])
    assert app.state.builds["n"] == 3, "a 500 was cached"


def test_what_must_never_be_shared_is_not(app):
    _run(app, [("GET", "/ap/objects/cookie", AP)] * 2)            # a Set-Cookie answer
    assert app.state.builds["n"] == 2
    _run(app, [("POST", "/ap/inbox", AP)] * 2)                    # not a read
    _run(app, [("GET", "/client/thing", AP)] * 2)                 # not an ActivityPub path
    assert app.state.builds["n"] == 6
    _run(app, [("GET", "/ap/objects/p4", dict(AP, Origin="https://x.example"))] * 2)   # a browser: CORS varies
    assert app.state.builds["n"] == 8
