"""The image proxy (`/client/proxy-image`, no login, and `/api/chat/proxy-image`) never reaches this node's network.

It checked a URL with `is_safe_url` and then let httpx resolve the name AGAIN to connect -- the DNS-rebinding hole the
Web Search reader was closed against on 2026-10-18 (redirect_guard.PinnedTransport), still open here. Found by the
2026-10-24 route-coverage scan. Same harness as tests/test_web_reader_fetches_safely.py: the shipped guard and fetch
code, with only DNS and the TCP dial replaced; any request that reaches "this node's own network" is recorded there.
"""
import asyncio
from types import SimpleNamespace

import httpx
import pytest
from fastapi import FastAPI

from tests import reader_fake_web as FW

D = FW.DOMAIN
CDN = "cdn" + D


@pytest.fixture
def web(monkeypatch):
    from app.services import search_service as S
    w = FW.FakeWeb()
    w.install(monkeypatch)
    w.dns[CDN] = [FW.PUBLIC[0]]
    w.page(CDN, "/cat.png", FW.PNG, ctype="image/png")
    monkeypatch.setattr(S, "own_media_hosts", lambda: set(), raising=False)
    yield w
    w.close()


@pytest.fixture
def call(web):
    from app.routers import client as client_router, chat as chat_router
    from app.auth import get_current_user
    from app.database import get_db
    app = FastAPI()
    app.include_router(client_router.router)
    app.include_router(chat_router.router)
    app.dependency_overrides[get_current_user] = lambda: SimpleNamespace(id=1, username="m", is_admin=False)
    app.dependency_overrides[get_db] = lambda: None

    def go(path, url):
        async def run():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://node.lan",
                                         timeout=60) as c:
                return await c.get(path, params={"url": url})
        return asyncio.run(run())
    return go


ENDPOINTS = ["/client/proxy-image", "/api/proxy-image"]


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_a_public_image_is_proxied(web, call, endpoint):
    r = call(endpoint, "http://%s/cat.png" % CDN)
    assert r.status_code == 200 and r.content == FW.PNG, (r.status_code, r.text[:200])


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_a_private_address_asked_for_directly_is_refused(web, call, endpoint):
    call(endpoint, "http://127.0.0.1:%d/secret" % web.internal_port)
    assert web.internal_hits == [], web.internal_hits


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("flip_after", [1, 2, 3])
def test_a_name_that_answers_public_then_loopback_cannot_reach_this_node(web, call, endpoint, flip_after):
    """DNS rebinding: the first lookups (the URL check, the redirect hook's own check) see a public address, the
    connect's lookup a moment later sees loopback. Where exactly the flip lands depends on the attacker's timing."""
    web.dns["rebind" + D] = lambda n: [FW.PUBLIC[1]] if n <= flip_after else ["127.0.0.1"]
    r = call(endpoint, "http://rebind%s:%d/secret" % (D, web.internal_port))
    assert FW.SECRET.encode() not in r.content
    assert web.internal_hits == [], ("the image proxy reached this node's own network", web.internal_hits)


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_a_name_with_a_dead_public_answer_and_a_loopback_one_is_refused(web, call, endpoint):
    web.dns["twoface" + D] = [FW.DEAD_PUBLIC, "127.0.0.1"]
    call(endpoint, "http://twoface%s:%d/secret" % (D, web.internal_port))
    assert web.internal_hits == [], web.internal_hits


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_a_redirect_to_a_private_address_is_refused(web, call, endpoint):
    web.page(CDN, "/hop.png", "", status=302, headers={"Location": "http://127.0.0.1:%d/secret" % web.internal_port})
    call(endpoint, "http://%s/hop.png" % CDN)
    assert web.internal_hits == [], web.internal_hits


# --------------------------------------------------------------------------------------------- link previews
# `/client/preview` (no login) reads a page's <title>/OpenGraph tags and hands them back -- an internal page's title
# is exactly what an SSRF wants. It checked every hop with its own resolver and then connected through a plain
# client (rebinding), and its own rule let CGNAT / Tailscale 100.64.0.0/10 through (not `is_private` in Python).
@pytest.mark.parametrize("flip_after", [1, 2, 3])
def test_a_link_preview_never_reads_this_nodes_network_through_a_rebinding_name(web, call, flip_after):
    web.dns["rebindp" + D] = lambda n: [FW.PUBLIC[1]] if n <= flip_after else ["127.0.0.1"]
    r = call("/client/preview", "http://rebindp%s:%d/secret" % (D, web.internal_port))
    assert FW.SECRET not in r.text, r.text[:200]
    assert web.internal_hits == [], ("a link preview reached this node's own network", web.internal_hits)


@pytest.mark.parametrize("addr", ["100.64.0.7", "100.100.100.100", "127.0.0.1", "169.254.169.254", "0.0.0.0", "::1"])
def test_a_link_preview_refuses_a_non_public_address(web, call, addr):
    web.dns["inside" + D] = [addr]
    r = call("/client/preview", "http://inside%s:%d/secret" % (D, web.internal_port))
    assert FW.SECRET not in r.text and web.internal_hits == [], (addr, web.internal_hits)


def test_a_link_preview_of_a_public_page_still_works(web, call):
    web.page(CDN, "/post", "<html><head><title>Ferry times</title><meta property='og:title' content='Ferry times'>"
                           "</head><body>x</body></html>")
    r = call("/client/preview", "http://%s/post" % CDN)
    assert r.status_code == 200 and "Ferry times" in r.text, (r.status_code, r.text[:200])


# --------------------------------------------------------------------------------------------- calendar subscriptions
# Calendar → Subscribe fetches a URL the user typed, every few hours, and shows what came back. It checked each hop's
# name (is_safe_host, which already refuses CGNAT) and then connected with a plain client.
@pytest.mark.parametrize("flip_after", [1, 2, 3])
def test_a_calendar_subscription_never_reaches_this_nodes_network_through_a_rebinding_name(web, flip_after):
    from app.services import caldav_subscribe
    web.dns["rebindc" + D] = lambda n: [FW.PUBLIC[1]] if n <= flip_after else ["127.0.0.1"]
    try:
        asyncio.run(caldav_subscribe.fetch_ics("http://rebindc%s:%d/cal.ics" % (D, web.internal_port)))
    except Exception:
        pass
    assert web.internal_hits == [], ("a calendar subscription reached this node's own network", web.internal_hits)


def test_a_public_calendar_still_subscribes(web):
    from app.services import caldav_subscribe
    web.page(CDN, "/cal.ics", "BEGIN:VCALENDAR\r\nVERSION:2.0\r\nEND:VCALENDAR\r\n", ctype="text/calendar")
    text, etag, changed = asyncio.run(caldav_subscribe.fetch_ics("http://%s/cal.ics" % CDN))
    assert "BEGIN:VCALENDAR" in text and changed


# --------------------------------------------------------------------------------------------- feeds (RSS, torrent RSS)
# A feed URL is typed by a member and fetched by the node through `afallback_transport` -- the proxy (Tor) first, then
# DIRECT. Tor cannot reach a private address; the direct leg resolved the name again after the guard looked.
@pytest.mark.parametrize("proxy", ["none", "down"])
@pytest.mark.parametrize("flip_after", [1, 2, 3])
def test_a_feed_fetch_never_reaches_this_nodes_network_through_a_rebinding_name(web, monkeypatch, proxy, flip_after):
    from app.services import proxy_utils, rss_service
    # "down": a proxy is configured and refuses, so the request falls back to the direct leg.
    monkeypatch.setattr(proxy_utils, "get_outbound_proxy",
                        lambda: None if proxy == "none" else "http://%s:3128" % FW.DEAD_PUBLIC)
    web.dns["rebindf" + D] = lambda n: [FW.PUBLIC[1]] if n <= flip_after else ["127.0.0.1"]
    try:
        asyncio.run(rss_service._get_bytes("http://rebindf%s:%d/feed.xml" % (D, web.internal_port)))
    except Exception:
        pass
    assert web.internal_hits == [], ("a feed fetch reached this node's own network", proxy, web.internal_hits)


def test_a_public_feed_still_fetches(web, monkeypatch):
    from app.services import proxy_utils, rss_service
    monkeypatch.setattr(proxy_utils, "get_outbound_proxy", lambda: None)
    web.page(CDN, "/feed.xml", "<rss><channel><title>News</title></channel></rss>", ctype="application/rss+xml")
    assert b"<title>News</title>" in asyncio.run(rss_service._get_bytes("http://%s/feed.xml" % CDN))
