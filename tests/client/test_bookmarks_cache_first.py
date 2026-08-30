"""Bookmarks must paint what is already held before it asks the relay for anything.

Reported as "Bookmarks is taking forever to load, circle black screen". The view blanked the feed to
a spinner, awaited `Relay.query` for any bookmarked id the Store did not hold, and painted at the
very bottom — so one id nobody could fetch hid every bookmark that was right there. And the REQ went
out without `Relay.ready()`: a REQ written to a CONNECTING socket is dropped in silence, which is
the black screen that never resolves.

This runs the SHIPPED renderBookmarks against a relay that never answers.
"""
import json
import subprocess
import unittest
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent / "bookmarks_cache_first_runtime.mjs"


class BookmarksPaintFromCacheFirst(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["node", str(RUNTIME)], capture_output=True, text=True, timeout=120)
        if out.returncode:
            raise AssertionError("the bookmarks runtime failed: " + (out.stderr or "")[-2000:])
        cls.got = json.loads(out.stdout.strip().splitlines()[-1])

    def test_held_bookmarks_are_on_screen_while_the_relay_never_answers(self):
        self.assertEqual(2, self.got["paintedWhileRelayHangs"],
                         "the relay was hanging and the feed showed %d post(s) — the Store held two "
                         "of these bookmarks the whole time. This is the black screen."
                         % self.got["paintedWhileRelayHangs"])
        self.assertFalse(self.got["isSpinner"],
                         "the view is still showing a spinner over posts it already has")

    def test_the_relay_is_asked_only_after_it_can_answer(self):
        self.assertTrue(self.got["readyCalled"],
                        "renderBookmarks queries without Relay.ready(); a REQ to a CONNECTING "
                        "socket is dropped in silence and nothing ever repaints")
        self.assertTrue(self.got["readyBeforeQuery"],
                        "Relay.ready() is awaited AFTER the query, which is the same as not at all")


if __name__ == "__main__":
    unittest.main()
