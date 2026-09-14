"""THE "DID IT COME UP" CHECK MUST WAIT, AND THE "DID IT SURVIVE" CHECK MUST NOT.

`scripts/android_device_checks.sh` asked `adb shell pidof` ONE TIME, 23 seconds after launch, and
treated a miss as a failure. On a loaded CI emulator that is a race, and it produced a verdict that
contradicted itself inside a single run:

    FAIL: the app is not running after launch      <- near the top
    ok: still running                              <- at the very bottom, same run
    Tests 117/117 completed. (0 skipped) (0 failed) <- the instrumented suite, green

The whole Android gate went red on a commit that touched none of it. That matters beyond one red
tick: this gate has already spent eight runs red for an unrelated reason, and a gate that cries
wolf is one people stop reading — at which point it protects nothing.

The fix is not to soften the check, it is to ask the right question. "Did it COME UP" is honestly
answered by a bounded wait: an app that never starts still fails, 30s later. "Did it SURVIVE the
cycle" is honestly answered by a single sample, because there a miss at that instant IS the bug —
so that one deliberately keeps its single `pidof` and this test pins that asymmetry too.

Both halves RUN the shipped shell, against a stubbed `adb`, so this cannot drift from the script.
"""
import os
import re
import shutil
import subprocess
import tempfile
import unittest

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT = os.path.join(REPO, "scripts", "android_device_checks.sh")


def _launch_block():
    """The shipped wait, lifted between its own markers."""
    src = open(SCRIPT).read()
    start = src.index('say "the process is actually up"')
    end = src.index('say "background it', start)
    block = src[start:end]
    assert "pidof" in block, "the launch check no longer asks pidof — this test is pinned to old code"
    return block


def _run(block, succeed_on):
    """Run the block with a fake `adb` that reports the app up only from the Nth call on.

    succeed_on = 0 means it never comes up.
    """
    d = tempfile.mkdtemp(prefix="pc-launchwait-")
    try:
        counter = os.path.join(d, "n")
        with open(os.path.join(d, "adb"), "w") as fh:
            fh.write(
                "#!/bin/bash\n"
                "n=$(( $(cat %s 2>/dev/null || echo 0) + 1 )); echo $n > %s\n"
                "[ %d -ne 0 ] && [ $n -ge %d ] && exit 0\n"
                "exit 1\n" % (counter, counter, succeed_on, succeed_on or 1))
        os.chmod(os.path.join(d, "adb"), 0o755)
        runner = os.path.join(d, "run.sh")
        with open(runner, "w") as fh:
            fh.write("set -uo pipefail\nPKG=place.poster.app\nFAILED=0\n"
                     "say(){ printf '\\n=== %s\\n' \"$*\"; }\n"
                     "fail(){ printf '\\nFAIL: %s\\n' \"$*\"; FAILED=1; }\n"
                     "ok(){ printf 'ok: %s\\n' \"$*\"; }\n"
                     "sleep(){ :; }\n"          # the wait is real; the waiting is not
                     + block + "\nexit $FAILED\n")
        env = dict(os.environ, PATH=d + os.pathsep + os.environ["PATH"])
        r = subprocess.run(["bash", runner], capture_output=True, text=True,
                           timeout=120, env=env)
        return r.returncode, r.stdout
    finally:
        shutil.rmtree(d, ignore_errors=True)


class LaunchCheckWaits(unittest.TestCase):
    def test_a_slow_start_is_not_a_failure(self):
        code, out = _run(_launch_block(), succeed_on=4)
        self.assertIn("ok: running", out,
                      "an app that came up on the 4th poll was reported as never running:\n" + out)
        self.assertEqual(code, 0, "a slow start failed the gate:\n" + out)

    def test_an_app_that_never_starts_still_fails(self):
        code, out = _run(_launch_block(), succeed_on=0)
        self.assertIn("FAIL", out,
                      "an app that NEVER started was reported as running — the wait was turned "
                      "into a rubber stamp:\n" + out)
        self.assertNotEqual(code, 0, "a dead app did not fail the gate:\n" + out)

    def test_the_survival_check_keeps_its_single_sample(self):
        """The opposite rule, and it is the reason this is safe to relax at launch."""
        src = open(SCRIPT).read()
        line = next((l for l in src.splitlines() if "still running" in l and "pidof" in l), None)
        self.assertIsNotNone(line, "the end-of-cycle survival check is gone")
        self.assertNotIn("for ", line,
                         "the survival check has been given a retry loop too. There a miss IS the "
                         "failure — retrying until it comes back would hide an app that died and "
                         "was restarted:\n    " + line.strip())


if __name__ == "__main__":
    unittest.main()
