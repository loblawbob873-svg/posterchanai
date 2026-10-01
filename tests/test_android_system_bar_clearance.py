"""The APK's page is moved clear of the status bar by MEASUREMENT -- SystemBarClearance, RUN with javac.

Reported 2026-09-24 with a screenshot from a Galaxy S25 (One UI 8, Android 16) on APK 1.0.2365: the
search box on Nostrverse and Notifications started UNDER the clock, on the build that carried the
first fix. That fix margined the WebView by the insets its listener was handed, and on that phone it
was handed nothing. The margin is now computed from where the view is and where the bars are.
The device half (edge-to-edge forced, listener starved) is PageClearsTheStatusBarDeviceTest.
"""
import json
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
PKG = ROOT / "mobile/android/app/src/main/java/place/poster/app"
HAVE_JDK = shutil.which("javac") and shutil.which("java")

MAIN = r"""
package place.poster.app;
public class ClearanceMain {
  public static void main(String[] a) {
    StringBuilder out = new StringBuilder("[");
    int[][][] cases = {
      // view {l,t,r,b}, current margins, window {w,h}, bars {l,t,r,b}
      {{0,0,1080,2340},{0,0,0,0},{1080,2340},{0,110,0,63}},     // S25: drawn under both bars
      {{0,110,1080,2277},{0,0,0,0},{1080,2340},{0,110,0,63}},   // system already insets the window
      {{0,110,1080,2277},{0,110,0,63},{1080,2340},{0,110,0,63}},// our own margin already applied
      {{0,110,1080,2277},{0,110,0,63},{1080,2340},{0,0,0,0}},   // bars gone (fullscreen): take it back
      {{0,50,1080,2340},{0,0,0,0},{1080,2340},{0,110,0,0}},     // partly under: exactly the overlap
    };
    for (int i = 0; i < cases.length; i++) {
      int[][] c = cases[i];
      int[] m = SystemBarClearance.margins(c[0][0], c[0][1], c[0][2], c[0][3], c[1], c[2][0], c[2][1], c[3]);
      out.append(i == 0 ? "" : ",").append("[").append(m[0]).append(",").append(m[1]).append(",")
         .append(m[2]).append(",").append(m[3]).append("]");
    }
    System.out.println(out.append("]"));
  }
}
"""


@pytest.mark.skipif(not HAVE_JDK, reason="javac/java not installed")
def test_the_margin_is_exactly_the_overlap_measured_and_never_counted_twice():
    with tempfile.TemporaryDirectory() as d:
        main = Path(d) / "ClearanceMain.java"
        main.write_text(MAIN)
        c = subprocess.run(["javac", "-d", d, str(PKG / "SystemBarClearance.java"), str(main)],
                           capture_output=True, text=True)
        assert c.returncode == 0, c.stderr
        r = subprocess.run(["java", "-cp", d, "place.poster.app.ClearanceMain"], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout) == [
            [0, 110, 0, 63],   # the S25: pushed below the status bar and above the nav bar
            [0, 0, 0, 0],      # already clear: nothing
            [0, 110, 0, 63],   # our margin counted once, kept
            [0, 0, 0, 0],      # the bars went away: the margin goes too
            [0, 60, 0, 0],     # half under: exactly the 60px that overlap
        ]


def test_main_activity_measures_on_layout_not_only_when_handed_insets():
    src = (PKG / "MainActivity.java").read_text()
    body = src[src.index("private void keepWebViewClearOfSystemBars()"):src.index("private void allowMediaWithoutAGesture()")]
    assert "SystemBarClearance.margins(" in body
    assert "addOnLayoutChangeListener" in body, "only an insets event re-checks -- the S25 never sends one"
    assert "getRootWindowInsets" in body


KEYBOARD = r"""
package place.poster.app;
public class KeyboardMain {
  public static void main(String[] a) {
    int[] bars = {0, 110, 0, 63};
    // An edge-to-edge window (One UI 8) with the keyboard up: the view is the whole window.
    int[] e2e = SystemBarClearance.margins(0, 0, 1080, 2340, new int[]{0,0,0,0}, 1080, 2340,
        SystemBarClearance.withKeyboard(bars, 900));
    // The system already resized the window for the keyboard: the view ends above it already.
    int[] resized = SystemBarClearance.margins(0, 110, 1080, 1440, new int[]{0,0,0,0}, 1080, 2340,
        SystemBarClearance.withKeyboard(bars, 900));
    // Keyboard gone again: back to just the bars.
    int[] gone = SystemBarClearance.margins(0, 110, 1080, 1440, new int[]{0,110,0,900}, 1080, 2340,
        SystemBarClearance.withKeyboard(bars, 0));
    System.out.println("[" + java.util.Arrays.toString(e2e) + "," + java.util.Arrays.toString(resized)
        + "," + java.util.Arrays.toString(gone) + "]");
  }
}
"""


@pytest.mark.skipif(not HAVE_JDK, reason="javac/java not installed")
def test_the_page_also_stays_clear_of_the_keyboard_without_ever_leaving_the_top():
    """"the terminal top gets cut off, where you see the terminal tabs": the keyboard is a bar the page
    must stay ABOVE -- by shrinking, never by sliding up under the clock."""
    with tempfile.TemporaryDirectory() as d:
        main = Path(d) / "KeyboardMain.java"
        main.write_text(KEYBOARD)
        c = subprocess.run(["javac", "-d", d, str(PKG / "SystemBarClearance.java"), str(main)],
                           capture_output=True, text=True)
        assert c.returncode == 0, c.stderr
        r = subprocess.run(["java", "-cp", d, "place.poster.app.KeyboardMain"], capture_output=True, text=True)
        assert r.returncode == 0, r.stderr
        assert json.loads(r.stdout) == [
            [0, 110, 0, 900],   # edge-to-edge: below the clock AND above the keyboard
            [0, 0, 0, 0],       # the system already resized: nothing to add
            [0, 110, 0, 63],    # keyboard closed: the bottom margin shrinks back to the nav bar
        ]


def test_the_main_screen_resizes_for_the_keyboard_instead_of_panning():
    """Unset, Android may PAN the window for a focused field -- the whole page slides up under the
    status bar. Every other activity here already says adjustResize; the main one must too."""
    import xml.etree.ElementTree as ET
    ns = "{http://schemas.android.com/apk/res/android}"
    root = ET.parse(ROOT / "mobile/android/app/src/main/AndroidManifest.xml").getroot()
    main = [a for a in root.iter("activity") if a.get(ns + "name") == ".MainActivity"]
    assert len(main) == 1
    assert (main[0].get(ns + "windowSoftInputMode") or "").startswith("adjustResize"), \
        "MainActivity leaves the keyboard mode to Android, which pans the page under the status bar"
