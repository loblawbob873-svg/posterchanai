"""A relay URL that is not ws(s): must be refused, not retried for ever.

`new WebSocket(x)` RESOLVES A RELATIVE STRING AGAINST THE PAGE. So a blank, half-typed or
path-shaped relay entry does not fail loudly — it opens a socket to the app's own address, which
serves no WebSocket, and the refusal is indistinguishable from a relay being down.

Measured in production: 35 rejected upgrades to `/client` in twenty minutes, one client retrying
behind a backoff against a URL that could never work, while the relay list showed nothing wrong.

The shipped predicate is extracted and run under node, rather than reimplemented here — a copy would
keep passing after the real one drifted.
"""
import json
import re
import shutil
import subprocess
import unittest
from pathlib import Path

RELAY = Path(__file__).resolve().parents[2] / "static" / "js" / "client" / "relay.js"

CASES = {
    # what a relay actually is
    "wss://relay.damus.io": True,
    "wss://relay.example.org/": True,
    "ws://127.0.0.1:3052": True,
    "WSS://Relay.Example.Org": True,          # scheme is case-insensitive
    # the shapes that resolved against the page and produced the 403 loop
    "": False,
    "   ": False,
    "/client": False,
    "relay.damus.io": False,                  # the commonest typo: no scheme
    "//relay.damus.io": False,                # protocol-relative: resolves to the PAGE's scheme
    ".": False,
    "https://relay.damus.io": False,          # right host, wrong protocol for a socket
    "http://relay.damus.io": False,
}


def _predicate():
    src = RELAY.read_text(encoding="utf-8")
    m = re.search(r"function _isRelayUrl\(u\)\{.*?\n  \}", src, re.S)
    if not m:
        raise AssertionError("_isRelayUrl is gone from relay.js; this test needs updating")
    return m.group(0)


class RelayUrlGuard(unittest.TestCase):
    def test_only_a_websocket_url_is_accepted(self):
        node = shutil.which("node")
        if not node:
            raise unittest.SkipTest("node is not installed")
        script = (_predicate() + "\nconst cases = " + json.dumps(list(CASES)) + ";\n"
                  "console.log(JSON.stringify(cases.map(_isRelayUrl)));")
        p = subprocess.run([node, "-e", script], capture_output=True, text=True, timeout=60)
        if p.returncode != 0:
            raise AssertionError("node failed: " + (p.stderr or "")[-2000:])
        got = json.loads(p.stdout)
        for (url, want), is_ok in zip(CASES.items(), got):
            with self.subTest(url=url):
                self.assertEqual(
                    is_ok, want,
                    f"{url!r} should be {'accepted' if want else 'refused'} — a refused URL that is "
                    f"accepted opens a socket against the page itself")

    def test_a_bad_url_is_not_retried(self):
        """Retrying one is how a typo becomes an endless stream of rejected connections that nobody
        can trace back to it."""
        src = RELAY.read_text(encoding="utf-8")
        body = src.split("_open(){", 1)[1].split("try { this.ws = new WebSocket", 1)[0]
        self.assertIn("_isRelayUrl", body, "_open does not check the URL before opening a socket")
        # COMMENTS OUT FIRST. The comment above that branch says "NOT `_retry()`", and matching prose
        # instead of code is how a check passes against the bug it describes — which this one did on
        # its first run, and which the i18n and terminal checks each did once before it.
        code = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
        code = re.sub(r"//[^\n]*", "", code)
        self.assertNotIn("_retry()", code,
                         "the malformed-URL branch must not schedule a retry: it cannot succeed")


if __name__ == "__main__":
    unittest.main()
