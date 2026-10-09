"""The APK on our .onion: a preflight asking for PRIVATE-NETWORK access must be granted to the native app's
origin and still refused to anyone else.

Reported: an APK on poster.place's .onion got "could not establish your app session: Failed to fetch" while
the relay worked. Under Orbot an .onion resolves to a private address, so the WebView's preflight carries
`Access-Control-Request-Private-Network: true`; the app answered it 400 (measured), which a page sees as a
network failure. Driven against the real app and its real middleware stack."""
from fastapi.testclient import TestClient

import app.main as main

client = TestClient(main.app)
PNA = {"Access-Control-Request-Method": "POST", "Access-Control-Request-Headers": "content-type",
       "Access-Control-Request-Private-Network": "true"}


def test_the_native_app_may_reach_us_on_a_private_address():
    for origin in ("https://localhost", "app://posterchan"):
        r = client.options("/api/auth/nostr-login", headers=dict(PNA, Origin=origin))
        assert r.status_code == 200, (origin, r.status_code, r.text)
        assert r.headers.get("access-control-allow-origin") == origin
        assert r.headers.get("access-control-allow-private-network") == "true"


def test_an_unlisted_origin_is_still_refused():
    r = client.options("/api/auth/nostr-login", headers=dict(PNA, Origin="https://evil.example"))
    # refused: no allow-origin and a 400 (the private-network header alone grants nothing to a browser)
    assert r.status_code == 400 and r.headers.get("access-control-allow-origin") is None


def test_the_ordinary_preflight_is_unchanged():
    h = {k: v for k, v in PNA.items() if "Private" not in k}
    r = client.options("/api/auth/nostr-login", headers=dict(h, Origin="https://localhost"))
    assert r.status_code == 200 and r.headers.get("access-control-allow-origin") == "https://localhost"
