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
