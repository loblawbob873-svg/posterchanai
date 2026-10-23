"""THE WEB READER FETCHES ARBITRARY PAGES FOR ANYONE SIGNED IN — TESTED AT THE SERVER BOUNDARY, WITH THE REAL GUARD.

Web Search's reader (`GET /api/websearch/read`, the text the shipped client renders), the framed page view
(`/api/websearch/page`, still served to older clients) and its subresource proxy (`/api/websearch/asset`) take a
URL from the caller -- in practice a search RESULT, i.e. a URL an attacker can influence -- and fetch it FROM THIS
NODE. So the SSRF guard is a security property, and here it is the shipped one: `search_service.is_safe_url`, the
hand-rolled redirect loops, the real httpx client. Only DNS and the TCP dial are replaced (tests/reader_fake_web.py),
so "refused" is MEASURED: the stand-in for this node's own network records every request that reaches it, and its
secret must never come back in a response.

Also here: a normal article reads as text (and, framed, keeps its picture and stylesheet through the asset proxy); a
page that never ends and a page that trickles forever are both BOUNDED; a 404, a 503 and a timeout are said plainly.

Each request goes through the REAL router over ASGI (FastAPI + httpx.ASGITransport); only the session is a fixture.
"""
import asyncio
import time
from types import SimpleNamespace
from urllib.parse import quote

import httpx
import pytest
from fastapi import FastAPI

from app.routers import websearch as W
from app.services import page_render, search_service as S
from tests import reader_fake_web as FW

D = FW.DOMAIN
NEWS, MIRROR, HOP = "news" + D, "mirror" + D, "hop" + D

ARTICLE = """<!doctype html><html><head><title>Ferry timetable changes</title>
<link rel="stylesheet" href="/site.css"></head><body><nav>Home | About</nav><main>
<h1>Ferry timetable changes</h1>
<p>The first ferry of the day now leaves the harbour at 06:10, twenty minutes earlier than last summer.</p>
<img src="/harbour.png" alt="the harbour at dawn">
<p>The last sailing back from the island is at 23:40 on weekdays and 00:15 on Fridays and Saturdays.</p>
<p>Tickets bought on the old timetable remain valid for any sailing until the end of the month.</p>
<p>Paragraph 1 of the notice explains, in ordinary words, which crossing number 1 changes and why the operator moved it.</p>\n<p>Paragraph 2 of the notice explains, in ordinary words, which crossing number 2 changes and why the operator moved it.</p>\n<p>Paragraph 3 of the notice explains, in ordinary words, which crossing number 3 changes and why the operator moved it.</p>\n<p>Paragraph 4 of the notice explains, in ordinary words, which crossing number 4 changes and why the operator moved it.</p>\n<p>Paragraph 5 of the notice explains, in ordinary words, which crossing number 5 changes and why the operator moved it.</p>\n<p>Paragraph 6 of the notice explains, in ordinary words, which crossing number 6 changes and why the operator moved it.</p>\n<p>Paragraph 7 of the notice explains, in ordinary words, which crossing number 7 changes and why the operator moved it.</p>\n<p>Paragraph 8 of the notice explains, in ordinary words, which crossing number 8 changes and why the operator moved it.</p>\n<p>Paragraph 9 of the notice explains, in ordinary words, which crossing number 9 changes and why the operator moved it.</p>\n<p>Paragraph 10 of the notice explains, in ordinary words, which crossing number 10 changes and why the operator moved it.</p>\n<p>Paragraph 11 of the notice explains, in ordinary words, which crossing number 11 changes and why the operator moved it.</p>\n<p>Paragraph 12 of the notice explains, in ordinary words, which crossing number 12 changes and why the operator moved it.</p>\n<p>Paragraph 13 of the notice explains, in ordinary words, which crossing number 13 changes and why the operator moved it.</p>\n<p>Paragraph 14 of the notice explains, in ordinary words, which crossing number 14 changes and why the operator moved it.</p>\n<p>Paragraph 15 of the notice explains, in ordinary words, which crossing number 15 changes and why the operator moved it.</p>\n<p>Paragraph 16 of the notice explains, in ordinary words, which crossing number 16 changes and why the operator moved it.</p>\n<p>Paragraph 17 of the notice explains, in ordinary words, which crossing number 17 changes and why the operator moved it.</p>\n<p>Paragraph 18 of the notice explains, in ordinary words, which crossing number 18 changes and why the operator moved it.</p>\n<p>Paragraph 19 of the notice explains, in ordinary words, which crossing number 19 changes and why the operator moved it.</p>\n<p>Paragraph 20 of the notice explains, in ordinary words, which crossing number 20 changes and why the operator moved it.</p>\n<p>Paragraph 21 of the notice explains, in ordinary words, which crossing number 21 changes and why the operator moved it.</p>\n<p>Paragraph 22 of the notice explains, in ordinary words, which crossing number 22 changes and why the operator moved it.</p>\n<p>Paragraph 23 of the notice explains, in ordinary words, which crossing number 23 changes and why the operator moved it.</p>\n<p>Paragraph 24 of the notice explains, in ordinary words, which crossing number 24 changes and why the operator moved it.</p>\n</main></body></html>"""


@pytest.fixture
def web(monkeypatch):
    w = FW.FakeWeb()
    w.install(monkeypatch)
    for name in (NEWS, MIRROR, HOP):
        w.dns[name] = [FW.PUBLIC[0]]
    w.page(NEWS, "/ferries", ARTICLE)
    w.page(NEWS, "/site.css", "body{background:url(/bg.png)}", ctype="text/css")
    w.page(NEWS, "/harbour.png", FW.PNG, ctype="image/png")
    # Hermetic: no exemptions from settings, no configured SearXNG, and never a headless-browser render.
    monkeypatch.setattr(S, "own_media_hosts", lambda: set())
    monkeypatch.setattr(S, "_configured_url", lambda: "")
    renders = []
    monkeypatch.setattr(page_render, "render_page_text", lambda url, *a, **k: renders.append(url) or None)
    w.renders = renders
    yield w
    w.close()


@pytest.fixture
def node(web):
    """The real websearch router, signed in as an ordinary member."""
    app = FastAPI()
    app.include_router(W.router)
    me = SimpleNamespace(id=4242, username="reader", nostr_npub="ab" * 32, is_admin=False, can_ai=False)
    app.dependency_overrides[W.get_search_member] = lambda: me
    app.dependency_overrides[W._member_page_viewer] = lambda: me
    from app.database import get_db
    app.dependency_overrides[get_db] = lambda: None

    def call(path, **params):
        async def go():
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app),
                                         base_url="http://node.lan", timeout=120) as c:
                return await c.get(path, params=params)
        return asyncio.run(go())
    return call


def internal(web, path="/secret"):
    return "http://127.0.0.1:%d%s" % (web.internal_port, path)


def assert_refused(web, r, what):
    body = r.text
    assert FW.SECRET not in body, (what, "the node's own network answered and its body came back", body[:300])
    assert web.internal_hits == [], (what, "a request reached this node's own network", web.internal_hits)
    assert "blocked" in body.lower() or "refused" in body.lower() or "not a public address" in body.lower(), \
        (what, "a refusal must say so", r.status_code, body[:300])


# ---------------------------------------------------------------------------------------------- a normal article
def test_an_article_reads_as_its_text(web, node):
    r = node("/api/websearch/read", url="http://%s/ferries" % NEWS)
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["error"] is None, out
    assert out["title"] == "Ferry timetable changes"
    assert "06:10" in out["content"] and "23:40" in out["content"], out["content"]
    assert "Home | About" not in out["content"], "navigation chrome is not the article"
    assert web.renders == [], "a readable page must not spend a headless-browser render"


def test_the_framed_page_keeps_its_picture_and_stylesheet(web, node):
    r = node("/api/websearch/page", url="http://%s/ferries" % NEWS)
    assert r.status_code == 200
    assert "default-src 'none'" in r.headers["content-security-policy"]
    html = r.text
    assert "06:10" in html
    img = "/api/websearch/asset?url=" + quote("http://%s/harbour.png" % NEWS, safe="")
    css = "/api/websearch/asset?url=" + quote("http://%s/site.css" % NEWS, safe="")
    assert img in html and css in html, html[:1500]
    # …and those subresource links actually serve the bytes, through the same guard.
    got = node("/api/websearch/asset", url="http://%s/harbour.png" % NEWS)
    assert got.status_code == 200 and got.content == FW.PNG and got.headers["content-type"] == "image/png"
    sheet = node("/api/websearch/asset", url="http://%s/site.css" % NEWS)
    assert sheet.status_code == 200 and "/api/websearch/asset?url=" + quote("http://%s/bg.png" % NEWS, safe="") in sheet.text


# ---------------------------------------------------------------------------------------------- the SSRF guard
# Every address that is "this node's network": loopback, RFC1918, link-local + the cloud metadata endpoint, IPv6
# loopback, the unspecified address (Linux dials it as loopback), CGNAT (Tailscale's 100.64/10), and the spellings
# inet_aton accepts for 127.0.0.1.
PRIVATE_TARGETS = {
    "loopback": "http://127.0.0.1:{p}/secret",
    "rfc1918-10": "http://10.0.0.5:{p}/admin",
    "rfc1918-192": "http://192.168.0.1:{p}/",
    "rfc1918-172": "http://172.16.4.4:{p}/",
    "metadata": "http://169.254.169.254/latest/meta-data/iam/security-credentials/",
    "ipv6-loopback": "http://[::1]:{p}/secret",
    "ipv4-mapped-ipv6": "http://[::ffff:127.0.0.1]:{p}/secret",
    "unspecified": "http://0.0.0.0:{p}/secret",
    "cgnat-tailscale": "http://100.101.102.103:{p}/",
    "decimal": "http://2130706433:{p}/secret",
    "short-form": "http://127.1:{p}/secret",
    "hex": "http://0x7f000001:{p}/secret",
    "userinfo": "http://news.reader-web.test@127.0.0.1:{p}/secret",
    "name-to-loopback": "http://loopy" + D + ":{p}/secret",
}
ENDPOINTS = ("/api/websearch/read", "/api/websearch/page", "/api/websearch/asset")


def _prep(web):
    web.dns["loopy" + D] = ["127.0.0.1"]
    return {k: v.format(p=web.internal_port) for k, v in PRIVATE_TARGETS.items()}


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("target", sorted(PRIVATE_TARGETS))
def test_a_private_address_asked_for_directly_is_refused(web, node, endpoint, target):
    url = _prep(web)[target]
    r = node(endpoint, url=url)
    assert_refused(web, r, (endpoint, target))


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("target", sorted(PRIVATE_TARGETS))
def test_a_redirect_to_a_private_address_is_refused(web, node, endpoint, target):
    url = _prep(web)[target]
    web.redirect(NEWS, "/go", url)
    r = node(endpoint, url="http://%s/go" % NEWS)
    assert_refused(web, r, (endpoint, target))
    assert (NEWS, "/go") in web.public_hits, "the public first hop must have been fetched (premise)"


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("status", (301, 302, 303, 307, 308))
def test_every_redirect_status_is_checked_through_a_chain(web, node, endpoint, status):
    """public → another public name → a relative hop → loopback: only the LAST hop is private."""
    web.redirect(NEWS, "/a", "http://%s/b" % MIRROR, status)
    web.redirect(MIRROR, "/b", "/c", status)
    web.redirect(MIRROR, "/c", internal(web), status)
    r = node(endpoint, url="http://%s/a" % NEWS)
    assert_refused(web, r, (endpoint, status))
    assert (MIRROR, "/c") in web.public_hits


@pytest.mark.parametrize("endpoint", ENDPOINTS)
@pytest.mark.parametrize("scheme", ("file:///etc/passwd", "gopher://127.0.0.1:70/", "ftp://127.0.0.1/",
                                    "dict://127.0.0.1:11211/stat"))
def test_a_redirect_to_another_scheme_is_refused(web, node, endpoint, scheme):
    web.redirect(NEWS, "/x", scheme)
    r = node(endpoint, url="http://%s/x" % NEWS)
    assert "root:" not in r.text
    assert web.internal_hits == []
    if endpoint == "/api/websearch/read":
        assert r.json()["error"], r.text


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_a_name_that_answers_public_then_loopback_cannot_reach_this_node(web, node, endpoint):
    """DNS rebinding: the guard's lookup sees a public address, the connect's lookup a moment later sees loopback."""
    web.dns["rebind" + D] = lambda n: [FW.PUBLIC[1]] if n <= 1 else ["127.0.0.1"]
    r = node(endpoint, url="http://rebind%s:%d/secret" % (D, web.internal_port))
    assert FW.SECRET not in r.text, r.text[:300]
    assert web.internal_hits == [], web.internal_hits


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_a_name_with_a_dead_public_answer_and_a_loopback_one_is_refused(web, node, endpoint):
    """Two A records, the public one refusing connections: a guard that judged only the first answer passes, and
    the client's own fallback then dials the second."""
    web.dns["twoface" + D] = [FW.DEAD_PUBLIC, "127.0.0.1"]
    r = node(endpoint, url="http://twoface%s:%d/secret" % (D, web.internal_port))
    assert FW.SECRET not in r.text, r.text[:300]
    assert web.internal_hits == [], web.internal_hits


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_a_name_with_a_public_ipv4_and_a_loopback_ipv6_is_refused(web, node, endpoint):
    """gethostbyname() only ever sees IPv4; the connection prefers IPv6."""
    web.dns["dual" + D] = [FW.PUBLIC[2], "::1"]
    r = node(endpoint, url="http://dual%s:%d/secret" % (D, web.internal_port))
    assert FW.SECRET not in r.text, r.text[:300]
    assert web.internal_hits == [], web.internal_hits


def test_the_guard_refuses_every_answer_not_only_the_first():
    """The same three shapes at the guard itself (no fetch), so a regression names the function."""
    mp = pytest.MonkeyPatch()
    w = FW.FakeWeb()
    try:
        w.install(mp)
        mp.setattr(S, "own_media_hosts", lambda: set())
        w.dns["twoface" + D] = [FW.PUBLIC[0], "127.0.0.1"]
        w.dns["dual" + D] = [FW.PUBLIC[0], "::1"]
        w.dns["cgnat" + D] = ["100.64.1.1"]
        w.dns["fine" + D] = [FW.PUBLIC[0]]
        for name in ("twoface", "dual", "cgnat"):
            ok, why = S.is_safe_url("http://%s%s/" % (name, D))
            assert not ok, (name, "passed the guard")
        assert S.is_safe_url("http://fine%s/" % D) == (True, "")
    finally:
        mp.undo()
        w.close()


# ---------------------------------------------------------------------------------------------- huge and slow
def _timed(fn, limit):
    t = time.monotonic()
    out = fn()
    took = time.monotonic() - t
    assert took < limit, "took %.1fs (limit %ss)" % (took, limit)
    return out, took


def _run_bounded(fn, limit):
    """Run `fn` in a thread so an unbounded fetch FAILS the test instead of hanging the suite."""
    import concurrent.futures as cf
    ex = cf.ThreadPoolExecutor(1)
    fut = ex.submit(fn)
    try:
        return fut.result(limit)
    except cf.TimeoutError:
        raise AssertionError("still fetching after %ss -- the reader is unbounded" % limit)
    finally:
        ex.shutdown(wait=False)


@pytest.mark.parametrize("endpoint", ("/api/websearch/read", "/api/websearch/page"))
def test_a_page_that_never_ends_is_cut_off(web, node, endpoint):
    web.huge(NEWS, "/endless")
    r = _run_bounded(lambda: node(endpoint, url="http://%s/endless" % NEWS), 30)
    assert r.status_code == 200
    # It stopped reading: what the server managed to send is bounded by the cap plus what the socket buffers held.
    time.sleep(0.3)
    sent = web.sent.get("/endless", 0)
    assert sent < 16_000_000, "kept reading %d bytes" % sent
    if endpoint.endswith("/read"):
        out = r.json()
        assert "All work and no play" in out["content"] and len(out["content"]) <= 40100, len(out["content"])


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_a_page_that_trickles_forever_gives_up_and_says_so(web, node, endpoint, monkeypatch):
    """One byte every half second never trips a read timeout; only a whole-fetch deadline ends it."""
    monkeypatch.setattr(S, "FETCH_DEADLINE", 3.0, raising=False)
    # The asset proxy serves stylesheets, pictures and fonts only (an HTML body is a 204), so it gets a slow picture.
    web.slow(NEWS, "/drip", every=0.3, ctype="image/png" if endpoint.endswith("/asset") else "text/html; charset=utf-8")
    r = _run_bounded(lambda: node(endpoint, url="http://%s/drip" % NEWS), 20)
    text = r.json()["error"] if endpoint.endswith("/read") else r.text
    assert "too long" in (text or "").lower(), (r.status_code, (text or "")[:300])


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_a_site_that_never_answers_is_a_timeout_said_plainly(web, node, endpoint, monkeypatch):
    monkeypatch.setattr(S, "FETCH_DEADLINE", 3.0, raising=False)
    web.silent(NEWS, "/hang", seconds=30)
    r = _run_bounded(lambda: node(endpoint, url="http://%s/hang" % NEWS), 20)
    text = r.json()["error"] if endpoint.endswith("/read") else r.text
    assert "too long" in (text or "").lower(), (r.status_code, (text or "")[:300])


# ---------------------------------------------------------------------------------------------- errors, plainly
@pytest.mark.parametrize("status,words", ((404, "not found"), (503, "unavailable"), (500, "server error"),
                                          (403, "refused")))
def test_an_error_status_is_said_plainly_by_the_reader(web, node, status, words):
    web.page(NEWS, "/e", "<html><body>%d</body></html>" % status, status=status)
    out = node("/api/websearch/read", url="http://%s/e" % NEWS).json()
    assert out["content"] == ""
    assert words in (out["error"] or "").lower() and str(status) in out["error"], out


@pytest.mark.parametrize("status,words", ((404, "not found"), (503, "unavailable"), (500, "server error"),
                                          (403, "refused")))
def test_an_error_status_is_said_plainly_by_the_page_view(web, node, status, words):
    # A REAL error page: the site's own chrome, a search box, a few paragraphs -- plenty to "render", none of it the
    # article. An empty one and a full one must both be said as the error they are.
    chrome = "".join("<p>Browse our sections: travel, harbour news, timetables, item %d of the site menu.</p>" % i
                     for i in range(12))
    web.page(NEWS, "/e", "<html><body><h1>Oops!</h1>%s</body></html>" % chrome, status=status)
    web.page(NEWS, "/e2", "<html><body>%d</body></html>" % status, status=status)
    r2 = node("/api/websearch/page", url="http://%s/e2" % NEWS)
    assert words in r2.text.lower() and str(status) in r2.text, r2.text[:600]
    r = node("/api/websearch/page", url="http://%s/e" % NEWS)
    assert words in r.text.lower() and str(status) in r.text, r.text[:600]
    assert "built by javascript" not in r.text.lower(), "an error page is not a script-built page"


def test_a_name_that_does_not_resolve_is_said_plainly(web, node):
    out = node("/api/websearch/read", url="http://nowhere%s/" % D).json()
    assert out["content"] == "" and out["error"], out
    r = node("/api/websearch/page", url="http://nowhere%s/" % D)
    assert "can't be shown here" in r.text and "resolve" in r.text.lower(), r.text[:400]


# ---------------------------------------------------------------------------------------------- the page view's markup
from tests.client.test_web_reader_full_app import PAYLOADS, XSS_PAGE  # noqa: E402  (one list, both tests)


def _assert_inert(r):
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(r.text, "lxml")
    assert not soup.find_all(["script", "iframe", "object", "embed", "frame", "form", "button", "input", "template",
                              "noscript"]), "an executing or submitting element survived"
    for el in soup.find_all(True):
        for a, v in el.attrs.items():
            v = " ".join(v) if isinstance(v, list) else v
            assert not a.lower().startswith("on"), (el.name, a)
            assert "javascript:" not in v.lower().replace("\t", "") and "vbscript:" not in v.lower(), (el.name, a, v)
            assert not v.lower().startswith("data:text/html"), (el.name, a, v)
    assert not soup.find("meta", attrs={"http-equiv": True}), "a meta refresh survived"
    return soup


@pytest.mark.parametrize("i", range(len(PAYLOADS)), ids=lambda i: PAYLOADS[i][:28])
def test_each_payload_is_stripped_and_the_article_kept(web, node, i):
    """One payload at a time, inside an ordinary article: nothing that can run survives in ANY attribute, and the
    article AFTER the payload is still there (a payload that swallows the rest of the page is a broken page)."""
    page = ("<!doctype html><html><head><title>Notice</title></head><body><main><h1>Notice</h1>%s%s"
            "<p>AFTER-THE-PAYLOAD the harbour opens at dawn.</p></main></body></html>"
            % (PAYLOADS[i], "".join("<p>Paragraph %d of the notice, in ordinary words.</p>" % k for k in range(12))))
    web.page(NEWS, "/p", page)
    r = node("/api/websearch/page", url="http://%s/p" % NEWS)
    assert r.status_code == 200
    soup = _assert_inert(r)
    assert "AFTER-THE-PAYLOAD" in soup.get_text(" ", strip=True), "the article after the payload was thrown away"
    assert "script-src" not in r.headers["content-security-policy"]


def test_the_whole_hostile_page_is_inert_and_readable(web, node):
    web.page(NEWS, "/xss", XSS_PAGE)
    soup = _assert_inert(node("/api/websearch/page", url="http://%s/xss" % NEWS))
    text = soup.get_text(" ", strip=True)
    assert "The harbour opens at dawn" in text and "Paragraph 15 of the notice" in text


@pytest.mark.parametrize("endpoint", ENDPOINTS)
def test_the_redirect_check_holds_on_its_own(web, node, endpoint, monkeypatch):
    """Two layers refuse a redirect into this node's network: the guard on every hop, and the pinned transport at
    connect time. Each must hold alone -- here the transport is an ordinary one, so only the hop check stands."""
    monkeypatch.setattr(S, "fetch_transport", lambda: httpx.AsyncHTTPTransport(), raising=False)
    web.redirect(NEWS, "/go", internal(web))
    assert_refused(web, node(endpoint, url="http://%s/go" % NEWS), endpoint)
