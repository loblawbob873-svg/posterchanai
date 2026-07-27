"""Tor / .onion support: the URLs an onion visitor is handed must stay ON the onion.

Run: venv-unified/bin/python -m unittest tests.test_onion_urls

The failure this pins down is quiet rather than loud. A hidden service that serves the client shell but
then answers /client/config with the admin's clearnet relay + media host is a facade: every socket and
every image immediately leaves Tor through an exit node, and an onion-only client (no Orbot route to the
clearnet, or an instance that simply isn't published on the clearnet) can't reach them at all. Worse for
Blossom — the upload response URL is what gets EMBEDDED IN THE NOTE the user publishes, so a clearnet
media host stamps the instance's real domain into an onion user's posts, permanently.

Host is client-controlled, so the second half of this is the spoof guard: an arbitrary `Host: evil.onion`
must never be echoed back as a URL we hand out. It has to match the address Tor actually generated.
"""
import unittest
from unittest import mock

from app.services import tor_service


class _Req:
    """The two attributes request_onion_host() reads off a Starlette Request."""

    def __init__(self, host, query=None):
        self.headers = {"host": host}
        self.url = type("U", (), {"netloc": host, "scheme": "http"})()
        self.query_params = query or {}
        self.cookies = {}


class TestRequestOnionHost(unittest.TestCase):
    def test_clearnet_host_is_not_an_onion(self):
        with mock.patch.object(tor_service, "get_onion_address", return_value="abc.onion"):
            self.assertEqual(tor_service.request_onion_host(_Req("poster.place")), "")

    def test_matching_onion_is_recognised_with_and_without_port(self):
        with mock.patch.object(tor_service, "get_onion_address", return_value="abc.onion"):
            self.assertEqual(tor_service.request_onion_host(_Req("abc.onion")), "abc.onion")
            self.assertEqual(tor_service.request_onion_host(_Req("abc.onion:80")), "abc.onion")
            self.assertEqual(tor_service.request_onion_host(_Req("ABC.ONION")), "abc.onion")

    def test_spoofed_onion_host_is_refused(self):
        """`Host: evil.onion` on a node whose onion is abc.onion must not become a URL we emit."""
        with mock.patch.object(tor_service, "get_onion_address", return_value="abc.onion"):
            self.assertEqual(tor_service.request_onion_host(_Req("evil.onion")), "")

    def test_no_onion_configured_refuses_every_onion_host(self):
        """With the hidden service off there is nothing to match against — never trust the header alone."""
        with mock.patch.object(tor_service, "get_onion_address", return_value=None):
            self.assertEqual(tor_service.request_onion_host(_Req("abc.onion")), "")


class TestOnionTorrc(unittest.TestCase):
    def test_relay_port_rides_the_same_hidden_service(self):
        """Tor forwards TCP, not paths: without an explicit port line the onion has no relay at all,
        and the client (which is relay-first) comes up empty."""
        import tempfile
        from pathlib import Path
        svc = tor_service.TorService(data_dir=tempfile.mkdtemp(), onion_enabled=True,
                                     onion_target="127.0.0.1:3051", onion_relay_port=3052)
        torrc = Path(svc._create_torrc()).read_text()
        self.assertIn("HiddenServicePort 80 127.0.0.1:3051", torrc)
        self.assertIn("HiddenServicePort 3052 127.0.0.1:3052", torrc)

    def test_no_relay_port_means_no_extra_line(self):
        import tempfile
        from pathlib import Path
        svc = tor_service.TorService(data_dir=tempfile.mkdtemp(), onion_enabled=True,
                                     onion_target="127.0.0.1:3051", onion_relay_port=0)
        torrc = Path(svc._create_torrc()).read_text()
        self.assertIn("HiddenServicePort 80 127.0.0.1:3051", torrc)
        self.assertEqual(torrc.count("HiddenServicePort"), 1)


class TestOnionAwareUrls(unittest.TestCase):
    """_relay_url / _blossom_url / blossom._base_url must all prefer the onion over the configured
    clearnet values — and must be untouched for a normal clearnet request."""

    def setUp(self):
        from app.routers import client as client_router
        from app.routers import blossom as blossom_router
        self.client_router = client_router
        self.blossom_router = blossom_router
        # The production shape: explicit clearnet relay + media host configured.
        settings = {"client_relay_url": "wss://poster.place/relay",
                    "blossom_public_url": "https://media.poster.place",
                    "nostr_relay_port": "3052"}
        self.p = mock.patch.object(client_router, "_setting",
                                   lambda db, k, d="": settings.get(k, d))
        self.p.start(); self.addCleanup(self.p.stop)
        self.p2 = mock.patch.object(blossom_router.blossom_service, "_cfg",
                                    lambda db: {"public_url": "https://media.poster.place"})
        self.p2.start(); self.addCleanup(self.p2.stop)

    def test_clearnet_request_keeps_the_configured_urls(self):
        with mock.patch.object(tor_service, "get_onion_address", return_value="abc.onion"):
            r = _Req("poster.place")
            self.assertEqual(self.client_router._relay_url(r, None), "wss://poster.place/relay")
            self.assertEqual(self.client_router._blossom_url(r, None), "https://media.poster.place")
            self.assertEqual(self.blossom_router._base_url(r, None), "https://media.poster.place")

    def test_onion_request_gets_onion_urls(self):
        with mock.patch.object(tor_service, "get_onion_address", return_value="abc.onion"):
            r = _Req("abc.onion")
            self.assertEqual(self.client_router._relay_url(r, None), "ws://abc.onion:3052/relay")
            self.assertEqual(self.client_router._blossom_url(r, None), "http://abc.onion/blossom")
            self.assertEqual(self.blossom_router._base_url(r, None), "http://abc.onion/blossom")

    def test_onion_blob_url_is_the_onion_not_the_media_domain(self):
        """The published note carries this string — a clearnet host here deanonymises the instance."""
        from app.services import blossom_service
        blob = mock.Mock(sha256="a" * 64, size=1, mime="image/png", created_at=0)
        with mock.patch.object(tor_service, "get_onion_address", return_value="abc.onion"):
            base = self.blossom_router._base_url(_Req("abc.onion"), None)
        self.assertEqual(blossom_service.descriptor(blob, base)["url"],
                         "http://abc.onion/blossom/" + "a" * 64)


if __name__ == "__main__":
    unittest.main()
