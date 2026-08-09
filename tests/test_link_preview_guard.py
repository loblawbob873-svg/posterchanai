"""`/client/preview` fetches on the reader's behalf — bounded, off the event loop, and re-checked.

This endpoint is what a timeline calls once per link card, and it is public. Three things about it
took the node down or could leak from it:

  blocking-dns   The SSRF guard resolves with socket.getaddrinfo, which BLOCKS, and the handler is
                 async on a single uvicorn worker. Measured: a feed hydrating its cards fired 813
                 previews inside ten seconds; `GET /client` then timed out at 20s three times
                 running while every other route answered in milliseconds, 45 scheduler jobs were
                 missed by up to 18s, and the uptime monitor called the site down.
  unbounded      Nothing capped how many of those ran at once — each is a connection, a TLS
                 handshake, up to 512KB read and a regex pass, all on the one worker.
  redirect-ssrf  follow_redirects=True validates only the FIRST url. The same shape let a 302 reach
                 169.254.169.254 in the web-search fetcher; here the body becomes a link card's
                 title and description.

Driven against the real handler with a mock transport, so the redirect walk and the gate are the
shipped ones.
"""
import asyncio
import json
import unittest

import httpx

import app.routers.client as C


def _run(coro):
    return asyncio.run(coro)


def _body(resp):
    return json.loads(bytes(resp.body).decode())


class _Fake:
    """Stands in for the module's shared client, backed by httpx's MockTransport."""

    def __init__(self, routes):
        self.routes = routes
        self.seen = []

    def _handler(self, request: httpx.Request) -> httpx.Response:
        self.seen.append(str(request.url))
        r = self.routes.get(str(request.url))
        if r is None:
            return httpx.Response(404)
        return r

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self._handler), follow_redirects=False)


HTML = ('<html><head><meta property="og:title" content="A Title">'
        '<meta property="og:description" content="A description"></head></html>')


class LinkPreviewGuard(unittest.TestCase):
    def setUp(self):
        C._preview_cache.clear()
        self._real_host = C._is_public_host
        # Every hostname is "public" unless it is the one standing in for a private address.
        C._is_public_host = staticmethod(lambda h: h != "internal.invalid")
        C._preview_gate = asyncio.Semaphore(C._PREVIEW_MAX)

    def tearDown(self):
        C._is_public_host = self._real_host
        C._preview_client = None

    def _with(self, fake):
        async def _http():
            return fake.client()
        C._preview_http = _http
        return fake

    def test_the_guard_runs_off_the_event_loop(self):
        """A blocking resolve in an async handler stops the whole node, not just this request — so
        it must not run on the loop's thread. Asserted by comparing thread ids, not by reading the
        code: `await _is_public_host_async(...)` looks identical either way at the call site."""
        import threading
        seen = {}

        def where(host):
            seen["guard"] = threading.get_ident()
            return True

        C._is_public_host = staticmethod(where)
        self._with(_Fake({"https://ok.example/a": httpx.Response(200, html=HTML)}))

        async def go():
            seen["loop"] = threading.get_ident()
            return await C.link_preview("https://ok.example/a")

        r = _run(go())
        self.assertEqual(_body(r)["title"], "A Title")
        self.assertIn("guard", seen, "the guard did not run at all")
        self.assertNotEqual(seen["guard"], seen["loop"],
                            "the DNS resolve ran on the event loop — it blocks every other request")

    def test_a_redirect_into_a_private_host_is_not_followed(self):
        fake = self._with(_Fake({
            "https://ok.example/start": httpx.Response(302, headers={"location": "http://internal.invalid/secret"}),
            "http://internal.invalid/secret": httpx.Response(200, html="<title>SECRETS</title>"),
        }))
        r = _run(C.link_preview("https://ok.example/start"))
        self.assertEqual(_body(r), {}, "nothing from behind the redirect may be returned")
        self.assertNotIn("http://internal.invalid/secret", fake.seen,
                         "the private host must never even be requested")

    def test_an_ordinary_redirect_is_followed_and_re_checked(self):
        fake = self._with(_Fake({
            "https://ok.example/start": httpx.Response(301, headers={"location": "https://ok2.example/end"}),
            "https://ok2.example/end": httpx.Response(200, html=HTML),
        }))
        r = _run(C.link_preview("https://ok.example/start"))
        self.assertEqual(_body(r)["title"], "A Title")

    def test_a_redirect_loop_gives_up(self):
        fake = self._with(_Fake({
            "https://ok.example/a": httpx.Response(302, headers={"location": "https://ok.example/b"}),
            "https://ok.example/b": httpx.Response(302, headers={"location": "https://ok.example/a"}),
        }))
        r = _run(C.link_preview("https://ok.example/a"))
        self.assertEqual(_body(r), {})
        self.assertLessEqual(len(fake.seen), C._PREVIEW_HOPS + 1, "the hop limit must bound the walk")

    def test_only_a_few_fetches_run_at_once(self):
        """The cap is the whole point: a browser is not the only thing that can call this."""
        peak = {"n": 0, "cur": 0}

        async def _http():
            def handler(request):
                return httpx.Response(200, html=HTML)

            class Tracked(httpx.AsyncClient):
                def stream(self, *a, **k):
                    outer = super().stream(*a, **k)

                    class Ctx:
                        async def __aenter__(s):
                            peak["cur"] += 1
                            peak["n"] = max(peak["n"], peak["cur"])
                            await asyncio.sleep(0.02)
                            return await outer.__aenter__()

                        async def __aexit__(s, *e):
                            peak["cur"] -= 1
                            return await outer.__aexit__(*e)
                    return Ctx()
            return Tracked(transport=httpx.MockTransport(handler), follow_redirects=False)

        C._preview_http = _http

        async def go():
            await asyncio.gather(*[C.link_preview(f"https://ok.example/{i}") for i in range(30)])
        _run(go())
        self.assertLessEqual(peak["n"], C._PREVIEW_MAX,
                             f"{peak['n']} fetches ran at once against a cap of {C._PREVIEW_MAX}")
        self.assertGreater(peak["n"], 1, "…but it must still be concurrent, not serialised")

    def test_a_slot_is_always_given_back(self):
        """A fetch that raises must not leak its slot, or the endpoint dies after _PREVIEW_MAX errors."""
        async def _http():
            def boom(request):
                raise httpx.ConnectError("nope")
            return httpx.AsyncClient(transport=httpx.MockTransport(boom), follow_redirects=False)

        C._preview_http = _http
        for i in range(C._PREVIEW_MAX + 3):
            _run(C.link_preview(f"https://ok.example/boom{i}"))
        self.assertFalse(C._preview_gate.locked(), "slots were leaked by failing fetches")


if __name__ == "__main__":
    unittest.main()
