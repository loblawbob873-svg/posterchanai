"""Files' "This phone": the Android storage bridge, RUN against a real directory.

Asked 2026-10-28: "android: no way to browse user's android storage on files". PhoneFilesCore is the Android half of
the desktop's hostfs.js (same answers, same shapes), and it holds the one rule a phone needs that a desktop does not:
"All files access" reaches far more than a person's files, so EVERY path is confined to a storage root -- after
symlinks are followed. javac + java run it here against a temporary "storage root" with a real outside next to it.
"""
import json
import os
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / "mobile/android/app/src/main/java/place/poster/app/files/PhoneFilesCore.java"
HAVE_JDK = shutil.which("javac") and shutil.which("java")

HARNESS = r"""
import place.poster.app.files.PhoneFilesCore;
import java.io.*;
import java.util.*;

public class Harness {
    static List<String> out = new ArrayList<>();
    static void ok(String what, boolean cond) { out.add((cond ? "ok   " : "FAIL ") + what); }
    static boolean refused(Runnable r) { try { r.run(); return false; } catch (RuntimeException e) { return true; } }
    interface Io { void run() throws Exception; }
    static boolean throwsIo(Io r) { try { r.run(); return false; } catch (Exception e) { return true; } }
    static String msg(Io r) { try { r.run(); return ""; } catch (Exception e) { return String.valueOf(e.getMessage()); } }

    public static void main(String[] a) throws Exception {
        File base = new File(a[0]), sd = new File(base, "storage"), outside = new File(base, "outside");
        PhoneFilesCore c = new PhoneFilesCore(Arrays.asList(sd));
        String R = sd.getCanonicalPath();

        Map<String, Object> l = c.list(R);
        List<?> names = new ArrayList<>();
        for (Object e : (List<?>) l.get("entries")) ((List<Object>) names).add(((Map<?, ?>) e).get("name"));
        ok("lists the root with folders and files", names.contains("Download") && names.contains("note.txt"));
        ok("the root has no parent (Up stops there)", l.get("parent") == null);
        ok("a subfolder's parent is the root", R.equals(c.list(R + "/Download").get("parent")));
        Map<?, ?> note = null;
        for (Object e : (List<?>) l.get("entries")) if ("note.txt".equals(((Map<?, ?>) e).get("name"))) note = (Map<?, ?>) e;
        ok("a file carries its size and is not a folder", note != null && ((Long) note.get("size")) == 5L && !((Boolean) note.get("dir")));

        // CONFINEMENT: nothing outside a storage root, by any spelling.
        ok("outside the root is refused", throwsIo(() -> c.list(outside.getCanonicalPath())));
        ok("../ out of the root is refused", throwsIo(() -> c.list(R + "/../outside")));
        ok("a symlink leading outside is refused", throwsIo(() -> c.read(R + "/Download/escape/secret.txt", 0)));
        ok("a relative path is refused", throwsIo(() -> c.list("storage")));
        ok("a folder name with a slash is refused", msg(() -> c.mkdir(R, "../evil")).contains("not a name"));
        ok("nothing was made outside", !new File(base, "evil").exists());

        // mkdir / rename: never over somebody's other file.
        String made = c.mkdir(R, "Photos");
        ok("mkdir makes the folder", new File(made).isDirectory());
        ok("rename refuses to overwrite", msg(() -> c.rename(R + "/note.txt", "Photos")).contains("already"));
        String renamed = c.rename(R + "/note.txt", "notes.txt");
        ok("rename renames", new File(renamed).isFile() && !new File(R, "note.txt").exists());
        ok("the storage root itself cannot be renamed", throwsIo(() -> c.rename(R, "x")));

        // copy into its own folder is a duplicate; a folder never goes inside itself; collisions refused.
        c.transfer(Arrays.asList(R + "/notes.txt"), R, false);
        ok("copy into the same folder makes a (copy)", new File(R, "notes (copy).txt").isFile());
        ok("a folder cannot go inside itself", msg(() -> c.transfer(Arrays.asList(R + "/Download"), R + "/Download/inner", false)).contains("inside itself"));
        c.transfer(Arrays.asList(R + "/notes.txt"), R + "/Photos", true);
        ok("move moves", new File(R, "Photos/notes.txt").isFile() && !new File(R, "notes.txt").exists());
        ok("a collision is refused", msg(() -> c.transfer(Arrays.asList(R + "/notes (copy).txt"), R, true)).contains("same"));
        c.transfer(Arrays.asList(R + "/Download/inner"), R + "/Photos", false);
        ok("folders copy whole", new File(R, "Photos/inner/deep.txt").isFile() && new File(R, "Download/inner/deep.txt").isFile());

        // trash: recoverable, on the same volume, and not shown as a folder.
        String trashed = c.trash(R + "/Photos/notes.txt");
        ok("trash moves into the volume's own bin", new File(trashed).isFile() && trashed.contains(PhoneFilesCore.TRASH));
        List<Object> after = new ArrayList<>();
        for (Object e : (List<?>) c.list(R).get("entries")) after.add(((Map<?, ?>) e).get("name"));
        ok("the bin is not listed", !after.contains(PhoneFilesCore.TRASH));
        ok("the root cannot be trashed", throwsIo(() -> c.trash(R)));

        // read / write: bounded, compare-and-swap, never creates.
        byte[] got = c.read(R + "/Download/hello.bin", 0);
        ok("read returns the bytes", got.length == 3 && got[0] == 1 && got[2] == 3);
        ok("read over the cap is refused", msg(() -> c.read(R + "/Download/hello.bin", 2)).contains("too big"));
        File doc = new File(R, "Download/doc.txt");
        long m0 = doc.lastModified();
        c.writeBytes(doc.getPath(), "two".getBytes("UTF-8"), m0);
        ok("write replaces the file", "two".equals(new String(c.read(doc.getPath(), 0), "UTF-8")));
        ok("a file changed since it was opened is not overwritten",
           msg(() -> c.writeBytes(doc.getPath(), "x".getBytes(), m0 - 60_000)).contains("changed-on-disk"));
        ok("write never creates a file", throwsIo(() -> c.writeBytes(R + "/Download/new.txt", "x".getBytes(), 0)));
        ok("no temp file is left behind", !new File(R, "Download/.pc-doc.txt.tmp").exists());

        ok("mime for a pdf", "application/pdf".equals(PhoneFilesCore.mimeOf("A.PDF")));
        for (String s : out) System.out.println(s);
    }
}
"""


@unittest.skipUnless(HAVE_JDK, "a JDK is required")
class PhoneFilesCoreRuns(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = Path(tempfile.mkdtemp(prefix="pc-phonefiles-"))
        build, world = cls.tmp / "build", cls.tmp / "world"
        sd, outside = world / "storage", world / "outside"
        (sd / "Download" / "inner").mkdir(parents=True)
        outside.mkdir()
        (outside / "secret.txt").write_text("secret")
        (sd / "note.txt").write_text("hello")
        (sd / "Download" / "hello.bin").write_bytes(bytes([1, 2, 3]))
        (sd / "Download" / "doc.txt").write_text("one")
        (sd / "Download" / "inner" / "deep.txt").write_text("deep")
        os.symlink(outside, sd / "Download" / "escape")
        build.mkdir()
        (build / "Harness.java").write_text(HARNESS)
        subprocess.run(["javac", "-d", str(build), str(CORE), str(build / "Harness.java")], check=True,
                       capture_output=True, text=True, timeout=180)
        r = subprocess.run(["java", "-cp", str(build), "Harness", str(world)], capture_output=True, text=True, timeout=120)
        cls.out = r.stdout + r.stderr

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.tmp, ignore_errors=True)

    def test_every_rule_holds(self):
        lines = [l for l in self.out.splitlines() if l.startswith(("ok", "FAIL"))]
        self.assertGreater(len(lines), 25, self.out)
        self.assertFalse([l for l in lines if l.startswith("FAIL")], self.out)


SDK_JAR = Path.home() / "android-sdk/platforms/android-35/android.jar"
STUBS = ROOT / "tests/androidstubs"


@unittest.skipUnless(HAVE_JDK and SDK_JAR.exists(), "a JDK and the Android SDK are required")
class PhoneFilesPluginCompiles(unittest.TestCase):
    """The APK only builds in CI; this is the floor -- the plugin against the REAL android.jar (Capacitor and androidx
    from the repo's stubs), so a wrong Android API name fails here in seconds, not in a CI build an hour later."""
    def test_the_plugin_compiles_against_the_android_sdk(self):
        java = ROOT / "mobile/android/app/src/main/java"
        with tempfile.TemporaryDirectory() as t:
            src = Path(t) / "src"
            for part in ("com", "androidx"):
                shutil.copytree(STUBS / part, src / part)
            files = sorted(str(p) for p in (java / "place/poster/app/files").glob("*.java"))
            r = subprocess.run(["javac", "-nowarn", "-d", str(Path(t) / "out"), "-cp", str(SDK_JAR),
                                "-sourcepath", str(src)] + files, capture_output=True, text=True, timeout=300)
        self.assertEqual(r.returncode, 0, r.stderr[-3000:])


class PhoneFilesWiring(unittest.TestCase):
    def test_the_plugin_is_registered_and_the_permissions_are_declared(self):
        main = (ROOT / "mobile/android/app/src/main/java/place/poster/app/MainActivity.java").read_text()
        self.assertIn("registerPlugin(place.poster.app.files.PhoneFilesPlugin.class)", main)
        man = (ROOT / "mobile/android/app/src/main/AndroidManifest.xml").read_text()
        self.assertIn("android.permission.MANAGE_EXTERNAL_STORAGE", man)
        self.assertIn('android:requestLegacyExternalStorage="true"', man)

    def test_the_bridge_is_fetched_by_files_and_cached_for_offline(self):
        # Not a boot <script>: Files fetches it the first time it draws inside the APK (the boot budget).
        page = (ROOT / "templates/client.html").read_text()
        sw = (ROOT / "static/js/client/sw.js").read_text()
        files = (ROOT / "static/js/client/files.js").read_text()
        self.assertNotIn("phonefiles.js", page)
        self.assertIn("'/static/js/client/phonefiles.js'", sw)
        self.assertIn("_withModule('phonefiles.js', 'pcPhoneHost')", files)

if __name__ == "__main__":
    unittest.main()
