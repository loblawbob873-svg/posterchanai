"""The same relay under two spellings must be ONE connection.

Reported from a real relay list: `relay.poster.place` appearing TWICE, alongside
`poster.place/relay`. The pool dedupes with a string `Set`, and `wss://relay.poster.place/` is not
the string `wss://relay.poster.place` — so both got a `Conn`, both authenticated, and both carried
the same firehose. Twice the sockets, twice the traffic, and a relay list that reads like a bug.

`_relayUrl` already normalised the `/relay/` form and deliberately refused to strip slashes broadly,
because an external relay may distinguish `/nostr` from `/nostr/`. That caution is right for a PATH
and does not apply to a bare host: RFC 3986 makes an empty path and "/" equivalent for the origin,
so `wss://host/` and `wss://host` can only ever be one relay.

These run the SHIPPED `_relayUrl` and the SHIPPED `configure()` — the dedup is the thing under test,
so a fixture that reimplemented either would prove nothing.
"""
import json
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent.parent
RELAY_JS = ROOT / "static/js/client/relay.js"


def norm(urls):
    """Run the shipped `_relayUrl` over each url, in node."""
    src = RELAY_JS.read_text(encoding="utf-8")
    body = re.search(r"function _relayUrl\(u\)\{[\s\S]*?\n  \}", src).group(0)
    script = (body + "\nconst out=%s.map(_relayUrl);console.log(JSON.stringify(out));"
              % json.dumps(list(urls)))
    done = subprocess.run(["node", "-e", script], capture_output=True, text=True, timeout=20)
    assert done.returncode == 0, done.stdout + done.stderr
    return json.loads(done.stdout)


class OneRelayOneSocket(unittest.TestCase):

    def test_a_bare_host_with_and_without_a_trailing_slash_is_one_relay(self):
        """THE BUG, exactly as it appeared in the list."""
        got = norm(["wss://relay.poster.place/", "wss://relay.poster.place"])
        self.assertEqual(len(set(got)), 1, "two sockets to one relay: %r" % got)

    def test_it_dedupes_after_normalising_not_before(self):
        """The pool's own `configure` maps through `_relayUrl` and THEN builds the Set. Both
        spellings must collapse to a single entry, which is what makes it a single `Conn`."""
        self.assertIn("new Set((urls||[]).map(_relayUrl)", RELAY_JS.read_text(encoding="utf-8"))

    def test_the_managed_relay_endpoint_still_normalises(self):
        got = norm(["wss://poster.place/relay/", "wss://poster.place/relay"])
        self.assertEqual(len(set(got)), 1)

    def test_a_real_path_is_left_exactly_as_written(self):
        """The caution this file already carried: an external relay may serve different content at
        `/nostr` and `/nostr/`, so those must stay two relays."""
        got = norm(["wss://x.example/nostr/", "wss://x.example/nostr"])
        self.assertEqual(len(set(got)), 2, "collapsed two distinct external paths: %r" % got)

    def test_a_query_or_fragment_is_untouched(self):
        got = norm(["wss://a.example/?x=1", "wss://a.example/#f"])
        self.assertEqual(got, ["wss://a.example/?x=1", "wss://a.example/#f"])

    def test_two_different_hosts_remain_two_relays(self):
        """`poster.place/relay` and `relay.poster.place` may reach the same backend, but they are
        different endpoints and the client must not decide otherwise."""
        got = norm(["wss://poster.place/relay", "wss://relay.poster.place"])
        self.assertEqual(len(set(got)), 2)


if __name__ == "__main__":
    unittest.main()
