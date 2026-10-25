"""Settings -> Phone -> "Restarts & memory" names WHY the app's process last ended (ApplicationExitInfo).

The tablet desktop "starts like a fresh new session" with no toast (2026-10-25). A renderer death always toasts, so
that is the whole process ending -- and Android records why. ExitReasons labels it; it is pure Java, so this
compiles it with javac and RUNS it against ApplicationExitInfo's documented constants.
"""
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "mobile/android/app/src/main/java/place/poster/app/health"

HARNESS = r'''
import place.poster.app.health.ExitReasons;
public class Check {
  static void eq(String a, String b) { if (!a.equals(b)) throw new AssertionError(a + " != " + b); }
  public static void main(String[] x) {
    // ApplicationExitInfo: REASON_LOW_MEMORY=3, CRASH=4, CRASH_NATIVE=5, ANR=6, USER_REQUESTED=10, PACKAGE_UPDATED=16
    eq(ExitReasons.reason(3), "low-memory"); eq(ExitReasons.reason(4), "crash");
    eq(ExitReasons.reason(5), "crash-native"); eq(ExitReasons.reason(6), "anr");
    eq(ExitReasons.reason(10), "user-requested"); eq(ExitReasons.reason(16), "package-updated");
    eq(ExitReasons.reason(99), "unknown");
    // A memory reclaim, a crash and an ANR are worth a report; the person swiping it away or an update is not.
    if (!ExitReasons.unexpected(3) || !ExitReasons.unexpected(4) || !ExitReasons.unexpected(5) || !ExitReasons.unexpected(6))
      throw new AssertionError("a fault was not flagged");
    if (ExitReasons.unexpected(10) || ExitReasons.unexpected(16) || ExitReasons.unexpected(11))
      throw new AssertionError("a deliberate end was flagged");
    // RunningAppProcessInfo.IMPORTANCE_FOREGROUND=100, VISIBLE=200, CACHED=400
    eq(ExitReasons.importance(100), "foreground"); eq(ExitReasons.importance(200), "visible");
    eq(ExitReasons.importance(400), "cached");
    if (!ExitReasons.onScreen(100) || !ExitReasons.onScreen(200) || ExitReasons.onScreen(400))
      throw new AssertionError("on-screen is wrong");
    System.out.println("ok");
  }
}
'''


@pytest.mark.skipif(not shutil.which("javac"), reason="javac required")
def test_exit_reasons_are_named_and_judged(tmp_path):
    (tmp_path / "Check.java").write_text(HARNESS)
    out = tmp_path / "out"
    r = subprocess.run(["javac", "-d", str(out), str(SRC / "ExitReasons.java"), str(tmp_path / "Check.java")],
                       capture_output=True, text=True)
    assert r.returncode == 0, r.stderr
    r = subprocess.run(["java", "-cp", str(out), "Check"], capture_output=True, text=True)
    assert r.returncode == 0 and "ok" in r.stdout, r.stdout + r.stderr


def test_the_plugin_is_registered_and_guarded_for_old_android():
    main = (ROOT / "mobile/android/app/src/main/java/place/poster/app/MainActivity.java").read_text()
    assert "registerPlugin(place.poster.app.health.AppHealthPlugin.class);" in main
    plugin = (SRC / "AppHealthPlugin.java").read_text()
    # getHistoricalProcessExitReasons is API 30; minSdk is 26 and lint's NewApi is fatal for the release build.
    call = plugin.index("getHistoricalProcessExitReasons")
    assert "Build.VERSION.SDK_INT >= Build.VERSION_CODES.R" in plugin[:call]
