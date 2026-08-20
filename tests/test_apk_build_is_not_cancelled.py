"""THE APK BUILD MUST NOT BE CANCELLED BY THE NEXT PUSH.

Everything native in this app reaches its owner exactly one way: the GitHub APK build, published to
the rolling `apk-latest` release. `sync.sh` cannot carry any of it.

`concurrency.cancel-in-progress: true` on that workflow means every push kills the build before it,
so in a busy hour the only commit that produces an installable artifact is whichever happened to be
LAST — and it is nearly never the interesting one. Measured: the commit that fixed the launcher icons
(fc0dd762) was cancelled by the next push, the newest APK stayed at a commit from before the fix, and
the fix was reported as still broken by somebody testing a build that could not contain it. Two more
rounds went into that.

This is a one-line rule with an expensive failure and no other symptom, so it is asserted rather than
remembered. The emulator workflow has no concurrency group at all, which is also fine — it must never
grow `cancel-in-progress: true` either, for the same reason: its answer about a commit is the only
one anybody gets.
"""
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WF = os.path.join(ROOT, ".github", "workflows")

# Workflows whose OUTPUT is what somebody installs or reads about a specific commit. A cancelled run
# of one of these is a commit with no answer.
SHIPPING = ["android.yml", "android-emulator.yml"]


def _uncommented(text):
    return "\n".join(l for l in text.splitlines() if not l.lstrip().startswith("#"))


class ApkBuildFinishes(unittest.TestCase):

    def test_the_workflows_exist(self):
        """A renamed file turns every assertion below into a test of nothing."""
        for name in SHIPPING:
            self.assertTrue(os.path.exists(os.path.join(WF, name)), name + " is gone")

    def test_no_shipping_build_cancels_itself(self):
        for name in SHIPPING:
            body = _uncommented(open(os.path.join(WF, name), encoding="utf-8").read())
            for line in body.splitlines():
                if "cancel-in-progress" in line:
                    self.assertRegex(
                        line, r"cancel-in-progress:\s*false",
                        name + ": a queued push must WAIT, not kill the build that is running — "
                               "the APK for the commit somebody is waiting on is otherwise the one "
                               "that never gets built (see fc0dd762)")

    def test_the_apk_build_still_runs_on_a_push_that_touches_the_app(self):
        """The other way this could go quiet: a build that never cancels because it never starts."""
        body = open(os.path.join(WF, "android.yml"), encoding="utf-8").read()
        self.assertIn("- 'mobile/**'", body)
        self.assertRegex(body, r"on:\s*\n\s*push:")


if __name__ == "__main__":
    unittest.main()
