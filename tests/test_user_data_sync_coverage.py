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
    "listings": [30402],
    "git repos": [30617],
    "communities": [34550],
    "torrents": [2003, 2004],
    "calendar events + RSVPs": [31922, 31923, 31924, 31925],
}


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


if __name__ == "__main__":
    unittest.main()
