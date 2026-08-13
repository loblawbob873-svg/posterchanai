"""SearXNG, run natively instead of in a container.

Run: venv-unified/bin/python -m unittest tests.test_searxng_native

What is covered here is the wiring that fails SILENTLY — the reason the container went away is not
tested (it is a packaging choice), but every consequence of the move is:

- THE MOUNT IS NOT PUBLIC. The container was bound to 127.0.0.1; a mount on the app's own port
  inherits the app's public TLS, and this instance has its limiter disabled deliberately. Behind
  nginx the peer is 127.0.0.1 for internet traffic too, so a loopback-only check would open a
  metasearch proxy — one that makes arbitrary outbound requests with this node's IP — on every node
  that terminates TLS. This is the finding that costs the most if it is wrong, so it is first.
- IMPORTING SearXNG MUST NOT SILENCE THE APP. `searx/__init__.py` runs logging.basicConfig(WARNING)
  and logging.root.setLevel(WARNING) at import time. Unguarded, the first search a node ever makes
  turns off every INFO log the app emits, node-wide, and nothing says so.
- THE UNIT STILL WINS. `posterchanai-searxng.service` and the in-app mount are the same code; the
  probe has to keep choosing the running unit, or every node loads a second copy of a 200-engine
  catalogue into the app process for nothing.
- AND THE MOUNT IS THE FALLBACK. With no unit listening, resolution must reach the mount rather than
  falling through to a public instance, which is what it did before and which answers a server 429.
- "AVAILABLE" MEANS THE SETTINGS FILE TOO. `use_default_settings` ships the JSON API OFF, and with
  it off every search here is a 403 with an HTML body that each caller reads as "no results". A node
  with the package and no settings.yml is not a working instance and must not be advertised as one.
"""
import logging
import os
import unittest
from unittest import mock

from app.services import search_service as S
from app.services import searxng_native as N


def _scope(peer="127.0.0.1", headers=()):
    return {"type": "http", "client": (peer, 51234), "headers": list(headers)}


class GateTests(unittest.TestCase):
    """Who may reach /searxng."""

    def test_loopback_with_no_proxy_headers_is_allowed(self):
        self.assertTrue(N.request_is_local(_scope()))
        self.assertTrue(N.request_is_local(_scope("::1")))

    def test_a_forwarded_header_is_refused_even_from_loopback(self):
        """THE one that matters: nginx on the same box connects from 127.0.0.1, so without this every
        node with a reverse proxy in front of it serves SearXNG to the internet."""
        for header in (b"x-forwarded-for", b"x-real-ip", b"x-forwarded-host", b"forwarded"):
            self.assertFalse(N.request_is_local(_scope(headers=[(header, b"203.0.113.9")])),
                             f"{header!r} must not be served")

    def test_header_matching_is_case_insensitive(self):
        """HTTP/1.1 header names are case-insensitive and ASGI only lowercases them BY CONVENTION —
        a server that passes them through verbatim would walk straight past a `==` on the raw bytes."""
        self.assertFalse(N.request_is_local(_scope(headers=[(b"X-Forwarded-For", b"203.0.113.9")])))

    def test_a_remote_peer_is_refused(self):
        for peer in ("192.168.0.9", "10.0.0.4", "203.0.113.9", ""):
            self.assertFalse(N.request_is_local(_scope(peer)))

    def test_a_scope_with_no_client_is_refused(self):
        self.assertFalse(N.request_is_local({"type": "http", "headers": []}))


class AvailabilityTests(unittest.TestCase):
    def test_available_needs_the_package_AND_the_settings_file(self):
        with mock.patch.object(N, "find_spec", return_value=None), \
             mock.patch.object(N, "settings_path", return_value=_ExistingPath()):
            self.assertFalse(N.available())
        with mock.patch.object(N, "find_spec", return_value=object()), \
             mock.patch.object(N, "settings_path", return_value=_MissingPath()):
            self.assertFalse(N.available(), "no settings.yml means the JSON API is off — not usable")
        with mock.patch.object(N, "find_spec", return_value=object()), \
             mock.patch.object(N, "settings_path", return_value=_ExistingPath()):
            self.assertTrue(N.available())

    def test_mount_url_follows_the_app_port(self):
        with mock.patch.dict(os.environ, {"POSTERCHANAI_PORT": "3999"}):
            self.assertEqual(N.mount_url(), "http://127.0.0.1:3999/searxng")
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("POSTERCHANAI_PORT", None)
            self.assertEqual(N.mount_url(), "http://127.0.0.1:3051/searxng")

    def test_the_mount_path_is_what_the_url_says(self):
        """Two spellings of the same path is how the app ends up probing a URL it does not serve."""
        self.assertTrue(N.mount_url().endswith(N.MOUNT_PATH))


class ResolutionTests(unittest.TestCase):
    """The unit first, the mount second, a public instance last."""

    def setUp(self):
        S._local_probe.update({"ts": 0.0, "url": ""})

    def tearDown(self):
        S._local_probe.update({"ts": 0.0, "url": ""})

    def test_a_running_unit_is_preferred_over_our_own_mount(self):
        """Same code either way, so this is purely about not importing a 200-engine catalogue into
        the app process when a warm copy is already answering."""
        with mock.patch.object(S, "_is_searxng", side_effect=lambda b: b == "http://127.0.0.1:8899"), \
             mock.patch.object(N, "available", return_value=True) as avail:
            self.assertEqual(S.local_searxng_url(), "http://127.0.0.1:8899")
            avail.assert_not_called()

    def test_the_mount_answers_when_no_unit_is_listening(self):
        """Before this, a stopped unit meant falling through to a PUBLIC instance — which 429s a
        server — on a node perfectly capable of searching for itself."""
        with mock.patch.object(S, "_is_searxng", return_value=False), \
             mock.patch.object(N, "MOUNTED", True), \
             mock.patch.object(N, "mount_url", return_value="http://127.0.0.1:3051/searxng"):
            self.assertEqual(S.local_searxng_url(), "http://127.0.0.1:3051/searxng")

    def test_the_app_process_never_probes_itself(self):
        """It would be a request to ourselves, and it is asked during startup — before this server
        accepts connections — because that is when the bot manager builds every bot's environment."""
        with mock.patch.object(S, "_is_searxng", return_value=False) as probe, \
             mock.patch.object(N, "MOUNTED", True), \
             mock.patch.object(N, "mount_url", return_value="http://127.0.0.1:3051/searxng"):
            S.local_searxng_url()
            probed = [c.args[0] for c in probe.call_args_list]
            self.assertNotIn("http://127.0.0.1:3051/searxng", probed,
                             "the process that MOUNTED it must answer from the flag, not a request "
                             "to itself")

    def test_a_process_that_does_not_serve_the_mount_must_probe_it(self):
        """THE compose `split` case: the worker is its own container, so 127.0.0.1:3051 is itself and
        serves nothing. "SearXNG is importable in this venv" is not "something is serving it here",
        and answering from availability alone hands every news digest a URL that cannot answer —
        worse than the public fallback, which it then never gets to try."""
        with mock.patch.object(S, "_is_searxng", return_value=False), \
             mock.patch.object(N, "MOUNTED", False), \
             mock.patch.object(N, "available", return_value=True), \
             mock.patch.object(N, "mount_url", return_value="http://127.0.0.1:3051/searxng"):
            self.assertEqual(S.local_searxng_url(), "",
                             "an unanswered mount must not be adopted")

    def test_a_worker_beside_a_live_app_DOES_get_the_mount(self):
        """Bare metal, where the worker is a subprocess on the app's own host: the probe answers, so
        the worker searches through the app's mount rather than a public instance."""
        with mock.patch.object(S, "_is_searxng", side_effect=lambda b: b.endswith("/searxng")), \
             mock.patch.object(N, "MOUNTED", False), \
             mock.patch.object(N, "available", return_value=True), \
             mock.patch.object(N, "mount_url", return_value="http://127.0.0.1:3051/searxng"):
            self.assertEqual(S.local_searxng_url(), "http://127.0.0.1:3051/searxng")

    def test_nothing_bundled_still_falls_through_to_the_public_instance(self):
        with mock.patch.object(S, "_is_searxng", return_value=False), \
             mock.patch.object(N, "MOUNTED", False), \
             mock.patch.object(N, "available", return_value=False), \
             mock.patch.object(S, "search_enabled", return_value=True), \
             mock.patch.object(S.settings_store, "get", return_value=""):
            self.assertEqual(S.resolve_searxng_url(), S.DEFAULT_SEARXNG_URL)

    def test_the_mount_url_is_treated_as_LOCAL_by_the_transport(self):
        """It is this process. Sent through the Tor proxy it would 502 — Tor cannot route loopback —
        and `afallback_transport` does not retry a delivered response, so every search would fail."""
        self.assertTrue(S._is_local_base("http://127.0.0.1:3051/searxng"))

    def test_a_broken_searxng_import_never_breaks_resolution(self):
        with mock.patch.object(S, "_is_searxng", return_value=False), \
             mock.patch.object(N, "MOUNTED", False), \
             mock.patch.object(N, "available", side_effect=RuntimeError("boom")):
            self.assertEqual(S.local_searxng_url(), "")


@unittest.skipUnless(N.available(), "SearXNG (or its settings.yml) not installed on this node")
class ImportSideEffectTests(unittest.TestCase):
    """The one that is invisible in production.

    `searx/__init__.py` calls logging.basicConfig(level=WARNING) and logging.root.setLevel(WARNING)
    at import time. The app would keep working and simply stop logging anything below WARNING from
    the first search onward.
    """

    def test_importing_searxng_leaves_our_root_logger_alone(self):
        root = logging.getLogger()
        before_level, before_handlers = root.level, list(root.handlers)
        try:
            root.setLevel(logging.INFO)
            self.assertIsNotNone(N.wsgi_app(), "searx is installed, so this must build")
            self.assertEqual(root.level, logging.INFO,
                             "importing searx reset the root log level — the app just went quiet")
            self.assertEqual(list(root.handlers), before_handlers,
                             "importing searx replaced the root handlers")
        finally:
            root.setLevel(before_level)
            root.handlers[:] = before_handlers


class _ExistingPath:
    def is_file(self):
        return True


class _MissingPath:
    def is_file(self):
        return False


if __name__ == "__main__":
    unittest.main()
