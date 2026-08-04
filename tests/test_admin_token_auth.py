"""The admin panel authenticates with a TOKEN, not a cookie.

The panel is a page from the instance, framed by the Nostr client. An iframe's document load carries
only COOKIES, so while the PAGE was the thing being authorised the panel needed one — and in a bundled
app (desktop `app://posterchan`, the APK) that cookie is cross-site, which needs SameSite=None, which
browsers accept only with Secure, which is only sent over HTTPS. A .onion is plain HTTP by design, so
against one the panel could not work at all: /admin saw no session, redirected to the client, and the
app rendered the website where the panel should be.

So the page is served unauthenticated — it holds no data, only fields — and the client hands it the
bearer token over postMessage, which is scheme-agnostic. Verified end to end in a browser (a framed
/admin, cookies never set): with the token the panel's own /api/admin/settings succeeds; without it the
page says so instead of showing empty fields.

The assertions here are the parts that fail SILENTLY: an ordering mistake means the wrapper is
installed after the first request, and a wrong postMessage target means a credential is broadcast.
"""
import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)

AUTH_JS = os.path.join(ROOT, "static", "js", "admin-auth.js")
ADMIN_HTML = os.path.join(ROOT, "templates", "admin.html")
APP_JS = os.path.join(ROOT, "static", "js", "client", "app.js")
MAIN_PY = os.path.join(ROOT, "app", "main.py")


def _read(p):
    with open(p, encoding="utf-8") as fh:
        return fh.read()


def test_the_page_is_served_without_a_session():
    """It cannot require one: the token that authorises it arrives AFTER it has loaded."""
    src = _read(MAIN_PY)
    i = src.index("async def admin_page(")
    body = src[i:i + 2000]
    assert 'if current_user and not current_user.is_admin' in body, \
        "the non-admin redirect should survive; only the anonymous refusal goes"
    assert 'if not current_user:\n        return RedirectResponse' not in body, \
        "/admin still refuses anonymous visitors, so the framed panel can never receive its token"


def test_the_shell_carries_no_server_rendered_identity():
    """window.isAdmin was the last templated value and nothing read it. If something puts one back,
    the page stops being safe to serve unauthenticated."""
    html = _read(ADMIN_HTML)
    # An assignment, not a mention — the comment explaining why it went names it.
    assert not re.search(r"window\.isAdmin\s*=", html)
    assert not re.search(r"\{\{[^}]*\buser\b", html), "the shell renders a server-side user value again"


def test_the_auth_wrapper_loads_before_any_admin_script():
    html = _read(ADMIN_HTML)
    a = html.index("admin-auth.js")
    for later in ("/static/js/admin.js", "admin-bots.js", "admin-emoji.js"):
        assert a < html.index(later), f"{later} loads before admin-auth.js — its first call goes out unauthenticated"


def test_the_page_only_accepts_a_token_from_its_embedder():
    js = _read(AUTH_JS)
    assert "e.source !== window.parent" in js, "any frame or window could hand this page a credential"
    assert "'pc-admin-token'" in js


def test_the_gate_asks_with_the_same_credentials_as_everything_else():
    """The bug the end-to-end test caught: the gate called the CAPTURED original fetch, so it checked
    without the token it had just been given and declared a perfectly good admin session unusable."""
    js = _read(AUTH_JS)
    m = re.search(r"__pcAdminAuth[\s\S]{0,400}", js)
    assert m and "window.fetch(" in m.group(0), \
        "the auth gate must go through the wrapper, or it reports 'not an admin' while every real call works"


def test_the_wrapper_never_clobbers_an_existing_authorization():
    js = _read(AUTH_JS)
    assert "get('Authorization')" in js and "has = true" in js, \
        "an upload carrying its own NIP-98 header would have it overwritten"


def test_the_client_sends_the_token_to_one_exact_origin():
    """A credential must not be broadcast. '*' here would hand the session to any page that manages to
    end up in that frame."""
    js = _read(APP_JS)
    i = js.index("function _sendAdminToken()")
    body = js[i:i + 500]
    assert "postMessage({ type:'pc-admin-token'" in body
    assert "'*'" not in body, "the token is posted with a wildcard target origin"
    assert "_adminOrigin()" in body


def test_the_hello_is_answered_only_for_our_own_frame():
    js = _read(APP_JS)
    i = js.index("function _bindAdminTokenBridge()")
    body = js[i:i + 600]
    assert "e.source!==_adminFrameEl.contentWindow" in body, \
        "any window could trigger a token send by posting a hello"
