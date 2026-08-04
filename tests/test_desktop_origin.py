"""desktop/origin.js — "is this URL ours?", the question main.js asks before it navigates, grants a
permission, or answers an IPC call.

This exists because getting it wrong broke four unrelated-looking things at once, and NONE of them
could be caught here any other way: Electron needs an X display this box does not have, and
check_desktop_standalone.py drives the bundle in headless Chrome over http://127.0.0.1 with the preload
STUBBED — so it never evaluates an app:// URL at all.

THE BUG: `new URL('app://posterchan/x').origin` is the string "null". WHATWG gives a tuple origin only
to the special schemes (http, https, ws, wss, ftp, file). Chromium's renderer knows better for a scheme
registered `standard: true`, so `location.origin` in the page is "app://posterchan" — but Node's URL in
the main process has no access to that registry. Comparing the two failed for every app:// URL, so:

  - will-navigate treated ordinary in-app navigation as off-site and called shell.openExternal() on an
    app:// URL. Windows answers "We can't open this app link" — which is what Logout did.
  - setWindowOpenHandler did the same to target=_blank links.
  - the permission handlers DENIED camera, mic, notifications, screen share and the save picker to our
    own page, so calls could not work.
  - every IPC handler refused the bundled client, so the instance picker and Tor controls did nothing.
"""
import json
import os
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ORIGIN_JS = os.path.join(ROOT, "desktop", "origin.js")

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

APP = "app://posterchan"


def call(fn, *args):
    src = (f"const m=require({json.dumps(ORIGIN_JS)});"
           f"process.stdout.write(JSON.stringify(m.{fn}(...{json.dumps(list(args))})));")
    r = subprocess.run(["node", "-e", src], capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


class TestOriginOf:
    def test_a_custom_scheme_gets_a_real_origin_not_the_string_null(self):
        """The whole bug in one assertion."""
        got = call("originOf", "app://posterchan/index.html")
        assert got == APP, (
            f"originOf returned {got!r}. If this is 'null' the app hands its own URLs to the OS, "
            "denies itself the camera and ignores its own IPC.")

    def test_special_schemes_still_use_the_parser(self):
        assert call("originOf", "https://poster.place/client") == "https://poster.place"
        assert call("originOf", "http://abc.onion:80/client") == "http://abc.onion"

    def test_authority_less_urls_stay_opaque(self):
        """file:, data: and friends have no authority. They must return '' rather than a value that
        could compare EQUAL to another opaque origin — two opaque origins are not the same origin."""
        for u in ("file:///etc/passwd", "data:text/html,hi", "about:blank", "not a url", ""):
            assert call("originOf", u) == "", u

    def test_the_port_is_part_of_the_origin(self):
        assert call("originOf", "https://host:8443/x") == "https://host:8443"
        assert call("originOf", "https://host:8443/x") != call("originOf", "https://host/x")


class TestIsOurs:
    def test_our_own_bundle_is_ours_with_and_without_an_instance(self):
        assert call("isOurs", "app://posterchan/index.html", APP, "https://poster.place") is True
        assert call("isOurs", "app://posterchan/", APP, "") is True, (
            "the bundle stopped being 'ours' in relays-only mode — every permission and IPC call "
            "from our own page would be refused")

    def test_the_configured_instance_is_ours(self):
        """The client frames <instance>/admin, so the instance's pages have to qualify."""
        assert call("isOurs", "https://poster.place/admin", APP, "https://poster.place") is True

    def test_a_third_party_is_never_ours(self):
        assert call("isOurs", "https://evil.example/x", APP, "https://poster.place") is False
        assert call("isOurs", "app://other/x", APP, "https://poster.place") is False

    def test_with_no_instance_only_the_bundle_qualifies(self):
        """Relays-only means there IS no trusted server, so a page from one must not inherit the
        camera, the screen or the IPC bridge just because it used to be configured."""
        assert call("isOurs", "https://poster.place/client", APP, "") is False

    def test_a_lookalike_host_is_not_ours(self):
        for u in ("https://poster.place.evil.com/x", "https://notposter.place/x",
                  "http://poster.place/x"):
            assert call("isOurs", u, APP, "https://poster.place") is False, u

    def test_garbage_is_not_ours(self):
        for u in ("", "not a url", "javascript:alert(1)", "file:///etc/passwd"):
            assert call("isOurs", u, APP, "https://poster.place") is False, u
