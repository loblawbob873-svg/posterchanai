"""The session cookie must be usable from a BUNDLED client, which calls this API cross-site.

The desktop app serves its client from its own privileged scheme (app://posterchan). That origin was
added to the CORS allowlist in app/main.py and never to the cookie allowlist here, so login responses
came back SameSite=Lax — which a browser will not even STORE from a cross-site response, let alone send
on a cross-site request.

The symptom was specific and looked like anything but a cookie flag: the Admin panel is an IFRAME of
the instance's /admin page, an iframe document load carries only COOKIES (never the Authorization
bearer the bundled fetch shim attaches to XHR), so /admin saw no session and redirected to the client —
and the app rendered the WEBSITE inside the admin pane. Reported as "Tor loads the website again, not
admin", with Tor incidental: the cookie was missing with or without it.

SameSite=None requires Secure, so this only helps against an HTTPS instance. A plain-HTTP one (a .onion,
a LAN box) genuinely cannot hold this session in a bundled app; the client says so instead of showing a
timeline where the admin panel belongs.
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from app.routers import auth  # noqa: E402


class _Req:
    def __init__(self, origin, scheme="https", xfp=None):
        self.headers = {"origin": origin}
        if xfp:
            self.headers["x-forwarded-proto"] = xfp
        self.url = type("U", (), {"scheme": scheme})()


@pytest.mark.parametrize("origin", ["app://posterchan", "capacitor://localhost", "https://localhost"])
def test_bundled_clients_get_a_cross_site_cookie(origin):
    samesite, secure = auth._cookie_attrs(_Req(origin))
    assert (samesite, secure) == ("none", True), (
        f"{origin} calls this API cross-site; SameSite={samesite} means the browser never stores the "
        "session cookie, and the admin iframe then loads the instance's timeline instead of the panel")


def test_the_web_pwa_keeps_lax():
    """Same-origin browsers must NOT get None — SameSite is the only CSRF defence here (the middleware
    is disabled), and None would let the session ride cross-site POSTs."""
    assert auth._cookie_attrs(_Req("https://poster.place")) == ("lax", True)


def test_plain_http_does_not_claim_secure():
    """A LAN instance over http must still be able to set a cookie at all."""
    assert auth._cookie_attrs(_Req("http://nas.lan:3051", scheme="http")) == ("lax", False)
