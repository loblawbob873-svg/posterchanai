"""The automatic half of "sync my data": members' replaceable lists refresh on a schedule, gently.

A list kept only on the member's own relays (a star made in another client, a mute list from
another app) used to arrive only when somebody pressed a backfill button. The refresh must also be
POLITE — the public relays rate-limit by IP (damus already 503s us under load) — so its whole
budget is one unpaged query per user, at most 3 of their relays, at most 2 users per hourly tick,
stamped so a user is asked once a day however often the relay restarts."""
import asyncio
import inspect
import unittest
from unittest import mock

from app.services.nostr_relay import ingest


class _Store:
    def __init__(self, tags):
        self._tags = tags
    async def query(self, filters):
        return [{"kind": 10002, "tags": self._tags}] if self._tags is not None else []
    async def has_event(self, _id): return True
    async def add_event(self, ev, origin=None): return False


class _Server:
    class subs:
        @staticmethod
        def fanout(ev, send): pass
    _send = None


def _run(tags, upstream=("wss://up.example.com",)):
    asked = []
    async def fake_query(relays, filters, direct=False):
        asked.append((list(ingest._relay.normalize_relays(relays)), filters))
        return []
    with mock.patch.object(ingest._relay, "query", side_effect=fake_query):
        asyncio.run(ingest.refresh_member_lists(_Store(tags), _Server(), list(upstream), "ab" * 32))
    return asked


class RefreshBudget(unittest.TestCase):
    def test_one_unpaged_query_against_their_own_relays(self):
        asked = _run([["r", "wss://theirs.example.com/"]])
        self.assertEqual(len(asked), 1, "the refresh paged or fanned out — it must cost ONE query")
        relays, filters = asked[0]
        self.assertEqual(relays, ["wss://theirs.example.com"])
        self.assertNotIn("until", filters[0], "an `until` means paging — that's a backfill, not a refresh")
        for k in (10003, 30003, 10000, 10001):
            self.assertIn(k, filters[0]["kinds"])

    def test_their_relay_list_is_capped(self):
        asked = _run([["r", f"wss://r{i}.example.com"] for i in range(10)])
        self.assertLessEqual(len(asked[0][0]), 3)

    def test_upstream_and_onion_and_no_list_cost_nothing(self):
        self.assertEqual(_run([["r", "wss://up.example.com/"]]), [], "re-asked an upstream")
        self.assertEqual(_run([["r", "wss://x.onion/"]]), [])
        self.assertEqual(_run([]), [])
        self.assertEqual(_run(None), [])

    def test_content_kinds_stay_out(self):
        """Kind 1 in this filter turns the hourly tick into a timeline crawl."""
        for k in (1, 6, 7, 30023):
            self.assertNotIn(k, ingest._MEMBER_LIST_KINDS)


class TickDiscipline(unittest.TestCase):
    """The scheduler half, pinned at source: these are the rules that keep us off the rate limiters."""

    def _src(self):
        src = open("app/services/nostr_relay/thread.py", encoding="utf-8").read()
        a = src.index("async def _maybe_refresh_lists")
        return src[a:a + 2600]

    def test_two_users_per_tick_spaced_and_stamped_before_the_ask(self):
        seg = self._src()
        self.assertIn("if done >= 2:", seg, "the per-tick cap is gone — a big user table is a burst")
        self.assertIn("await asyncio.sleep(30)", seg, "users are not spaced out")
        self.assertIn("now - last < 86400", seg, "the daily stamp is gone")
        # stamp BEFORE the ask: a failing relay must not be re-asked every hour all day
        self.assertLess(seg.index("kv_set"), seg.index("refresh_member_lists("),
                        "the stamp is written after the ask — a failure retries hourly forever")


if __name__ == "__main__":
    unittest.main()
