"""torrents.js attaches itself from a MutationObserver, so it must converge.

The Torrents view is rendered by app.js. Rather than editing a 40k-line file, torrents.js watches
`#feed` and inserts its own controls — a Feeds button in the tab strip and a Files button on every
torrent row. That design has exactly one way to go badly wrong and it is not subtle: every node it
inserts wakes the observer, so a single unguarded insert is an infinite loop. The view also repaints
every two seconds (app.js rebuilds those rows to move the progress bars), so the observer fires
constantly whether or not anyone touches anything.

The other rule this pins is that `#feed` is SHARED by every screen in this client. A module that
injects without first checking which view is up decorates Notes, Mail and the timeline as well.

Runs the SHIPPED file under node against a DOM stub that COUNTS mutations, so "it converged" is a
number rather than an impression.
"""
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
SRC = os.path.join(ROOT, "static", "js", "client", "torrents.js")
RUNNER = os.path.join(ROOT, "tests", "client", "torrents_attach_runtime.mjs")


class TorrentsAttachConverges(unittest.TestCase):
    def test_attach_is_idempotent_and_leaves_other_views_alone(self):
        node = shutil.which("node")
        if not node:
            self.skipTest("node is not installed")
        r = subprocess.run([node, RUNNER, SRC], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, (r.stdout + "\n" + r.stderr).strip())


if __name__ == "__main__":
    unittest.main()
