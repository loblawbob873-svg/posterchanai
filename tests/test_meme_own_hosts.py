"""The Meme Builder's SSRF guard exempts the hosts THIS deployment serves — and nothing else.

Run: venv-unified/bin/python -m unittest tests.test_meme_own_hosts

Split-horizon DNS is what makes this necessary: our own public names resolve to a LAN address from
inside the network (media.poster.place -> 192.168.0.1), so is_safe_host reads them as private and
refuses them. The Blossom bases were exempted for that reason; a layer served from the deployment's
own fediverse instance hit the same wall and produced "refused source: https://…/media/…" on every
render. The extra names are an admin setting rather than a hardcoded list.

What matters here is the PARSE: this list decides which hosts skip the private-address check, so a
line that widens it beyond one exact hostname is a security bug, not a cosmetic one.

The list MOVED to search_service, next to the guard it exempts, because a second copy in the router
is what let the two disagree: the meme render path trusted media.poster.place while AI chat refused
the identical URL as "Private IP not allowed: 192.168.0.1". The parse assertions below are unchanged
across that move — that is what makes them evidence the move changed no behaviour.
"""
import unittest
from unittest import mock

from app.routers import client as client_router
from app.services import search_service


def _hosts(media_own_hosts, blossom="https://media.poster.place/blossom", dvm=""):
    vals = {"blossom_public_url": blossom, "nostr_dvm_blossom_url": dvm,
            "media_own_hosts": media_own_hosts}
    with mock.patch.object(search_service.settings_store, "get",
                           lambda k, *a, **kw: vals.get(k, "")):
        return search_service.own_media_hosts()


class TestOwnMediaHosts(unittest.TestCase):
    def test_blossom_bases_are_always_exempt(self):
        # The pre-existing exemption still stands with the setting empty.
        self.assertEqual(_hosts(""), {"media.poster.place"})
        self.assertIn("media.example", _hosts("", blossom="", dvm="https://media.example/x"))

    def test_bare_hostname_url_and_case(self):
        # An admin pastes whichever form is at hand; all three name the same host.
        for line in ("detroitriotcity.com", "https://detroitriotcity.com/media/abc.jpg",
                     "DetroitRiotCity.com", "detroitriotcity.com/media/"):
            self.assertIn("detroitriotcity.com", _hosts(line), line)

    def test_newline_and_comma_separated(self):
        got = _hosts(" poster.place \n\n, detroitriotcity.com ,\n")
        self.assertEqual(got, {"media.poster.place", "poster.place", "detroitriotcity.com"})

    def test_blank_setting_adds_nothing(self):
        for blank in ("", "   ", "\n\n", ",", None):
            self.assertEqual(_hosts(blank), {"media.poster.place"}, repr(blank))

    def test_matching_is_exact_no_wildcards(self):
        # A wildcard must NOT quietly exempt a whole zone: the entry is one hostname, so
        # "*.poster.place" matches nothing rather than every subdomain of it.
        got = _hosts("*.poster.place")
        self.assertNotIn("evil.poster.place", got)
        self.assertNotIn("poster.place", got)

    def test_a_port_does_not_leak_into_the_host(self):
        # "host:8080" must yield the HOST, or the entry silently never matches.
        self.assertIn("evil.example", _hosts("evil.example:8080"))

    def test_unlisted_host_is_not_exempt(self):
        self.assertNotIn("attacker.example", _hosts("poster.place"))


class TestGuardStillApplies(unittest.TestCase):
    """_fetch_media_guarded consults `own` ONLY to skip the guard — an unlisted host still runs it."""

    def _run(self, url, own, safe):
        """Returns "fetched" if the request was actually attempted, else the refusal detail."""
        import asyncio

        async def _fake_get(self, u, **kw):
            return mock.Mock(status_code=200, headers={"content-type": "image/jpeg"}, content=b"x",
                             raise_for_status=lambda: None)

        with mock.patch("app.services.rss_service.is_safe_host", return_value=safe), \
             mock.patch("app.services.rss_service.looks_fetchable", return_value=True), \
             mock.patch("httpx.AsyncClient.get", _fake_get):
            try:
                asyncio.run(client_router._fetch_media_guarded(url, own))
                return "fetched"
            except Exception as e:
                return getattr(e, "detail", str(e))

    def test_private_resolving_host_is_refused_unless_listed(self):
        url = "https://detroitriotcity.com/media/abc.jpg"
        self.assertEqual(self._run(url, set(), safe=False), "refused image source")
        self.assertEqual(self._run(url, {"detroitriotcity.com"}, safe=False), "fetched")

    def test_public_host_needs_no_exemption(self):
        self.assertEqual(self._run("https://example.com/a.jpg", set(), safe=True), "fetched")

    def test_non_http_scheme_is_refused_even_when_listed(self):
        self.assertEqual(self._run("file:///etc/passwd", {""}, safe=True), "bad image url")


class TestGuardHonoursOwnHosts(unittest.TestCase):
    """Reported from AI chat: "Couldn't fetch text from https://media.poster.place/….jpg: URL blocked:
    Private IP not allowed: 192.168.0.1".

    The exemption existed but only the meme/effect fetch path consulted it, so the SAME url was fine
    for a render and refused for anything else — reading a page, `translate <url>`, link previews.
    is_safe_url is the guard all of those share, so that is where it is asserted."""

    def _safe(self, url, own_hosts):
        vals = {"blossom_public_url": "", "nostr_dvm_blossom_url": "", "media_own_hosts": own_hosts}
        with mock.patch.object(search_service.settings_store, "get",
                               lambda k, *a, **kw: vals.get(k, "")), \
             mock.patch.object(search_service.socket, "gethostbyname", return_value="192.168.0.1"):
            return search_service.is_safe_url(url)

    def test_a_listed_own_host_is_allowed_despite_the_private_address(self):
        ok, why = self._safe("https://media.poster.place/abc.jpg", "media.poster.place")
        self.assertTrue(ok, why)

    def test_an_unlisted_host_resolving_privately_is_still_blocked(self):
        ok, why = self._safe("https://attacker.example/abc.jpg", "media.poster.place")
        self.assertFalse(ok)
        self.assertIn("Private IP", why)

    def test_listing_nothing_keeps_the_guard_shut(self):
        # The fix must not grant blanket trust: with the setting empty this URL is still refused,
        # which is why it is CONFIG rather than a hardcoded name.
        ok, _ = self._safe("https://media.poster.place/abc.jpg", "")
        self.assertFalse(ok)


if __name__ == "__main__":
    unittest.main()
