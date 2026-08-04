"""Getting to /admin without a cookie must not dead-end.

Reported as "Admin settings over Tor loads the default instance page, not logged in", and it is not a
Tor bug — a .onion is a SEPARATE ORIGIN, so the cookie held for the clearnet host is never sent to it
and every visit there starts logged out. On the domain you already have a session, so the path below
was never walked; over Tor (or in a new browser, or on a second device) it is walked every time:

    /admin  -> 302 /login      (no session)
    /login  -> 302 /client     ('next' accepted and DISCARDED)

…which lands you on the timeline with no sign-in prompt, no mention of admin, and nothing to go back
to. The password login page was retired deliberately — a Nostr sign-in in the client sets the very
cookie /admin needs — so the fix is not to bring it back, it is to send people to the sign-in surface
in ONE hop and remember where they were going.

The open-redirect cases are not hypothetical politeness: `next` comes from a URL, and "//evil.com" is
a protocol-relative URL that browsers follow off-site.
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pytest  # noqa: E402

from app.main import _safe_next, admin_page, login_page  # noqa: E402


@pytest.mark.parametrize("raw,want", [
    ("/admin", "/admin"),
    ("/admin?tab=nodes", "/admin?tab=nodes"),
    ("//evil.com", ""),
    ("https://evil.com", ""),
    ("http://evil.com", ""),
    ("javascript:alert(1)", ""),
    ("/back\\slash", ""),
    ("", ""),
    (None, ""),
    # Browsers DELETE tab/newline/CR from a URL before resolving it, so each of these reads as
    # "/<something>/evil.com" to a leading-character check and navigates to //evil.com — off-site.
    # Caught reviewing the first version of this function, which accepted all three.
    ("/\t/evil.com", ""),
    ("/\n/evil.com", ""),
    ("/\r/evil.com", ""),
    ("/\x7f/evil.com", ""),
])
def test_only_same_origin_paths_survive(raw, want):
    assert _safe_next(raw) == want


def test_admin_no_longer_refuses_a_logged_out_visitor():
    """SUPERSEDED, deliberately. /admin used to redirect an anonymous visitor to /client?next=/admin,
    which fixed the dead end but kept the page itself as the thing being authorised — and that is
    exactly what made the panel impossible in a bundled app: the framed page can only be authorised by
    a cookie, and a bundled app's cookie is cross-site (SameSite=None → Secure → HTTPS), so against a
    .onion there was no way in at all.

    The page now arrives unauthenticated and is handed a bearer token by the client, so it MUST render
    for a visitor with no session. A human who lands here without one is not abandoned: the page's own
    gate shows a sign-in message with a link carrying the same ?next= (see admin-auth.js), which is
    more informative than the silent redirect it replaces. The `next` plumbing below is unchanged and
    still used by that link and by /login."""
    r = asyncio.run(admin_page(request=None, db=None, current_user=None))
    assert "location" not in r.headers, (
        "/admin redirected an anonymous visitor; the framed panel then never loads, so it can never "
        "receive the token that authorises it")
    assert getattr(r, "status_code", 200) == 200


def test_login_keeps_where_you_were_going():
    r = asyncio.run(login_page(request=None, next="/admin"))
    assert r.headers["location"] == "/client?next=%2Fadmin"


def test_login_refuses_to_forward_an_off_site_next():
    r = asyncio.run(login_page(request=None, next="//evil.com"))
    assert r.headers["location"] == "/client", "an off-site 'next' must be dropped, not forwarded"
