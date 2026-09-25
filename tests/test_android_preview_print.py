"""Preview → Print on Android: the native half, compiled against the REAL Android SDK and RUN.

The WebView ignores window.print(), so the APK's Print button goes through PrintPlugin.java
(android.print.PrintManager). This test:
  * compiles PrintPlugin + OpenFilePlugin (it borrows safeName) against android.jar, so a wrong
    PrintDocumentAdapter signature is a failure here and not in CI's Gradle build;
  * RUNS its pure rules under `java`: a picture is fitted INSIDE the printable area (never cropped,
    centred), a huge photo is subsampled under the 4096px ceiling before it is decoded, and a PDF is
    recognised by its CONTENT (drive blobs often arrive as application/octet-stream);
  * checks the plugin is registered, and that the web half asks for it by name.
"""
import os
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

sys.path.insert(0, os.path.dirname(__file__))
import androidcompile as ac  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "mobile/android/app/src/main/java/place/poster/app/preview"
MAIN = (ROOT / "mobile/android/app/src/main/java/place/poster/app/MainActivity.java").read_text()
PREVIEW = (ROOT / "static/js/client/preview.js").read_text()

DRIVER = r"""
package place.poster.app.preview;
public final class PrintDriver {
  static void check(boolean ok, String what) { if (!ok) { System.out.println("FAIL " + what); System.exit(1); } }
  public static void main(String[] a) {
    // A landscape photo on a portrait page: full width, centred vertically, aspect kept.
    float[] r = PrintPlugin.fit(4000, 3000, 36, 36, 540, 720);
    check(Math.abs((r[2]-r[0]) - 540) < 0.01, "landscape fills the width");
    check(Math.abs((r[3]-r[1]) - 405) < 0.01, "aspect kept (540x405)");
    check(Math.abs(r[1] - (36 + (720-405)/2f)) < 0.01, "centred vertically");
    // A portrait photo: full height, never wider than the box.
    r = PrintPlugin.fit(1000, 4000, 0, 0, 600, 800);
    check(Math.abs((r[3]-r[1]) - 800) < 0.01 && (r[2]-r[0]) <= 600, "portrait fits the height");
    // Nothing ever lands outside the printable box.
    for (int w : new int[]{1, 7, 640, 9000}) for (int h : new int[]{1, 13, 480, 12000}) {
      r = PrintPlugin.fit(w, h, 10, 20, 500, 700);
      check(r[0] >= 9.99 && r[1] >= 19.99 && r[2] <= 510.01 && r[3] <= 720.01, "inside the box " + w + "x" + h);
    }
    check(PrintPlugin.sampleSize(8000, 6000, 4096) == 2, "8000px -> half");
    check(PrintPlugin.sampleSize(20000, 100, 4096) == 8, "20000px -> an eighth");
    check(PrintPlugin.sampleSize(4096, 4096, 4096) == 1, "at the ceiling stays whole");
    byte[] pdf = "%PDF-1.7\n".getBytes();
    check(PrintPlugin.isPdf("application/octet-stream", "3fa9c0", pdf), "PDF by its bytes");
    check(!PrintPlugin.isPdf("image/jpeg", "x.jpg", new byte[]{(byte)0xff,(byte)0xd8,(byte)0xff}), "a JPEG is a picture");
    check(PrintPlugin.isPdf("", "Report.PDF", new byte[]{1,2,3}), "PDF by name when nothing else says");
    System.out.println("OK");
  }
}
"""


def test_print_plugin_is_registered_and_asked_for():
    assert "registerPlugin(place.poster.app.preview.PrintPlugin.class)" in MAIN
    assert '@CapacitorPlugin(name = "Print")' in (PKG / "PrintPlugin.java").read_text()
    assert "capPlugin('Print', 'print')" in PREVIEW


@pytest.mark.skipif(not (shutil.which("javac") and shutil.which("java") and ac.android_jar()),
                    reason="needs javac, java and an android.jar")
def test_print_plugin_compiles_against_android_and_its_rules_hold():
    with tempfile.TemporaryDirectory() as out:
        drv = Path(out) / "src/place/poster/app/preview/PrintDriver.java"
        drv.parent.mkdir(parents=True)
        drv.write_text(DRIVER)
        r = ac.compile_sources([str(PKG / "PrintPlugin.java"), str(PKG / "OpenFilePlugin.java"), str(drv)], out)
        assert r.returncode == 0, r.stderr[-4000:]
        cp = os.pathsep.join([os.path.join(out, "classes"), ac.android_jar()])
        run = subprocess.run([shutil.which("java"), "-cp", cp, "place.poster.app.preview.PrintDriver"],
                             capture_output=True, text=True, timeout=60)
        assert run.stdout.strip().endswith("OK"), run.stdout + run.stderr
