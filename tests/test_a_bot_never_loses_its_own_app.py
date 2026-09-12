"""A revoked API key must not permanently silence a bot talking to its OWN app on loopback.

Measured on the running node: the bot's key is a real row in `api_keys` with `is_active = false`,
and the app answers

    with the bot's key : 401 Invalid API key
    with NO header     : 200 OK

because it accepts an unauthenticated call from loopback. But `ai/client.py` froze its headers at
IMPORT time and always sent `Authorization: Bearer <key>`, so the bot retried the same rejection
five times with backoff and went mute. Zero AI replies were generated all day, and nothing in the
bot's lifecycle touches that key — so disabling and re-enabling the bot could not fix it, which is
exactly what the operator expected to work.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "botframework/ai/client.py").read_text(encoding="utf-8")


def _fn(name):
    m = re.search(r"^def " + name + r"\(.*?\n(?=^\S|\Z)", SRC, re.S | re.M)
    assert m, "no function " + name
    return m.group(0)


class TheBotKeepsItsOwnApp(unittest.TestCase):

    def test_headers_are_built_per_request_not_frozen_at_import(self):
        """A module-level dict captures whatever the key was when python started, so a key changed
        or revoked afterwards can never be reflected."""
        self.assertIn("def _request_headers(", SRC)
        self.assertNotIn("headers=_headers,", SRC, "a call site still uses the frozen dict")
        self.assertIn("headers=_request_headers()", SRC)

    def test_an_empty_key_sends_no_authorization_header(self):
        """The app allows an unauthenticated call only when NO header is sent. Sending
        `Bearer ` (empty) is worse than sending nothing — it is a 401."""
        body = _fn("_request_headers")
        self.assertIn('h.pop("Authorization", None)', body)
        self.assertIn("OPENAI_API_KEY", body)

    def test_a_401_from_loopback_retries_without_the_key(self):
        """THE RULE. Our own app on 127.0.0.1 refusing our key is recoverable; retrying the same
        rejection five times is not."""
        self.assertIn("_is_local_endpoint(OPENAI_ENDPOINT)", SRC)
        self.assertIn("drop_auth=True", SRC)
        i = SRC.index("r.status_code == 401")
        self.assertIn("_is_local_endpoint", SRC[i:i + 120],
                      "the retry is not gated on the endpoint being local")

    def test_only_loopback_gets_that_treatment(self):
        """A REMOTE endpoint's 401 is a real refusal — stripping auth there would leak an
        unauthenticated request to somebody else's service."""
        body = _fn("_is_local_endpoint")
        for host in ("127.0.0.1", "localhost", "::1"):
            self.assertIn(host, body)
        self.assertIn("hostname", body, "it must compare the HOST, not match a substring of the url")

    def test_it_says_so_rather_than_degrading_silently(self):
        """A silently-degraded auth path is how this hid: the only evidence was a 401 in a log
        nobody reads."""
        self.assertIn("refused our API key", SRC)
        self.assertIn("Admin", SRC, "the message does not say where to fix it")


if __name__ == "__main__":
    unittest.main()
