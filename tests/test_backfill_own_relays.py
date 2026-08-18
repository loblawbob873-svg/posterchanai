"""The backfill must follow the member's OWN write relays, not just the configured upstreams.

Measured on a real member (2026-08-18): six kind-30617 repo announcements and the gitworkshop star
list (30003 d:'git-repo-bookmark') lived on nostr21.com — named in their kind-10002, absent from
every configured upstream. The backfill reported success (557 events) and the member kept asking
where their repos were, because a backfill that only asks OUR relays can only return what our
relays happen to hold. The fix is a third pass: after the public pass stores their 10002, ask the
clearnet wss relays it names (minus the ones already asked, capped) with the same authored filter.

These tests RUN the shipped backfill_author with the relay layer stubbed."""
import asyncio
import unittest
from unittest import mock

from app.services.nostr_relay import ingest


class _Store:
    def __init__(self, ten02_tags):
        self._tags = ten02_tags
    async def query(self, filters):
        if filters and filters[0].get("kinds") == [10002]:
            return [{"kind": 10002, "tags": self._tags}]
        return []
    async def has_event(self, _id):
        return True   # nothing new gets stored; we only care WHO was asked
    async def add_event(self, ev, origin=None):
        return False


class _Server:
    class subs:
        @staticmethod
        def fanout(ev, send):
            pass
    _send = None


def _run(ten02_tags, upstream):
    """Run backfill_author, recording every relay set _relay.query was given."""
    asked = []

    async def fake_query(relays, filters, direct=False):
        asked.append(list(ingest._relay.normalize_relays(relays)))
        return []   # empty page ends each pass after one query

    with mock.patch.object(ingest._relay, "query", side_effect=fake_query):
        asyncio.get_event_loop_policy().new_event_loop()
        asyncio.run(ingest.backfill_author(
            _Store(ten02_tags), _Server(), upstream, "ab" * 32, pace=0))
    return asked


class OwnRelayPass(unittest.TestCase):
    UP = ["wss://relay.damus.io", "wss://nos.lol"]

    def test_the_members_relays_are_asked_with_the_authored_kinds(self):
        asked = _run([["r", "wss://nostr21.com/"], ["r", "wss://relay.damus.io/"]], self.UP)
        flat = [u for call in asked for u in call]
        self.assertIn("wss://nostr21.com", flat,
                      "the relay named in the member's 10002 was never asked")

    def test_relays_already_in_the_upstream_set_are_not_asked_twice(self):
        asked = _run([["r", "wss://relay.damus.io/"]], self.UP)
        # every call that contains damus must be an upstream call (i.e. contains nos.lol too)
        for call in asked:
            if "wss://relay.damus.io" in call:
                self.assertIn("wss://nos.lol", call,
                               "a duplicate own-relay pass re-asked an upstream")

    def test_onion_and_plain_ws_relays_are_skipped(self):
        asked = _run([["r", "wss://abcdef.onion/"], ["r", "ws://192.168.0.5:3052"]], self.UP)
        flat = [u for call in asked for u in call]
        self.assertNotIn("wss://abcdef.onion", flat)
        self.assertNotIn("ws://192.168.0.5:3052", flat)

    def test_the_member_cannot_fan_us_out_to_unbounded_relays(self):
        tags = [["r", f"wss://r{i}.example.com"] for i in range(50)]
        asked = _run(tags, self.UP)
        own = [call for call in asked if call and ".example.com" in call[0]]
        self.assertTrue(own, "the own-relay pass never ran")
        distinct = {u for call in own for u in call}
        self.assertLessEqual(len(distinct), 4, "a member-authored 10002 fanned out unbounded")

    def test_a_member_with_no_relay_list_costs_nothing_and_breaks_nothing(self):
        asked = _run([], self.UP)
        self.assertTrue(asked, "the ordinary passes did not run")
        for call in asked:
            self.assertEqual(call, ingest._relay.normalize_relays(self.UP))


if __name__ == "__main__":
    unittest.main()


class SparseRelayPaging(unittest.TestCase):
    """The member's relays are paged ONE AT A TIME. _backfill_filter sets the next `until` to the
    minimum created_at of the whole page, so a merged page lets one sparse relay (five events from
    2024) drag `until` past two years of the dense relay's history — measured as a January star
    list that never arrived while the recent repos did."""

    def test_a_sparse_relay_cannot_skip_the_dense_relays_history(self):
        PK = "ab" * 32
        dense_asks = []

        async def fake_query(relays, filters, direct=False):
            urls = ingest._relay.normalize_relays(relays)
            f = filters[0]
            until = f.get("until", 10**12)
            out = []
            if "wss://dense.example.com" in urls:
                dense_asks.append(until)
                # dense history: events at 1000, 999, … down to 500, two per page
                top = min(until, 1000)
                out += [{"pubkey": PK, "created_at": t, "id": "x" * 64}
                        for t in range(top, max(top - 2, 499), -1) if t >= 500]
            if "wss://sparse.example.com" in urls:
                if until >= 100:
                    out += [{"pubkey": PK, "created_at": 100, "id": "y" * 64}]
            return out

        tags = [["r", "wss://dense.example.com"], ["r", "wss://sparse.example.com"]]
        with mock.patch.object(ingest._relay, "query", side_effect=fake_query):
            asyncio.run(ingest.backfill_author(
                _Store(tags), _Server(), ["wss://up.example.com"], PK,
                pace=0, max_pages=20))
        walked = [u for u in dense_asks if 500 < u < 995]
        self.assertTrue(walked,
                        "the dense relay was never asked for its mid-history — a sparse relay "
                        "in the same merged page dragged `until` past it")
