"""Every feature's user data must ride the sync — a feature missing from these lists restores a
member's account with a silent hole in it (measured: repo stars as kind-7 reactions, gitworkshop's
bookmark list, six repo announcements — each invisible until its kind joined).

The mapping below is the audit: FEATURE → the kinds it stores. A new feature adds its row here and
its kinds to backfill_author + the ingest default, or this fails with the feature's name."""
import re
import unittest

from app.services.nostr_relay import ingest


def _backfill_kinds():
    src = __import__("inspect").getsource(ingest.backfill_author)
    m = re.search(r"kinds = kinds or \[([0-9,\s]+)\]", src)
    return {int(k) for k in m.group(1).replace("\n", "").split(",") if k.strip()}


def _ingest_kinds():
    src = open("app/services/nostr_relay/thread.py", encoding="utf-8").read()
    m = re.search(r'nostr_relay_ingest_kinds", "([0-9,]+)"', src)
    return {int(k) for k in m.group(1).split(",")}


FEATURES = {
    "profile / follows / relay list": [0, 3, 10002],
    "posts, reposts, reactions (incl. repo stars), comments": [1, 6, 7, 1111],
    "deletions — an unstar or retraction made elsewhere": [5],
    "file metadata (webxdc attachments)": [1063],
    "polls": [1068],
    "mutes and pins": [10000, 10001],
    "search / DM / blossom server lists": [10007, 10050, 10063],
    "follow sets + legacy generic lists (still written by other clients)": [30000, 30001],
    "bookmarks + repo star lists": [10003, 30003],
    "articles": [30023],
    "live streams": [30311],
    "git repos": [30617],
    "torrents": [2003, 2004],
    # NIP-71 video. The removed Shorts screen READ these and published none of them; they are what
    # every other client's video posts arrive as, so they keep syncing. Divine's 34236 is the kind
    # that screen published and is NOT here — see RETIRED below.
    "short videos (NIP-71 — other clients' video posts)": [21, 22, 34235],
    "calendar events + RSVPs": [31922, 31923, 31924, 31925],
}

# THE OPPOSITE AUDIT, added 2026-09-04 when the owner retired these features on the relay itself:
# a kind the relay REFUSES must not be asked for. This file used to assert the reverse — the relay
# kept ingesting and serving listings, communities and shorts for every other client on it, on the
# reasoning that removing a screen is not a decision about other people's events. The owner decided
# otherwise ("make sure our relay no longer accepts events from the featurs we removed"), so the
# store now drops these on insert. A restore cannot restore what the store refuses to write, and a
# backfill that asks for them spends a member's budget fetching rows that die on arrival.
RETIRED = [40, 41, 42, 43, 44, 30402, 30403, 34236, 34550]


class SyncCoverage(unittest.TestCase):
    def test_every_feature_is_in_the_member_backfill(self):
        kinds = _backfill_kinds()
        for feature, ks in FEATURES.items():
            for k in ks:
                self.assertIn(k, kinds, f"{feature}: kind {k} missing from backfill_author — "
                                        f"a member restore silently loses it")

    def test_every_feature_is_in_the_ingest_default(self):
        kinds = _ingest_kinds()
        for feature, ks in FEATURES.items():
            for k in ks:
                self.assertIn(k, kinds, f"{feature}: kind {k} missing from ingest_kinds — "
                                        f"new writes made elsewhere never arrive")

    def test_no_feature_maps_to_a_kind_the_relay_now_refuses(self):
        """A row in FEATURES is a promise that the sync carries it. The retired kinds cannot be
        carried — store._insert_one drops them — so a row naming one would be a promise the relay
        breaks on every tick."""
        from app.services.nostr_relay.store import _RETIRED_KINDS
        self.assertEqual(sorted(_RETIRED_KINDS), sorted(RETIRED))
        for feature, ks in FEATURES.items():
            for k in ks:
                self.assertNotIn(k, RETIRED, f"{feature} names retired kind {k}")

    def test_the_retired_kinds_are_asked_for_nowhere(self):
        backfill, default = _backfill_kinds(), _ingest_kinds()
        for k in RETIRED:
            self.assertNotIn(k, backfill, f"backfill_author still asks for retired kind {k}")
            self.assertNotIn(k, default, f"ingest_kinds still asks for retired kind {k}")


if __name__ == "__main__":
    unittest.main()
