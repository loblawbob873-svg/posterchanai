"""`fetchEvent` ASKS RELAYS, AND A FEDIVERSE-ONLY POST IS ON NONE OF THEM.

The bridge now puts an `e` tag on a fediverse reply that answers one of this account's
Fediverse-only posts (tests/test_fedi_bridge_replies_to_a_private_post.py). That reference is only
worth having if the client can resolve it, and it cannot: the post was deliberately never published
to a relay, so `Relay.query` and the public-relay pool both correctly answer nothing.

`_loadFediOnlyHistory` pulls the most recent page at boot, which covers the TIMELINE. It does not
cover a thread opened cold -- from a notification, or from a pasted nevent link -- which is exactly
when somebody follows a reply back to the post it answers.

Order matters and is asserted: the archive is asked LAST, after every relay, so this can never
shadow a real answer. It is also the only route that can cost a request to this node, and a bundle
with no instance has no node to ask.
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
APP = (ROOT / "static/js/client/app.js").read_text(encoding="utf-8")


def _fn(name):
    at = APP.index(f"  async function {name}(")
    end = APP.index("\n  }", at)
    return re.sub(r"/\*.*?\*/", "", APP[at:end], flags=re.S)


class TestFetchEventFallsBack(unittest.TestCase):
    def setUp(self):
        self.body = _fn("fetchEvent")

    def test_it_asks_the_private_archive_at_all(self):
        self.assertIn("_fetchFediOnlyEvent(", self.body,
                      "fetchEvent cannot resolve a Fediverse-only parent, so the bridge's `e` tag "
                      "points at something the client will never find")

    def test_the_archive_is_asked_last(self):
        relay = self.body.index("Relay.query")
        public = self.body.index("fetchFromPublicRelays")
        private = self.body.index("_fetchFediOnlyEvent(")
        self.assertLess(relay, private)
        self.assertLess(public, private,
                        "the private archive must not be consulted before the relays, or it could "
                        "answer for an event the network actually has")


class TestTheLookupIsNarrow(unittest.TestCase):
    def setUp(self):
        self.body = _fn("_fetchFediOnlyEvent")

    def test_it_needs_an_instance_and_an_account(self):
        for guard in ("_standalone()", "GUEST", "ME"):
            self.assertIn(guard, self.body,
                          f"missing the {guard} guard: a bundle with no server has nothing to ask")

    def test_it_asks_for_one_event_not_the_archive(self):
        self.assertIn("private-events?ids=", self.body,
                      "paging the whole history to answer 'what is event X' is the wrong shape")
        self.assertIn("encodeURIComponent(id)", self.body)
        self.assertNotIn("limit=200", self.body)

    def test_it_verifies_what_came_back(self):
        """A server answer is not proof: check the id, the author and the fedi-only marker."""
        self.assertIn("x.id===id", self.body)
        self.assertIn("x.pubkey===pk", self.body)
        self.assertIn("_fediOnlyEvent(x)", self.body)

    def test_an_account_switch_mid_fetch_returns_nothing(self):
        self.assertIn("ME.pubkey!==pk", self.body,
                      "the account switcher can run inside this fetch; its result belongs to the "
                      "pubkey that asked")

    def test_what_it_finds_is_kept(self):
        self.assertIn("Store.saveEvent(ev)", self.body,
                      "without this the same lookup runs again on every repaint of the thread")


if __name__ == "__main__":
    unittest.main()
