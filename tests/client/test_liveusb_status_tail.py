"""System Settings → Installation media shows the newest output, not the oldest.

Reported as "the text area needs to show you the latest instead of being stuck on the top, looks bad
and like it's not working". That last clause is the important one: `.os-liveusb-status` is
`max-height:120px; overflow:auto` and it is handed the TAIL of the build log
(`output.slice(-5000)`), so it always HAD the newest lines — it just never scrolled to them. A <pre>
keeps its scroll position. A build printing steadily sat on whatever it said five thousand
characters ago, which from outside is indistinguishable from a job that has died.

Sticking to the bottom is only half of it. The other half is not stealing the scrollbar: reading an
error while a build runs must not be undone by the next poll, and this polls.
"""
import json
import subprocess
import unittest
from pathlib import Path

RUNTIME = Path(__file__).resolve().parent / "liveusb_tail_runtime.mjs"


class TheLogFollowsTheNewestLine(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        out = subprocess.run(["node", str(RUNTIME)], capture_output=True, text=True, timeout=120)
        if out.returncode:
            raise AssertionError("the tail runtime failed: " + (out.stderr or "")[-2000:])
        cls.got = json.loads(out.stdout.strip().splitlines()[-1])

    def test_a_long_log_scrolls_to_the_newest_line(self):
        self.assertTrue(self.got["atBottom"],
                        "the status box still shows the top of the log, so a running build looks "
                        "stuck on a line from thousands of characters ago")

    def test_scrolling_up_to_read_is_not_undone_by_the_next_poll(self):
        self.assertTrue(self.got["stayedPut"],
                        "the box scrolled itself back to the bottom while the reader was scrolled "
                        "up — an error you cannot finish reading is worse than one you cannot see")

    def test_it_follows_again_once_you_return_to_the_end(self):
        self.assertTrue(self.got["resumed"],
                        "after scrolling back to the end the box stopped following, so it goes "
                        "stale for the rest of the build")


if __name__ == "__main__":
    unittest.main()
