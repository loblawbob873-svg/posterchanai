"""A dead emulator and a failing test must not look the same, and they did.

The Android emulator job has now gone red three times in one evening for three different reasons:
a real test failure (a notification race), an emulator that died before any test ran, and a device
script that hit its 15-minute timeout (`device=124`) leaving nothing attached. All three arrived as
an identical red build with `No connected devices!`, and a signal that is red for infrastructure is
one people learn to re-run without reading — which is precisely how the real failure underneath gets
missed.

So `android_instrumented.sh` answers three things, the way checkall already does locally:

  0  the device ran the tests and they passed
  1  the device ran them and something failed
  2  THE TESTS DID NOT RUN

Exit 2 is never reported as a pass. It is a run annotation plus a line in the job summary saying in
words that nothing was verified on a device, and only two shapes qualify — both about the DEVICE,
never about a test: gradle's own "No connected devices!", and an emulator that is no longer attached
once the run is over.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = (ROOT / "scripts" / "android_instrumented.sh").read_text(encoding="utf-8")
WORKFLOW = (ROOT / ".github" / "workflows" / "android-emulator.yml").read_text(encoding="utf-8")


class DidNotRunIsItsOwnAnswer(unittest.TestCase):
    def test_the_script_can_say_it_did_not_run(self):
        self.assertIn("exit 2", SCRIPT,
                      "the instrumented script has no 'did not run' answer, so a dead emulator is "
                      "indistinguishable from a failing test")
        self.assertIn("device_present()", SCRIPT, "nothing checks whether a device is attached")

    def test_it_asks_before_paying_for_a_build(self):
        """No device attached means the emulator never booted or has already gone; every second
        spent building after that is spent for nothing."""
        body = SCRIPT[SCRIPT.index("cd mobile/android"):]
        self.assertRegex(body.split("./gradlew")[0], r"device_present \|\| skip",
                         "the device check runs after gradle, which wastes the whole build")

    def test_only_device_shapes_count_as_did_not_run(self):
        """A test failure must never be laundered into a skip."""
        self.assertIn("No connected devices", SCRIPT)
        self.assertRegex(SCRIPT, r"device_present \|\| skip .*disappeared",
                         "an emulator that vanished mid-run is not a verdict on the code")
        self.assertNotIn("skip \"tests failed", SCRIPT)

    def test_a_skip_is_announced_and_not_silent(self):
        self.assertIn("::warning title=Instrumented tests DID NOT RUN::", SCRIPT)
        self.assertIn("GITHUB_STEP_SUMMARY", SCRIPT,
                      "a skip that only appears in a log is a skip nobody sees")
        self.assertIn("nothing was verified on a device", SCRIPT,
                      "the skip must say what was NOT done, in words")

    def test_the_workflow_treats_2_as_did_not_run_rather_than_success(self):
        self.assertIn('case "$a:$b" in', WORKFLOW,
                      "the workflow still collapses every exit code into pass/fail")
        self.assertIn("DID NOT RUN", WORKFLOW,
                      "a build whose device checks never ran must say so on the run itself")

    def test_a_timeout_is_still_a_failure_not_a_skip(self):
        """`device=124` is `timeout` killing the device script after fifteen minutes, and it is
        AMBIGUOUS: the app may be hanging. My first version of the workflow case matched `*:2`,
        which turned exactly that into a green build. Only 0 and 2 may combine into "ran clean or
        did not run"; anything else stays red."""
        self.assertIn("0:0) exit 0;; 0:2|2:0|2:2)", WORKFLOW,
                      "the case arm is broad enough to swallow a timeout or a crash as a skip")
        self.assertNotIn("*:2|2:*", WORKFLOW,
                         "`*:2` matches a 124 timeout in the other half and reports success")

    def test_the_existing_timeout_contract_is_untouched(self):
        """test_android_emulator_timeouts.py pins these; keep them true here too."""
        self.assertIn("timeout --kill-after=30s 25m bash scripts/android_instrumented.sh; b=$?",
                      WORKFLOW)
        self.assertIn('echo "device=$a instrumented=$b"', WORKFLOW)


if __name__ == "__main__":
    unittest.main()
