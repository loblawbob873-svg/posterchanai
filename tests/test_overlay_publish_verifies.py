"""Publishing the overlay must PROVE the URL works, not print the command that would.

Reported from an installed machine:

    fatal: repository 'https://gentoo.poster.place/posterchan-overlay.git/' not found
    Could not reach the overlay — trying the update with what is already here.

That machine could not update at all, and `check_gentoo_overlay.py` had passed green hours earlier.
Both were true: nas mirrors a Gentoo distfiles tree into /raid/distfiles with `rsync --delete` at
06:00 daily, the overlay lives at /raid/distfiles/distfiles/posterchan-overlay.git, and upstream has
never heard of that directory — so the mirror deleted it every morning. Publishing put it back;
06:00 removed it again.

The mirror excludes it now. This test is about the OTHER half: publish_overlay.sh ended by telling
the operator how to verify, which is not verifying. Every failure that file already documents — a
mkdir that hit EACCES, a dangling HEAD, a missing update-server-info — has exactly the same shape:
the publish looks perfect from the machine that ran it and the URL is broken for everybody else. A
publish that cannot prove its own result will hide the next cause just as well as it hid this one.
"""
import re
import unittest
from pathlib import Path

SCRIPT = (Path(__file__).resolve().parents[1] / "scripts" / "publish_overlay.sh").read_text(
    encoding="utf-8")


class ThePublishChecksItself(unittest.TestCase):
    def test_it_asks_the_public_url_whether_the_repository_is_there(self):
        self.assertRegex(SCRIPT, r'git ls-remote "\$URL"',
                         "publish_overlay.sh never contacts the URL it just published to")

    def test_an_unreachable_publish_is_a_failure_not_a_notice(self):
        tail = SCRIPT[SCRIPT.index("git ls-remote \"$URL\""):]
        self.assertIn("exit 1", tail,
                      "an unreachable overlay exits 0, so sync.sh reports a green deploy while "
                      "every installed machine has lost its update path")
        self.assertIn("cannot 'emerge --sync'", tail,
                      "the failure must say what it costs the person on the other end")

    def test_it_does_not_merely_print_the_verification_command(self):
        self.assertNotIn("verify with:", SCRIPT,
                         "telling an operator how to check is not checking — that is what let a "
                         "deleted overlay stand for a day")

    def test_a_host_without_git_says_it_could_not_verify(self):
        """Silence and success must not look the same on a machine that cannot run the check."""
        self.assertIn("was NOT verified", SCRIPT)


if __name__ == "__main__":
    unittest.main()
