"""The join announcement must be one message per room, and must survive a restart.

The bot introduces itself when it joins because joining publishes nothing and a silent member does
not exist — but an introduction that repeats is spam in a room people are trying to use. Three ways
that could go wrong, all of them found by inspection rather than by the room filling up:

  * the marker file was TRACKED BY GIT, so `sync.sh`'s `git commit -a` would re-commit it on every
    bot restart that added a key, and `git reset --hard origin/master` on the other nodes would
    overwrite each node's own marker with one node's copy. That is the `.nitter_seen.json` lesson in
    CLAUDE.md: a .gitignore line is load-bearing until the file it hides is actually deleted.
  * every bot on a node runs with cwd=botframework/, so they SHARE one marker file. Keyed only on
    the room, the first bot to announce marked it for all of them and every later bot stayed
    invisible for ever — the exact deadlock the announcement exists to break.
  * adding the npub to that key changes its shape, so a room already introduced in would be
    introduced again. A dedup key that stops honouring its own history announces everything twice.
"""
import re
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "botframework" / "concordListener.py").read_text()


class HelloOnce(unittest.TestCase):
    def test_the_marker_is_not_tracked_by_git(self):
        """A runtime state file in the repo churns the tree and ships one node's state to all."""
        out = subprocess.run(["git", "ls-files", "botframework/.concord_hello.json"],
                             cwd=ROOT, capture_output=True, text=True).stdout.strip()
        self.assertEqual(out, "",
                         "the hello marker is tracked — every restart dirties the tree and a "
                         "deploy overwrites every node's marker with this one")

    def test_the_marker_is_ignored(self):
        ignored = subprocess.run(["git", "check-ignore", "botframework/.concord_hello.json"],
                                 cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(ignored.returncode, 0,
                         "the hello marker is not gitignored, so the next `git add -A` re-adds it")

    def test_the_key_identifies_the_bot_as_well_as_the_room(self):
        """Bots share one file; without this only the first one in a room ever introduces itself."""
        m = re.search(r'key = "hello:%s:%s:%s" % \(([^)]*)\)', SRC)
        self.assertIsNotNone(m, "the hello key no longer identifies the bot")
        self.assertIn("npub", m.group(1))

    def test_the_previous_key_shape_is_still_honoured(self):
        """Changing the key without this re-announces in every room already done."""
        self.assertIn('legacy = "hello:%s:%s"', SRC,
                      "the pre-npub marker is ignored, so every room is introduced a second time")
        self.assertRegex(SRC, r"hello\.has\(key\)\s+or\s+hello\.has\(legacy\)",
                         "the legacy marker is computed but never consulted")

    def test_it_is_marked_only_when_a_relay_took_it(self):
        """Marking on the attempt turns one failed publish into silence for ever."""
        self.assertIn("if took is not None and not took:", SRC,
                      "the announcement is marked sent without evidence that it was")

    def test_an_operator_can_switch_it_off(self):
        self.assertIn("CONCORD_ANNOUNCE", SRC)
        self.assertRegex(SRC, r'if not _announce_enabled\(\):\s*\n\s*return',
                         "the off switch is not checked before announcing")


if __name__ == "__main__":
    unittest.main()
