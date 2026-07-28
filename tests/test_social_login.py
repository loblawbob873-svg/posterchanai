"""Sign-in-with-an-account: the parts that must hold before a key is ever handed out.

Run: venv-unified/bin/python -m unittest tests.test_social_login

These flows are the one place the node mints and hands over a Nostr secret key, so the pieces tested
here are the ones where a mistake is not a broken button but a leaked or wrong identity: the one-time
handoff (single use, expires), the account-handle normalisation a Pleroma login matches existing users
on, and the gate that keeps a half-configured provider off the login screen. The OAuth round-trips
themselves need the real providers and are not mocked into a false sense of coverage here.
"""
import time
import asyncio
import unittest
from unittest import mock

from app.routers import social_login as sl
from app.services.nostr import nostr_service


class _U:
    def __init__(self, nsec, npub, uid=1):
        self.id, self.nostr_nsec, self.nostr_npub = uid, nsec, npub


class TestHandoff(unittest.TestCase):
    def setUp(self):
        sl._HANDOFFS.clear()
        sl._STATES.clear()

    def test_code_is_single_use(self):
        nsec, npub = sl._mint_keypair()
        code = sl._handoff(_U(nsec, npub), "google", "a@b.c", True)
        got = sl.collect_handoff(sl.HandoffRequest(code=code))
        self.assertEqual(got["nsec"], nsec)
        self.assertTrue(got["created"])
        with self.assertRaises(Exception):
            sl.collect_handoff(sl.HandoffRequest(code=code))

    def test_code_expires(self):
        nsec, npub = sl._mint_keypair()
        code = sl._handoff(_U(nsec, npub), "google", "a@b.c", False)
        sl._HANDOFFS[code]["t"] = time.time() - (sl._HANDOFF_TTL + 5)
        with self.assertRaises(Exception):
            sl.collect_handoff(sl.HandoffRequest(code=code))

    def test_unknown_code_is_not_a_key(self):
        with self.assertRaises(Exception):
            sl.collect_handoff(sl.HandoffRequest(code="not-a-real-code"))

    def test_codes_are_unguessable(self):
        nsec, npub = sl._mint_keypair()
        codes = {sl._handoff(_U(nsec, npub), "google", "x", False) for _ in range(50)}
        self.assertEqual(len(codes), 50)
        self.assertTrue(all(len(c) >= 32 for c in codes))

    def test_stale_oauth_state_is_evicted(self):
        sl._STATES["s"] = {"t": time.time() - (sl._STATE_TTL + 5), "p": "google"}
        sl._evict()
        self.assertNotIn("s", sl._STATES)


class TestMintedKey(unittest.TestCase):
    def test_key_is_a_real_usable_keypair(self):
        nsec, npub = sl._mint_keypair()
        self.assertTrue(nsec.startswith("nsec1"))
        self.assertTrue(npub.startswith("npub1"))
        self.assertEqual(nostr_service.npub_from_seckey(nsec), npub)

    def test_keys_are_distinct(self):
        self.assertEqual(len({sl._mint_keypair()[1] for _ in range(20)}), 20)


class TestAcct(unittest.TestCase):
    def test_bare_handle_gets_the_instance_host(self):
        # An instance reports its OWN users bare ("bob"), remote ones qualified. Both have to end up
        # in one form or a login would never match the row an earlier link wrote.
        self.assertEqual(sl._acct_of({"acct": "Bob"}, "https://detroitriotcity.com"),
                         "bob@detroitriotcity.com")

    def test_qualified_handle_is_left_alone(self):
        self.assertEqual(sl._acct_of({"acct": "carol@other.example"}, "https://detroitriotcity.com"),
                         "carol@other.example")

    def test_username_is_the_fallback(self):
        self.assertEqual(sl._acct_of({"username": "dave"}, "https://x.example"), "dave@x.example")

    def test_no_identity_is_not_an_empty_match(self):
        # "" must never be treated as a handle — it would match every unlabelled row.
        self.assertEqual(sl._acct_of({}, "https://x.example"), "")


class TestProviderGate(unittest.TestCase):
    def _with(self, vals):
        return mock.patch.object(sl, "_setting", lambda k, d="": vals.get(k, d))

    def test_google_needs_credentials_not_just_the_switch(self):
        with self._with({"google_login_enabled": "true"}):
            self.assertFalse(sl.providers()["google"])
        with self._with({"google_login_enabled": "true", "google_client_id": "id",
                         "google_client_secret": "sec"}):
            self.assertTrue(sl.providers()["google"])

    def test_off_by_default(self):
        with self._with({}):
            p = sl.providers()
            self.assertFalse(p["google"])
            self.assertFalse(p["pleroma"])

    def test_pleroma_instance_falls_back_to_the_bridge(self):
        with self._with({"pleroma_login_enabled": "true",
                         "fedi_bridge_instance_url": "https://detroitriotcity.com"}):
            p = sl.providers()
            self.assertTrue(p["pleroma"])
            self.assertEqual(p["pleroma_instance"], "https://detroitriotcity.com")


class TestRedirectOrigin(unittest.TestCase):
    """The redirect_uri must be the PUBLIC https origin, not uvicorn's view of the request.

    TLS stops at the reverse proxy, so the raw request is plain http on a LAN hop. Both providers
    match the redirect_uri exactly against the registered one, and Google refuses http for a Web
    client — so getting this wrong is not a cosmetic URL, it is every sign-in failing.
    """
    def _req(self, headers, scheme="http", netloc="192.168.0.9:3051"):
        return mock.Mock(headers=headers, url=mock.Mock(scheme=scheme, netloc=netloc))

    def test_forwarded_proto_beats_the_raw_scheme(self):
        r = self._req({"x-forwarded-proto": "https", "host": "poster.place"})
        self.assertEqual(sl._base_url(r), "https://poster.place")

    def test_forwarded_host_beats_the_raw_netloc(self):
        r = self._req({"x-forwarded-proto": "https", "x-forwarded-host": "poster.place",
                       "host": "192.168.0.9:3051"})
        self.assertEqual(sl._base_url(r), "https://poster.place")

    def test_proxy_chain_takes_the_first_hop(self):
        # X-Forwarded-* accumulate comma-separated through Cloudflare → nginx; the client-facing
        # value is the FIRST, and passing the whole list would build a syntactically broken URI.
        r = self._req({"x-forwarded-proto": "https, http", "x-forwarded-host": "poster.place, nas.lan"})
        self.assertEqual(sl._base_url(r), "https://poster.place")

    def test_no_proxy_headers_never_downgrades_to_http(self):
        # Direct hit with nothing forwarded: fall back to what the request claims, and https last —
        # an http redirect_uri is rejected by Google outright, so it is never the safer default.
        self.assertEqual(sl._base_url(self._req({"host": "poster.place"}, scheme="")),
                         "https://poster.place")


class TestConfiguredInstanceIsNotVisitorInput(unittest.TestCase):
    """The SSRF guard must not lock out the node's OWN instance.

    A self-hosted instance usually resolves to a LAN address from inside its own network (split-horizon
    DNS: detroitriotcity.com is 192.168.0.1 on this box, via the nginx that also serves /static). The
    guard exists for the URL an anonymous visitor types, but applied to the OPERATOR's configured
    instance it blocked the one instance the node most needs to work while allowing every stranger's.
    """
    def _req(self):
        return mock.Mock(headers={"x-forwarded-proto": "https", "x-forwarded-host": "poster.place"},
                         url=mock.Mock(scheme="http", netloc="10.0.0.1:3051"))

    def test_host_normalisation_ignores_scheme_and_trailing_slash(self):
        for spelling in ("https://Fedi.Example/", "fedi.example", "http://fedi.example",
                         "https://fedi.example/api/v1"):
            self.assertEqual(sl._norm_host(spelling), "fedi.example", spelling)

    def test_blank_settings_do_not_become_an_empty_allowlist_entry(self):
        # "" in the set would exempt an unparseable URL — the same class of bug as matching an
        # empty handle against every unlabelled row.
        with mock.patch.object(sl, "_setting", lambda k, d="": ""):
            self.assertEqual(sl._configured_instances(), set())

    def _start(self, instance, settings):
        with mock.patch.object(sl, "_setting", lambda k, d="": settings.get(k, d)), \
             mock.patch("app.services.pleroma_service.register_app",
                        new=mock.AsyncMock(return_value={"client_id": "cid", "client_secret": "sec"})), \
             mock.patch("app.services.pleroma_service.build_auth_url", lambda *a, **k: "https://i/oauth"):
            return asyncio.run(sl.pleroma_start(sl.PleromaLoginStart(instance_url=instance), self._req()))

    def test_the_operators_own_lan_instance_is_allowed(self):
        sl._STATES.clear()
        cfg = {"pleroma_login_enabled": "true", "pleroma_login_instance": "https://detroitriotcity.com"}
        # is_safe_host says NO (it resolves to a private address) and it must still go through.
        with mock.patch("app.services.rss_service.is_safe_host", lambda *_: False):
            out = self._start("https://detroitriotcity.com", cfg)
        self.assertIn("auth_url", out)

    def test_a_stranger_typing_a_lan_address_is_still_refused(self):
        cfg = {"pleroma_login_enabled": "true", "pleroma_login_instance": "https://detroitriotcity.com"}
        with mock.patch("app.services.rss_service.is_safe_host", lambda *_: False), \
             self.assertRaises(Exception) as e:
            self._start("http://192.168.0.50", cfg)
        self.assertIn("isn't allowed", str(getattr(e.exception, "detail", e.exception)))


if __name__ == "__main__":
    unittest.main()
