"""A bookmark read nobody finished is not a bookmark that is gone (bookmarks_incomplete_read_runtime.mjs).

The real-node bookmark test failed "under load" in deploy after deploy with exactly this on screen: one of two
bookmarks listed after a reload, no toast, no error. renderBookmarks took a timed-out Relay.query ([] with
complete:false) as the final answer. It now asks once more, and names what is still missing with Try again.
"""
import json
import subprocess
import unittest
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent / "bookmarks_incomplete_read_runtime.mjs"


class BookmarksIncompleteRead(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["node", str(RUNTIME)], capture_output=True, text=True, timeout=120)
        if out.returncode:
            raise AssertionError("the bookmarks runtime failed: " + (out.stderr or "")[-2000:])
        cls.got = json.loads(out.stdout.strip().splitlines()[-1])

    def test_a_missed_read_is_asked_again_and_both_bookmarks_show(self):
        g = self.got["missedThenAnswered"]
        self.assertEqual(2, g["shown"], "a read no relay answered was taken as final: %r" % g)
        self.assertEqual(2, g["asked"])
        self.assertFalse(g["notice"])

    def test_posts_that_never_arrive_are_named_and_try_again_brings_them(self):
        g = self.got["neverAnswered"]
        self.assertEqual(1, g["shown"])
        self.assertTrue(g["notice"] and g["button"], "a bookmark is missing and nothing on screen says so: %r" % g)
        self.assertTrue(g["pressed"], "Try again (#bm-missing-retry) has no handler")
        self.assertEqual(2, g["afterRetry"], "pressing Try again did not bring the post back: %r" % g)
        self.assertFalse(g["noticeAfter"], "the missing-posts line stayed after everything loaded")

    def test_a_complete_empty_answer_is_final_and_said(self):
        g = self.got["answeredEmpty"]
        self.assertEqual(1, g["asked"], "the relays answered completely; asking again is pointless")
        self.assertEqual(1, g["shown"])
        self.assertTrue(g["notice"])


    def test_a_list_no_relay_answered_offers_try_again_and_it_asks_again(self):
        g = self.got["listNeverRead"]
        self.assertTrue(g["unreachable"] and g["button"], "no relay answered and the screen does not say so: %r" % g)
        self.assertTrue(g["pressed"], "Try again (#bm-retry) has no handler")
        self.assertGreaterEqual(g["asked"], 1, "pressing Try again did not ask for the list again: %r" % g)


if __name__ == "__main__":
    unittest.main()
