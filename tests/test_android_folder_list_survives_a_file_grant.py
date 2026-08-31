"""Opening Folder Sync ended the app process, and it was never a missing try/catch.

THE REAL TRACE, pulled from the phone over adb after four rounds of guessing:

    java.lang.IllegalArgumentException: Invalid URI:
      content://com.android.providers.media.documents/document/video%3A150963
        at android.provider.DocumentsContract.getTreeDocumentId(DocumentsContract.java:1365)
        at place.poster.app.sync.FolderSyncPlugin.prettyName(FolderSyncPlugin.java:1256)
        at place.poster.app.sync.FolderSyncPlugin.list(FolderSyncPlugin.java:652)

`list()` walks every persisted read permission the phone holds and calls `getTreeDocumentId` on
each. `getPersistedUriPermissions()` returns EVERY persistable grant this app has ever taken — and
a single file chosen through the document picker is one of them. One video was enough:
`getTreeDocumentId` throws for a URI that is not a tree, Capacitor's `Bridge.callPluginMethod`
re-threw it as a RuntimeException on the CapacitorPlugins HandlerThread where nothing catches it,
and Android ended the process. "Folder Sync just crashes the app and returns you to desktop."

WHY NOTHING SAW IT. It is phone-specific: it needs a persisted grant on something that is not a
folder, i.e. somebody who has used the app to pick a file. A fresh emulator has none, so every
device check — including the one written for this screen the same day — passed on a phone that
could not reproduce it. The `androidstubs` used by the javac tests returned null from
`getTreeDocumentId` instead of throwing, so the bug compiled and passed there too. Both are fixed:
the stub now throws exactly as the platform does, and this test RUNS the shipped rule against the
real URI from the trace.
"""
import glob
import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SYNC = ROOT / "mobile/android/app/src/main/java/place/poster/app/sync"
STUBS = ROOT / "tests" / "androidstubs"
JAVA = ROOT / "mobile/android/app/src/main/java"
JAVAC = shutil.which("javac")


class ThePersistedGrantListIsNotAllFolders(unittest.TestCase):
    THE_VIDEO = "content://com.android.providers.media.documents/document/video%3A150963"
    A_FOLDER = "content://com.android.externalstorage.documents/tree/primary%3ADocuments"

    @unittest.skipIf(not JAVAC, "no javac on this node")
    def test_the_rule_runs_against_the_uri_from_the_real_crash(self):
        """Not a text match — the shipped `isSyncableTree` is compiled and CALLED, with a stub whose
        `getTreeDocumentId` throws the way the platform's does."""
        with tempfile.TemporaryDirectory() as out:
            # Same package, so the rule stays package-private in the app: a method made public
            # only for a test is a wider surface than the test is worth.
            pkg = Path(out) / "src" / "place" / "poster" / "app" / "sync"
            pkg.mkdir(parents=True, exist_ok=True)
            harness = pkg / "Probe.java"
            harness.write_text(
                "package place.poster.app.sync;\n"
                "public class Probe {\n"
                "  public static void main(String[] a) {\n"
                "    System.out.println(\"video=\" + FolderSyncPlugin.isSyncableTree("
                "android.net.Uri.parse(\"%s\")));\n"
                "    System.out.println(\"folder=\" + FolderSyncPlugin.isSyncableTree("
                "android.net.Uri.parse(\"%s\")));\n"
                "    System.out.println(\"null=\" + FolderSyncPlugin.isSyncableTree(null));\n"
                "  }\n}\n" % (self.THE_VIDEO, self.A_FOLDER), encoding="utf-8")
            src = sorted(glob.glob(str(SYNC / "*.java"))) + [str(harness)]
            c = subprocess.run([JAVAC, "-nowarn", "-proc:none", "-d", out,
                                "-sourcepath", str(STUBS) + os.pathsep + str(JAVA)] + src,
                               capture_output=True, text=True, timeout=300)
            self.assertEqual(c.returncode, 0, c.stderr[-3000:])
            r = subprocess.run(["java", "-cp", out, "place.poster.app.sync.Probe"], capture_output=True, text=True, timeout=60)
            self.assertEqual(r.returncode, 0, r.stderr[-2000:])
        got = dict(line.split("=") for line in r.stdout.split())
        self.assertEqual(got["video"], "false",
                         "the picked-video grant from the real crash is still treated as a folder")
        self.assertEqual(got["folder"], "true", "a real folder grant must still be listed")
        self.assertEqual(got["null"], "false")

    def test_list_skips_a_grant_that_is_not_a_folder(self):
        src = (SYNC / "FolderSyncPlugin.java").read_text(encoding="utf-8")
        body = src[src.index("public void list(PluginCall call)"):]
        body = body[:body.index("call.resolve(ret)")]
        self.assertIn("isSyncableTree(p.getUri())", body,
                      "list() still calls tree APIs on every persisted grant")
        skip = body.index("isSyncableTree(p.getUri())")
        use = body.index("prettyName(p.getUri())")
        self.assertLess(skip, use, "the check must come BEFORE the tree call, not after it")

    def test_one_bad_grant_costs_a_row_and_never_the_list(self):
        """That screen is the only way to reach every synced folder on the device."""
        src = (SYNC / "FolderSyncPlugin.java").read_text(encoding="utf-8")
        body = src[src.index("public void list(PluginCall call)"):]
        body = body[:body.index("call.resolve(ret)")]
        self.assertIn("catch (Throwable", body)

    def test_pretty_name_never_calls_the_throwing_api_unguarded(self):
        src = (SYNC / "FolderSyncPlugin.java").read_text(encoding="utf-8")
        fn = src[src.index("private String prettyName(Uri tree)"):]
        fn = fn[:fn.index("\n  }") + 4]
        i = fn.index("getTreeDocumentId")
        before = fn[:i]
        self.assertIn("isSyncableTree(tree)", before)
        self.assertIn("try {", before)

    def test_the_background_worker_filters_the_same_list(self):
        """SyncCheckWorker walks the same grants. It caught the throw, so it did not crash — it
        just stopped checking, silently, on the first picked file."""
        src = (SYNC / "SyncCheckWorker.java").read_text(encoding="utf-8")
        self.assertEqual(src.count("isSyncableTree(up.getUri())"), 2,
                         "both persisted-permission loops must filter to folders")


if __name__ == "__main__":
    unittest.main()
