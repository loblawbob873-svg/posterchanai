"""The client shell must not contain anything that switches off Chrome's preload scanner.

Reported twice as "poster.place looks stuck loading on mobile": the bottom nav drawn from the raw
HTML, no header, no feed, and Chrome's progress bar still running. The app boots on
DOMContentLoaded, which waits for ~70 blocking <script> tags, and those were being fetched ONE AT A
TIME, each after the previous had executed, because of a single line in <head>:

    <meta http-equiv="Content-Security-Policy" content="upgrade-insecure-requests">

Chromium's preload scanner stops preloading once it meets a CSP <meta>. Measured on a copy of the
real page served with 200 ms per request: 15.0 s to DOMContentLoaded with the meta, 3.2 s without.
On a 4G profile the live site took 15.7 s cold and 13.3 s on a repeat visit (all 304s, but in
series); on Fast 3G, 58.7 s.

The policy itself is still wanted (it upgrades http:// avatars on an https page), so it moved to the
RESPONSE HEADER, which means the same thing to the browser and does not touch the preload scanner.
It is still sent only over https: over plain http (a LAN node, an .onion) it would rewrite every
script URL to an https:// host that does not exist.
"""
import asyncio
import re

from starlette.requests import Request

from app.routers import client as client_router


def _request(proto):
    scope = {"type": "http", "method": "GET", "path": "/", "raw_path": b"/", "query_string": b"",
             "scheme": "http", "server": ("poster.place", 80), "client": ("1.2.3.4", 1),
             "root_path": "", "headers": [(b"host", b"poster.place"),
                                          (b"x-forwarded-proto", proto.encode())]}
    return Request(scope)


def _render(monkeypatch, proto):
    monkeypatch.setattr(client_router, "_default_theme", lambda db: "cyberpunk")
    return asyncio.run(client_router.render_client_shell(_request(proto), db=None))


def _html(resp):
    return resp.body.decode("utf-8")


def test_no_csp_meta_in_the_shell_so_scripts_load_in_parallel(monkeypatch):
    for proto in ("https", "http"):
        html = _html(_render(monkeypatch, proto))
        assert not re.search(r'<meta[^>]+http-equiv=["\']?content-security-policy', html, re.I), (
            f"a CSP <meta> is in the shell ({proto}): it disables Chrome's preload scanner and the "
            "~70 blocking scripts load one after another — the 'stuck loading on mobile' bug")


def test_https_shell_still_upgrades_insecure_requests_via_the_header(monkeypatch):
    resp = _render(monkeypatch, "https")
    assert "upgrade-insecure-requests" in resp.headers.get("content-security-policy", ""), (
        "over https the page must still upgrade http:// avatars/media, now through the header")


def test_plain_http_shell_sends_no_upgrade(monkeypatch):
    resp = _render(monkeypatch, "http")
    assert "upgrade-insecure-requests" not in resp.headers.get("content-security-policy", ""), (
        "over plain http the upgrade would point every script at an https:// host that does not exist")


def test_the_scripts_are_still_plain_blocking_tags(monkeypatch):
    """The fix relies on the preload scanner seeing the scripts in the markup. If they ever move to a
    JS loader, this test and the timing check need rethinking together."""
    html = _html(_render(monkeypatch, "https"))
    assert len(re.findall(r'<script src="/static/js/client/', html)) > 20
