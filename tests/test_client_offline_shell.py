"""The offline shell is a hand-copied list, so it drifts — these assertions are what notice.

Run: venv-unified/bin/python -m unittest tests.test_client_offline_shell

sw.js precaches a literal SHELL array of the client's modules, and templates/client.html loads them
with its own <script> tags. Nothing connected the two, and they had already diverged: stats.js was in
the page and absent from SHELL, so it was the one module the installed app could not load offline —
with nothing anywhere to say so, because the failure only appears to someone with no network.

The version-token assertions matter for the same invisible-failure reason. The page requests
`app.js?v=<mtime>` while SHELL lists the bare path, so the worker has to match with ignoreSearch or the
precache can answer no request at all; and it must NOT match loosely first, or a UI-only deploy (which
bumps ?v= but deliberately not CACHE) would serve the previous build with no update prompt to correct
it. Both directions are load-bearing and neither is visible in a browser that is online.
"""
import os
import re
import unittest

_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_SW = os.path.join(_ROOT, "static", "js", "client", "sw.js")
_PAGE = os.path.join(_ROOT, "templates", "client.html")


def _read(path):
    with open(path, encoding="utf-8") as fh:
        return fh.read()


def _shell_entries(sw_src):
    body = re.search(r"const SHELL = \[(.*?)\];", sw_src, re.S)
    assert body, "SHELL array not found in sw.js"
    return set(re.findall(r"'([^']+)'", body.group(1)))


def _page_assets(page_src):
    """Same-origin JS/CSS the shell page pulls in, with the ?v= cache-buster stripped."""
    urls = re.findall(r'(?:src|href)="(/static/[^"?]+)(?:\?v=\{\{ ver \}\})?"', page_src)
    return {u for u in urls if u.endswith(".js") or u.endswith(".css")}


class TestShellCoversThePage(unittest.TestCase):
    def test_every_script_and_stylesheet_is_precached(self):
        shell = _shell_entries(_read(_SW))
        missing = sorted(a for a in _page_assets(_read(_PAGE)) if a not in shell)
        self.assertEqual(
            missing, [],
            "client.html loads these but sw.js SHELL does not precache them, so they are "
            "unavailable offline: " + ", ".join(missing),
        )

    def test_shell_includes_the_navigation_entry(self):
        self.assertIn("/client", _shell_entries(_read(_SW)),
                      "SHELL must precache the '/client' document or a cold offline launch has no shell")


class TestVersionedUrlMatching(unittest.TestCase):
    def test_page_versions_its_assets(self):
        # If this stops being true the ignoreSearch handling below is dead weight — and, more to the
        # point, the cache-busting that Cloudflare's 31-day max-age depends on is gone.
        self.assertRegex(_read(_PAGE), r'/static/js/client/app\.js\?v=\{\{ ver \}\}')

    def test_worker_can_match_a_versioned_request(self):
        self.assertIn("ignoreSearch", _read(_SW),
                      "SHELL lists bare paths while the page requests ?v=<mtime>; without an "
                      "ignoreSearch fallback the precache can never answer a real request")

    def test_exact_match_is_tried_before_the_loose_one(self):
        src = _read(_SW)
        fn = re.search(r"function staleWhileRevalidate\(req\)\{(.*?)\n\}", src, re.S)
        self.assertTrue(fn, "staleWhileRevalidate not found in sw.js")
        body = fn.group(1)
        exact = body.find("cache.match(req)")
        loose = body.find("_stale(")
        self.assertNotEqual(exact, -1, "staleWhileRevalidate must try an EXACT match first")
        self.assertNotEqual(loose, -1, "staleWhileRevalidate needs the loose match as its offline fallback")
        self.assertLess(
            exact, loose,
            "the loose (ignoreSearch) match must be the OFFLINE FALLBACK, not the primary lookup: a "
            "UI-only deploy bumps ?v= without bumping CACHE, so matching loosely first would serve the "
            "previous build and no controllerchange would fire to prompt an update",
        )

    def test_app_code_uses_the_network_first_path(self):
        """A reload must receive the deployed client, not cache it only for the next reload."""
        src = _read(_SW)
        self.assertIn("if (isAppCode) e.respondWith(freshFirst(e.request))", src)
        self.assertNotIn("if (isAppCode) e.respondWith(staleWhileRevalidate(e.request))", src)


class TestNavigationsSurviveAQueryString(unittest.TestCase):
    def test_shortcut_and_share_urls_reach_the_cached_shell(self):
        # The manifest's shortcuts and the share target all land on /client with a query string. Exact-URL
        # matching missed every one of them, which made the home-screen icons the only guaranteed-offline
        # FAILURE in the app. shellDoc keys the document under a stable '/client' instead.
        src = _read(_SW)
        self.assertIn("function shellDoc(", src)
        self.assertRegex(src, r"e\.request\.mode === 'navigate'")
        self.assertRegex(src, r"cache\.put\('/client'")

    def test_config_is_cached_rather_than_bypassed(self):
        # /client/config carries relay_url. Bypassing the worker meant an offline boot did not know which
        # relay to reconnect to when the radio came back.
        src = _read(_SW)
        self.assertIn("networkFirst(e.request)", src)
        self.assertNotRegex(
            src, r"pathname\.startsWith\('/client/config'\)\) return",
            "/client/config must not bypass the worker — offline it leaves the client with no relay URL",
        )

    def test_first_paint_does_not_wait_forever_for_config(self):
        # Android can report itself online while an associated but poor network leaves fetch pending
        # for minutes. boot() runs before either app surface becomes visible, so an unbounded config
        # request is a literal black screen for every launcher shortcut.
        src = _read(os.path.join(_ROOT, "static", "js", "client", "app.js"))
        boot = re.search(r"async function boot\(\)\{(.*?)\n  \}", src, re.S)
        self.assertTrue(boot, "boot() not found in app.js")
        body = boot.group(1)
        self.assertIn("Promise.race", body)
        self.assertRegex(body, r"setTimeout\(\(\)=>resolve\(null\),\s*2500\)")
        self.assertIn("_bootCfg || _cfgCached() || {}", body)

    def test_late_config_response_still_refreshes_the_cache(self):
        src = _read(os.path.join(_ROOT, "static", "js", "client", "app.js"))
        boot = re.search(r"async function boot\(\)\{(.*?)\n  \}", src, re.S)
        self.assertTrue(boot)
        self.assertRegex(boot.group(1), r"\.then\(c=>\{ if\(c\) _cfgCache\(c\)")


class TestOutboxSafety(unittest.TestCase):
    """The queue may only hold kinds where re-sending the identical event is a no-op at the relay."""

    def test_no_replaceable_kind_is_queueable(self):
        src = _read(os.path.join(_ROOT, "static", "js", "client", "outbox.js"))
        m = re.search(r"const QUEUEABLE = new Set\(\[([^\]]*)\]\)", src)
        self.assertTrue(m, "QUEUEABLE set not found in outbox.js")
        kinds = {int(k) for k in re.findall(r"\d+", m.group(1))}
        self.assertTrue(kinds, "QUEUEABLE must not be empty")
        for k in sorted(kinds):
            # 0 and 3 are replaceable, 10000-19999 replaceable, 30000-39999 addressable: every one of them
            # overwrites a whole document, which is the exact shape that erased a follows list before.
            self.assertNotIn(k, (0, 3), f"kind {k} is replaceable and must never be queued")
            self.assertFalse(10000 <= k < 20000, f"kind {k} is replaceable and must never be queued")
            self.assertFalse(30000 <= k < 40000, f"kind {k} is addressable and must never be queued")
            # A delete the user believes failed must not fire later from a queue they have forgotten.
            self.assertNotEqual(k, 5, "kind 5 (delete) is destructive and must never be queued")


if __name__ == "__main__":
    unittest.main()
