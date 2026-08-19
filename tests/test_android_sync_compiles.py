"""The Android folder-sync package must COMPILE, and until now nothing checked that it did.

Android only builds in CI, so on this box the Java half is invisible: `test_android_reconcile_parity`
compiles three files (SyncReconcile, SyncDiff, Json) and the one test that built NativeSweep —
`test_android_native_sweep` — has been failing to compile since the record-set rewrite, because its
fakes still implement the pre-rewrite interfaces. A broken test is indistinguishable from an absent
one: the phone-side sweep, which is the half that deletes files while the screen is off, had no
compile coverage at all while it was being changed.

This is the floor, not the ceiling: javac over the whole `sync` package against the stub SDK. It
cannot tell you the sweep is right — the parity test and the sims do that — but it fails in seconds
when a rule is edited in one engine and mistyped in the other, which is exactly how the two halves
drift apart.
"""
import glob
import os
import shutil
import subprocess
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
JAVA = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java")
SYNC = os.path.join(JAVA, "place", "poster", "app", "sync")
STUBS = os.path.join(ROOT, "tests", "androidstubs")
JAVAC = shutil.which("javac")


@unittest.skipIf(not JAVAC, "no javac on this node")
@unittest.skipIf(not os.path.isdir(SYNC), "no android sources here")
class AndroidSyncCompiles(unittest.TestCase):
    def test_the_whole_sync_package_compiles(self):
        src = sorted(glob.glob(os.path.join(SYNC, "*.java")))
        self.assertTrue(src, "no sources found — the path moved and this test stopped checking")
        with tempfile.TemporaryDirectory() as out:
            r = subprocess.run(
                [JAVAC, "-nowarn", "-d", out, "-sourcepath", STUBS + os.pathsep + JAVA] + src,
                capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr[-4000:])

    def test_the_native_sweep_is_among_them(self):
        """Named, because it is the file this test exists for: the sweep that runs with the screen
        off, decides deletions, and is the one nothing was compiling."""
        self.assertTrue(os.path.exists(os.path.join(SYNC, "NativeSweep.java")))


if __name__ == "__main__":
    unittest.main()
