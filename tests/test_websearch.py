"""Web Search — the SearXNG front end, the reader, and the two LLM summaries.

Run: venv-unified/bin/python -m unittest tests.test_websearch

No network, no LLM, no database: SearXNG is a stubbed httpx response and the inference service is a
stub, because what these cover is the wiring that fails SILENTLY:

- WHERE a node searches. The resolution order (Admin setting → the SearXNG bundled with this node →
  a public instance) is one function, and every consumer — the AI's web-search tool, the news
  digests, the bots, this screen — has to come out of it with the SAME answer. The old default was a
  hardcoded `search.poster.place`, so every node that never filled the field in searched through one
  deployment's box and nothing said so.
- HOW it gets there. A remote instance goes through the Tor proxy (falling back to direct); a local
  one must NOT, because the proxy is Tor-only and loopback through Tor cannot work.
- That a failed search is DISTINGUISHABLE from a search with no results. `{"results": []}` with no
  error reads as "nothing matched your query", which is the wrong thing to tell someone whose node
  has no SearXNG at all.
- That the AI overview cites the results it was actually given, and refuses rather than inventing
  when there are none.
- That the LLM paths are gated on `can_ai` exactly like chat, and that a repeat click is served from
  the cache instead of re-running inference on a shared GPU.
"""
import asyncio
import unittest
from unittest import mock

import httpx
from fastapi import HTTPException

from app.routers import websearch as W
from app.services import search_service as S


def run(coro):
    return asyncio.run(coro)


class _User:
    def __init__(self, is_admin=False, can_ai=False):
        self.is_admin = is_admin
        self.can_ai = can_ai


def _searx_payload(n=3, answers=None, suggestions=None):
    return {
        "results": [
            {"title": f"Result {i}", "url": f"https://example.com/{i}",
             "content": f"snippet {i}", "engine": "google",
             "thumbnail_src": f"https://searx.example/img/{i}.jpg",
             "img_src": f"https://cdn.example/{i}.jpg",
             "publishedDate": "2026-01-02T00:00:00"}
            for i in range(1, n + 1)
        ],
        "answers": answers if answers is not None else [],
        "suggestions": suggestions or [],
    }


class _StubResponse:
    def __init__(self, data, status=200, headers=None):
        self._data = data
        self.status_code = status
        self.headers = headers or {}

    def raise_for_status(self):
        if self.status_code >= 400:
            raise httpx.HTTPStatusError("boom", request=None, response=None)

    def json(self):
        return self._data


class ResolveTests(unittest.TestCase):
    """Where a node searches, and how it gets there."""

    def setUp(self):
        S._local_probe.update({"ts": 0.0, "url": ""})

    def test_configured_setting_wins(self):
        with mock.patch.object(S, "search_enabled", return_value=True), \
             mock.patch.object(S.settings_store, "get", return_value="https://searx.mine/"), \
             mock.patch.object(S, "local_searxng_url", return_value="http://127.0.0.1:8899"):
            # …and the trailing slash is stripped, or every call would request `//search`.
            self.assertEqual(S.resolve_searxng_url(), "https://searx.mine")

    def test_bundled_instance_beats_the_public_fallback(self):
        with mock.patch.object(S, "search_enabled", return_value=True), \
             mock.patch.object(S.settings_store, "get", return_value=""), \
             mock.patch.object(S, "local_searxng_url", return_value="http://127.0.0.1:8899"):
            self.assertEqual(S.resolve_searxng_url(), "http://127.0.0.1:8899")

    def test_public_fallback_is_last(self):
        with mock.patch.object(S, "search_enabled", return_value=True), \
             mock.patch.object(S.settings_store, "get", return_value=""), \
             mock.patch.object(S, "local_searxng_url", return_value=""):
            self.assertEqual(S.resolve_searxng_url(), S.DEFAULT_SEARXNG_URL)

    def test_search_can_be_turned_off(self):
        """Clearing the URL no longer means "don't search" — it falls through to a public instance —
        so there has to be a switch that actually stops this node making search requests."""
        with mock.patch.object(S.settings_store, "get",
                               side_effect=lambda k, d=None: "false" if k == "searxng_enabled" else ""), \
             mock.patch.object(S, "local_searxng_url", return_value="http://127.0.0.1:8899"):
            self.assertEqual(S.resolve_searxng_url(), "")

    def test_a_blank_switch_means_on(self):
        """settings_store.get_bool reads "" as FALSE, and a blank row is what a legacy-table
        migration leaves behind — which would turn search off across a node with nothing said."""
        for stored in (None, "", "  "):
            with mock.patch.object(S.settings_store, "get", side_effect=lambda k, d=None, _v=stored: _v if k == "searxng_enabled" else ""):
                self.assertTrue(S.search_enabled(), f"stored {stored!r} must not disable search")

    def test_off_switch_reaches_a_search(self):
        svc = S.SearchService.__new__(S.SearchService)
        svc.searxng_url = "https://searx.example.com"      # …even with an instance configured
        with mock.patch.object(S, "search_enabled", return_value=False):
            self.assertEqual(run(svc.base()), "")
            self.assertEqual(run(svc.web_search("cats")), [])
            out = run(svc.search_page("cats"))
        self.assertEqual(out["results"], [])
        self.assertIn("turned off", out["error"])

    def test_local_probe_is_cached(self):
        """The probe runs on the search path; re-probing per search would put a connect attempt in
        front of every query the app makes."""
        calls = []

        def _get(url, **kw):
            calls.append(url)
            return _StubResponse({}, status=200, headers={"content-type": "application/json"})

        with mock.patch.object(S.httpx, "get", side_effect=_get):
            first = S.local_searxng_url()
            second = S.local_searxng_url()
        self.assertEqual(first, second)
        self.assertEqual(len(calls), 2, "one /healthz + one /config, then the cache")

    def test_probe_rejects_something_that_is_not_searxng(self):
        """A 404 from an unrelated listener used to pass (`status < 500`), and the node then adopted
        it as its search backend for five minutes with the public fallback never tried."""
        def _get(url, **kw):
            if url.endswith("/healthz"):
                return _StubResponse({}, status=404)
            return _StubResponse({}, status=200, headers={"content-type": "application/json"})
        with mock.patch.object(S.httpx, "get", side_effect=_get):
            self.assertEqual(S.local_searxng_url(), "")

    def test_probe_rejects_a_health_endpoint_without_a_searxng_behind_it(self):
        def _get(url, **kw):
            if url.endswith("/healthz"):
                return _StubResponse({}, status=200)
            return _StubResponse({}, status=200, headers={"content-type": "text/html"})
        with mock.patch.object(S.httpx, "get", side_effect=_get):
            self.assertEqual(S.local_searxng_url(), "")

    def test_probe_port_comes_from_the_file_the_installer_wrote(self):
        """An env var set at INSTALL time never reaches the app's systemd service."""
        with mock.patch.dict(S.os.environ, {}, clear=False):
            S.os.environ.pop("POSTERCHANAI_SEARXNG_PORT", None)
            with mock.patch("builtins.open", mock.mock_open(read_data="9000\n")):
                self.assertEqual(S._local_port(), "9000")
                self.assertIn("http://127.0.0.1:9000", S.local_searxng_urls())

    def test_remote_searches_go_through_the_proxy_and_local_does_not(self):
        sentinel = object()
        with mock.patch("app.services.proxy_utils.afallback_transport", return_value=sentinel):
            self.assertIs(S.search_transport("https://searx.example.com"), sentinel,
                          "a remote instance must go through the Tor proxy (with direct fallback)")
            for local in ("http://127.0.0.1:8899", "http://localhost:8899", "http://searxng:8080"):
                self.assertIsNot(S.search_transport(local), sentinel,
                                 f"{local} is on this machine — Tor cannot carry loopback traffic")

    def test_a_lan_instance_is_never_sent_through_tor(self):
        """Tor cannot route RFC1918. The proxy answers an unroutable target with a 502 RESPONSE, and
        afallback_transport only falls back on connect-level errors — so a self-hosted LAN SearXNG
        routed through the proxy fails every single request, reported to the user as "no results".
        This deployment's own instance is exactly that shape (http://192.168.0.85:8888)."""
        sentinel = object()
        with mock.patch("app.services.proxy_utils.afallback_transport", return_value=sentinel):
            for lan in ("http://192.168.0.85:8888", "http://10.1.2.3:8080", "http://172.16.5.5:8080",
                        "http://nas.lan:8888", "http://box.local:8080"):
                self.assertIsNot(S.search_transport(lan), sentinel, f"{lan} must be reached directly")

    def test_a_name_that_resolves_to_a_private_ip_is_local_too(self):
        """Split-horizon DNS is normal here — our own public names answer with a LAN address from
        inside the LAN."""
        with mock.patch.object(S.socket, "gethostbyname", return_value="192.168.0.1"):
            self.assertTrue(S._is_local_base("https://media.poster.place"))
        with mock.patch.object(S.socket, "gethostbyname", return_value="93.184.216.34"):
            self.assertFalse(S._is_local_base("https://searx.example.com"))


class SearchPageTests(unittest.TestCase):
    def setUp(self):
        self.svc = S.SearchService.__new__(S.SearchService)     # no DB / settings load
        self.svc.searxng_url = "https://searx.example.com"

    def _run_search(self, payload, **kw):
        with mock.patch.object(S.httpx, "AsyncClient") as client:
            inst = client.return_value.__aenter__.return_value
            async def _get(url, params=None):
                self.captured = (url, params or {})
                return _StubResponse(payload)
            inst.get = _get
            return run(self.svc.search_page("cats", **kw))

    def test_shapes_results_and_keeps_pagination(self):
        out = self._run_search(_searx_payload(2), page=3)
        self.assertIsNone(out["error"])
        self.assertEqual(len(out["results"]), 2)
        self.assertEqual(out["results"][0]["url"], "https://example.com/1")
        self.assertEqual(self.captured[1]["pageno"], "3")

    def test_unknown_category_and_time_range_are_not_forwarded(self):
        """`categories` reaches a third-party instance verbatim — the client does not get to choose
        what this node asks for."""
        out = self._run_search(_searx_payload(1), category="../etc/passwd", time_range="forever")
        self.assertEqual(self.captured[1]["categories"], "general")
        self.assertNotIn("time_range", self.captured[1])
        self.assertIsNone(out["error"])

    def test_full_image_src_only_for_the_images_tab(self):
        web = self._run_search(_searx_payload(1), category="general")
        img = self._run_search(_searx_payload(1), category="images")
        self.assertEqual(web["results"][0]["img_src"], "")
        self.assertEqual(img["results"][0]["img_src"], "https://cdn.example/1.jpg")

    def test_a_failed_search_is_not_an_empty_one(self):
        with mock.patch.object(S.httpx, "AsyncClient") as client:
            inst = client.return_value.__aenter__.return_value
            async def _boom(url, params=None):
                raise httpx.ConnectError("no route")
            inst.get = _boom
            out = run(self.svc.search_page("cats"))
        self.assertEqual(out["results"], [])
        self.assertTrue(out["error"], "an unreachable instance must not look like 'no results'")

    def test_no_instance_at_all_reports_it(self):
        self.svc.searxng_url = ""
        with mock.patch.object(S, "local_searxng_url", return_value=""), \
             mock.patch.object(S, "DEFAULT_SEARXNG_URL", ""):
            out = run(self.svc.search_page("cats"))
        self.assertTrue(out["error"])

    def test_answers_survive_both_searxng_shapes(self):
        out = self._run_search(_searx_payload(1, answers=[{"answer": "42"}, "plain"]))
        self.assertEqual(out["answers"], ["42", "plain"])


class RedirectSsrfTests(unittest.TestCase):
    """`is_safe_url` only ever saw the FIRST url. With httpx following redirects, the guard was
    advisory: one 302 reached the metadata service or a loopback admin endpoint and handed the body
    back. Web Search made that reachable with a URL the node did not choose (a search RESULT)."""

    def setUp(self):
        self.svc = S.SearchService.__new__(S.SearchService)
        self.svc.searxng_url = ""

    def _client(self, pages):
        """pages: {url: (status, headers, body)}"""
        class _Resp:
            def __init__(self, url, status, headers, body):
                self.url, self.status_code, self.headers, self.text = url, status, headers, body
            def raise_for_status(self):
                if self.status_code >= 400:
                    raise httpx.HTTPStatusError("boom", request=None, response=None)
        seen = []
        class _Client:
            async def __aenter__(self_inner): return self_inner
            async def __aexit__(self_inner, *a): return False
            async def get(self_inner, url, headers=None):
                seen.append(url)
                st, hd, body = pages[url]
                return _Resp(url, st, hd, body)
        return _Client, seen

    def test_a_redirect_to_a_private_address_is_refused(self):
        Client, seen = self._client({
            "https://attacker.example/r": (302, {"location": "http://169.254.169.254/latest/meta-data/"}, ""),
        })
        # The FIRST url passes the guard (it is an ordinary public page as far as the checker is
        # concerned) — the whole point is that the hop after it does not. Patched rather than left to
        # real DNS, or the test passes because the fixture hostname does not resolve.
        def _guard(u):
            return (False, "Private IP not allowed") if "169.254." in u else (True, "")
        with mock.patch.object(S.httpx, "AsyncClient", lambda *a, **kw: Client()), \
             mock.patch.object(S, "is_safe_url", side_effect=_guard):
            out = run(self.svc.fetch_url_content("https://attacker.example/r"))
        self.assertIn("blocked", (out or {}).get("error", ""))
        self.assertNotIn("http://169.254.169.254/latest/meta-data/", seen,
                         "the metadata address must never be requested")

    def test_an_ordinary_redirect_still_works(self):
        Client, seen = self._client({
            "https://site.example/a": (301, {"location": "/b"}, ""),
            "https://site.example/b": (200, {"content-type": "text/html"},
                                       "<html><body><main>" + ("Real article text. " * 40) + "</main></body></html>"),
        })
        with mock.patch.object(S.httpx, "AsyncClient", lambda *a, **kw: Client()), \
             mock.patch.object(S, "is_safe_url", return_value=(True, "")):
            out = run(self.svc.fetch_url_content("https://site.example/a"))
        self.assertIn("Real article text", out["content"])
        self.assertEqual(seen, ["https://site.example/a", "https://site.example/b"])

    def test_a_redirect_loop_terminates(self):
        Client, _ = self._client({"https://site.example/a": (302, {"location": "/a"}, "")})
        with mock.patch.object(S.httpx, "AsyncClient", lambda *a, **kw: Client()), \
             mock.patch.object(S, "is_safe_url", return_value=(True, "")):
            out = run(self.svc.fetch_url_content("https://site.example/a"))
        self.assertIn("redirect", out["error"])


class PageRenderTests(unittest.TestCase):
    """The page view: someone else's markup, served from OUR origin so it can be framed at all.

    Which means the sanitiser is load-bearing — anything that executes would be executing on
    poster.place — and so is the part that makes the page still LOOK like itself, because a stripped
    page that renders naked is the thing this endpoint exists to replace.
    """
    HTML = """<!doctype html><html><head>
      <link rel="stylesheet" href="/style.css">
      <script src="/app.js"></script>
      <base href="https://evil.example/">
    </head><body onload="steal()">
      <img src="/logo.png" data-src="/real.png" srcset="/a.png 1x, /b.png 2x">
      <a href="/page2">next</a>
      <a href="javascript:alert(1)">bad</a>
      <a href="mailto:x@y.z">mail</a>
      <form action="/post"><input name="q"><button>go</button></form>
      <iframe src="https://tracker.example/"></iframe>
      <p onclick="alert(1)">text</p>
    </body></html>"""

    def setUp(self):
        self.out = W._render_page(self.HTML, "https://site.example/dir/page")

    def test_nothing_executes(self):
        low = self.out.lower()
        for dead in ("<script", "<iframe", "<form", "onload=", "onclick=", "javascript:"):
            self.assertNotIn(dead, low, f"{dead} survived the sanitiser")

    def test_the_page_still_looks_like_itself(self):
        """Stylesheets and images are kept and absolutised — resolved against /api/websearch/page they
        would 404 and the 'real page' would render as unstyled text."""
        self.assertIn('href="https://site.example/style.css"', self.out)
        self.assertIn("https://site.example/real.png", self.out)     # data-src promoted (no JS to do it)
        self.assertIn("https://site.example/a.png 1x", self.out)     # srcset absolutised
        self.assertIn('<base href="https://site.example/dir/page"', self.out)

    def test_the_pages_own_base_is_replaced_not_kept(self):
        """A <base> the page brought with it would point every relative URL wherever it says."""
        self.assertNotIn("evil.example", self.out)

    def test_links_stay_in_the_app(self):
        self.assertIn("/api/websearch/page?url=https%3A%2F%2Fsite.example%2Fpage2", self.out)
        self.assertNotIn("mailto:", self.out)     # not ours to open from a frame

    def test_the_csp_allows_the_base_it_injects(self):
        """`base-uri 'none'` would make the browser ignore our own <base>, so every relative URL would
        resolve against the proxy endpoint — the page renders naked and nothing says why."""
        self.assertNotIn("base-uri", W._PAGE_CSP)
        self.assertIn("default-src 'none'", W._PAGE_CSP)     # …and still no scripts, ever
        self.assertNotIn("script-src", W._PAGE_CSP)

    def test_a_blocked_url_is_a_readable_page_not_a_stack_trace(self):
        svc = mock.Mock()
        async def _blocked(url, max_bytes=0):
            return {"url": url, "html": "", "content_type": "", "error": "URL blocked: Private IP not allowed"}
        svc.fetch_url_raw = _blocked
        with mock.patch.object(W, "get_search_service", return_value=svc):
            resp = run(W.render_page(url="http://10.0.0.1/", db=None, current_user=_User()))
        body = resp.body.decode()
        self.assertIn("can't be shown here", body)
        self.assertIn("Open the original", body)
        self.assertIn("default-src 'none'", resp.headers["content-security-policy"])


class KeyboardWiringTests(unittest.TestCase):
    """Web Search joins app.js's EXISTING card-cursor system rather than growing a second one.

    Source-level on purpose: the keys live in app.js and the buttons in websearch.js, so a rename on
    either side breaks them silently — the key simply does nothing, which nobody notices until they
    try it. (The same shape as tests/test_effect_command_coverage.py.)
    """
    import pathlib as _pl
    ROOT = _pl.Path(__file__).resolve().parents[1]

    def setUp(self):
        self.app_js = (self.ROOT / "static/js/client/app.js").read_text(encoding="utf-8")
        self.ws_js = (self.ROOT / "static/js/client/websearch.js").read_text(encoding="utf-8")

    def test_results_are_rows_the_cursor_can_reach(self):
        self.assertIn("'#feed .ws-card'", self.app_js,
                      "without this, j/k/Enter skip straight past every search result")

    def test_every_card_key_presses_a_button_that_exists(self):
        import re
        m = re.search(r"\['\.ws-card', \{([^}]*)\}\]", self.app_js)
        self.assertIsNotNone(m, "no .ws-card entry in _CARD_KEYS — S/N/U do nothing on a result")
        for key, sel in re.findall(r"(\w+):'\.([\w-]+)'", m.group(1)):
            self.assertIn(f'ws-{sel.split("ws-")[-1]}', self.ws_js,
                          f"key '{key}' presses .{sel}, which websearch.js does not render")

    def test_escape_closes_the_open_page(self):
        self.assertIn("Escape", self.ws_js)
        self.assertIn("closeReader", self.ws_js)


class _StubService:
    """Stands in for the inference service. Records what the model was shown."""
    def __init__(self, reply="A summary [1]."):
        self.reply = reply
        self.seen = []

    async def chat_completion(self, messages, **kw):
        self.seen.append(messages)
        return {"choices": [{"message": {"content": self.reply}}]}


class RouterTests(unittest.TestCase):
    def setUp(self):
        W._CACHE.clear()
        self.svc = _StubService()
        self.patches = [
            mock.patch.object(W, "get_inference_service", return_value=self.svc),
            mock.patch.object(W, "prepare_vram_for_llm", lambda db: None),
        ]
        for p in self.patches:
            p.start()
        self.addCleanup(lambda: [p.stop() for p in self.patches])

    def _search_service(self, results=None, page=None):
        svc = mock.Mock()
        found = page if page is not None else {"results": results or [], "answers": [], "suggestions": [], "error": None}
        async def _search_page(*a, **kw):
            return found
        async def _fetch(url, max_length=0):
            return {"url": url, "title": "T", "content": "body text " * 60, "error": None}
        svc.search_page = _search_page
        svc.fetch_url_content = _fetch
        return svc

    def test_ai_gate_matches_chat(self):
        for user, ok in ((_User(), False), (_User(can_ai=True), True), (_User(is_admin=True), True)):
            if ok:
                W._require_ai(user)
            else:
                with self.assertRaises(HTTPException) as e:
                    W._require_ai(user)
                self.assertEqual(e.exception.status_code, 403)

    def test_overview_cites_the_results_it_was_given(self):
        results = [{"title": f"R{i}", "url": f"https://e.com/{i}", "content": f"c{i}"} for i in (1, 2, 3)]
        with mock.patch.object(W, "get_search_service", return_value=self._search_service(results)):
            out = run(W.overview(W.OverviewReq(q="cats"), db=None, current_user=_User(can_ai=True)))
        self.assertEqual([s["n"] for s in out["sources"]], [1, 2, 3])
        self.assertEqual(out["sources"][0]["url"], "https://e.com/1")
        prompt = self.svc.seen[0][-1]["content"]
        for i in (1, 2, 3):
            self.assertIn(f"[{i}] R{i}", prompt, "each source must reach the model with its citation number")

    def test_overview_refuses_with_no_results(self):
        """Rather than asking the model to answer from nothing, which it will happily do."""
        with mock.patch.object(W, "get_search_service", return_value=self._search_service([])):
            with self.assertRaises(HTTPException) as e:
                run(W.overview(W.OverviewReq(q="cats"), db=None, current_user=_User(can_ai=True)))
        self.assertIn(e.exception.status_code, (404, 502))
        self.assertEqual(self.svc.seen, [], "no results means no inference at all")

    def test_overview_is_cached_per_query(self):
        results = [{"title": "R1", "url": "https://e.com/1", "content": "c"}]
        with mock.patch.object(W, "get_search_service", return_value=self._search_service(results)):
            run(W.overview(W.OverviewReq(q="cats"), db=None, current_user=_User(can_ai=True)))
            run(W.overview(W.OverviewReq(q="cats"), db=None, current_user=_User(can_ai=True)))
            run(W.overview(W.OverviewReq(q="dogs"), db=None, current_user=_User(can_ai=True)))
        self.assertEqual(len(self.svc.seen), 2, "the repeat click must not re-run inference")

    def test_summarize_needs_readable_text(self):
        svc = self._search_service()
        async def _thin(url, max_length=0):
            return {"url": url, "title": "T", "content": "hi", "error": None}
        svc.fetch_url_content = _thin
        with mock.patch.object(W, "get_search_service", return_value=svc):
            with self.assertRaises(HTTPException) as e:
                run(W.summarize_link(W.SummarizeReq(url="https://e.com/1"), db=None, current_user=_User(can_ai=True)))
        self.assertEqual(e.exception.status_code, 422)

    def test_reader_reports_a_blocked_url_instead_of_pretending(self):
        """fetch_url_content answers SSRF refusals in `error`; the reader must pass that through as a
        readable message next to the original link, not as an empty article."""
        svc = self._search_service()
        async def _blocked(url, max_length=0):
            return {"url": url, "title": url, "content": "", "error": "URL blocked: Private IP not allowed"}
        svc.fetch_url_content = _blocked
        with mock.patch.object(W, "get_search_service", return_value=svc):
            out = run(W.read_page(url="http://10.0.0.1/", db=None, current_user=_User()))
        self.assertIn("blocked", out["error"])
        self.assertEqual(out["content"], "")


class ProxyFallbackTests(unittest.TestCase):
    """The Tor→Tor→DIRECT listener the bundled SearXNG points at.

    The direct fallback is the whole reason it is a SEPARATE port: the main :8118 carries torrent
    traffic and must fail rather than connect directly. If `allow_direct` ever leaks onto the default,
    every torrent on the node quietly gets the real IP — which is the failure you cannot see from
    outside and cannot take back.
    """

    def _proxy(self, allow_direct):
        from app.services.http_proxy_service import HttpToSocksProxy
        p = HttpToSocksProxy(listen_port=0, socks_ports=["9999:dead"], allow_direct=allow_direct)
        async def _fail(*a, **kw):
            raise OSError("connection refused")
        p._socks_connect_one = _fail
        return p

    def test_default_is_tor_only(self):
        p = self._proxy(False)
        with self.assertRaises(Exception) as e:
            run(p._socks_connect("example.com", 443))
        self.assertIn("all Tor backends failed", str(e.exception))

    def test_fallback_listener_connects_direct_when_every_circuit_is_down(self):
        p = self._proxy(True)
        sentinel = ("reader", "writer")
        with mock.patch("asyncio.open_connection", new=mock.AsyncMock(return_value=sentinel)) as oc:
            got = run(p._socks_connect("example.com", 443))
        self.assertEqual(got, sentinel)
        oc.assert_awaited_once_with("example.com", 443)


class BotEnvTests(unittest.TestCase):
    def setUp(self):
        from app.services import bot_manager_service as B
        self.B = B
        B._searxng_env["url"] = ""

    def test_bots_get_the_resolved_instance(self):
        """A bot inherits SEARXNG_URL from the app. Copying the raw setting hands an empty string to
        any node relying on the default, and botframework/searxng.py then prints 'not configured' and
        returns nothing — the bot's web search silently stops while the app's still works."""
        B = self.B
        with mock.patch.object(B.settings_store, "get", return_value=""), \
             mock.patch.object(B.settings_store, "exists", return_value=False), \
             mock.patch("app.services.search_service.search_enabled", return_value=True), \
             mock.patch("app.services.search_service.local_searxng_url", return_value="http://127.0.0.1:8899"):
            env = B._load_global_env()
        self.assertEqual(env.get("SEARXNG_URL"), "http://127.0.0.1:8899")

    def test_a_probe_flap_does_not_respawn_every_bot(self):
        """SEARXNG_URL feeds NO_PROXY, and both are part of _spec_sig — so a value that changes with
        the 5-minute probe would terminate and restart every running bot, mid-stream, on a timer."""
        B = self.B
        probe = {"url": "http://127.0.0.1:8899"}
        with mock.patch.object(B.settings_store, "get", return_value=""), \
             mock.patch.object(B.settings_store, "exists", return_value=False), \
             mock.patch("app.services.search_service.search_enabled", return_value=True), \
             mock.patch("app.services.search_service.local_searxng_url", side_effect=lambda: probe["url"]):
            first = B._bot_searxng_url()
            probe["url"] = ""                     # the probe fails on the next tick
            second = B._bot_searxng_url()
        self.assertEqual(first, second, "a resolved bundled instance must be sticky for the process")

    def test_an_admin_edit_still_wins(self):
        B = self.B
        B._searxng_env["url"] = "http://127.0.0.1:8899"
        with mock.patch.object(B.settings_store, "get",
                               side_effect=lambda k, d=None: "https://searx.mine/" if k == "searxng_url" else None):
            self.assertEqual(B._bot_searxng_url(), "https://searx.mine")

    def test_the_off_switch_stops_the_bots_too(self):
        """It is checked FIRST: after the configured URL (or the sticky cache) it would stop the app
        searching while every bot carried on."""
        B = self.B
        B._searxng_env["url"] = "http://127.0.0.1:8899"
        with mock.patch.object(B.settings_store, "get",
                               side_effect=lambda k, d=None: "false" if k == "searxng_enabled" else "https://searx.mine"):
            self.assertEqual(B._bot_searxng_url(), "")

    def test_a_public_fallback_never_lands_in_no_proxy(self):
        """NO_PROXY exempts a host from the bot's Tor proxy. The public instance in there means every
        bot search leaves from the node's real IP."""
        B = self.B
        env = {"SEARXNG_URL": S.DEFAULT_SEARXNG_URL, "POSTERCHANAI_API_ENDPOINT": "http://127.0.0.1:3051"}
        with mock.patch("app.services.proxy_utils.get_proxy_config", return_value="http://127.0.0.1:8118"):
            built = B._build_env({"name": "b", "nsec": "x"}, env)
        no_proxy = built.get("NO_PROXY", "")
        self.assertNotIn("tiekoetter", no_proxy, "a public instance must stay ON the Tor proxy")
        self.assertIn("127.0.0.1", no_proxy)


if __name__ == "__main__":
    unittest.main()
