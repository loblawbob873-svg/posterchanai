"""Car (Bluetooth AVRCP) buttons on a playing track: where a press can be lost, and the instruments that say which.

Reported: "car media controls no longer work on music" -- plain Bluetooth, PosterChan music playing in the
background, a press does NOTHING AT ALL. A media key reaches this app only through an ACTIVE media session,
so the failure is "no session holds the buttons", "the wrong session holds them", or "the session holds them
and the page never answers". Covered here, off-device:

* TaskRemoval -- `Service.onTaskRemoved` fires for ANY task of the app leaving Recents, and Texts and the
  Phone app are tasks of their own. MusicService answered every one with shutdown(): the media session was
  released under a song that played on in the untouched WebView task. javac + java RUN the rule, and the
  same harness is run against the old behaviour to prove it fails there.
* PressLog -- the bounded record of the last presses Settings -> Phone -> "Music & car controls" shows.
* The standby session's PAUSE is handed to the page instead of being a no-op, and the cold path performs a
  pause for a page that is listening.
* Every press is logged under the `PosterChanMusic` logcat tag, and CI keeps that tag's lines.

The device half is mobile/android/app/src/androidTest/.../music/MusicCarButtonsDeviceTest.java (CI emulator).
"""
import os
import re
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANDROID = os.path.join(ROOT, "mobile", "android", "app")
MUSIC = os.path.join(ANDROID, "src", "main", "java", "place", "poster", "app", "music")
SERVICE_PATH = os.path.join(MUSIC, "MusicService.java")
STAY_PATH = os.path.join(ANDROID, "src", "main", "java", "place", "poster", "app", "push", "StayAwakeService.java")
PLUGIN_PATH = os.path.join(MUSIC, "MusicPlugin.java")
DEVICE_TEST = os.path.join(ANDROID, "src", "androidTest", "java", "place", "poster", "app", "music",
                           "MusicCarButtonsDeviceTest.java")


def _read(p):
    with open(p, encoding="utf-8") as f:
        return f.read()


def _code(java):
    java = re.sub(r"/\*.*?\*/", "", java, flags=re.S)
    return re.sub(r"//[^\n]*", "", java)


HAVE_JAVA = bool(shutil.which("javac") and shutil.which("java"))

HARNESS = r'''
import place.poster.app.music.TaskRemoval;
import place.poster.app.music.PressLog;
public class Harness {
  static int bad = 0;
  static void eq(String what, Object got, Object want) {
    if (!String.valueOf(got).equals(String.valueOf(want))) { bad++; System.out.println("FAIL " + what + ": " + got + " != " + want); }
  }
  public static void main(String[] a) {
    String P = "place.poster.app.MainActivity";
    // The player's own task: its WebView goes with it, so the session must too.
    eq("player task", TaskRemoval.endsPlayer(P, null, P), true);
    eq("alias of the player", TaskRemoval.endsPlayer("place.poster.app.LauncherAlias", P, P), true);
    eq("could not tell", TaskRemoval.endsPlayer(null, null, P), true);
    // Texts and the Phone app are tasks of their own: closing them leaves the song playing.
    eq("texts task", TaskRemoval.endsPlayer("place.poster.app.sms.ThreadListActivity", null, P), false);
    eq("thread task", TaskRemoval.endsPlayer("place.poster.app.sms.ThreadActivity", null, P), false);
    eq("phone task", TaskRemoval.endsPlayer("place.poster.app.phone.DialerActivity", null, P), false);
    eq("alias of another", TaskRemoval.endsPlayer("place.poster.app.TextsAlias", "place.poster.app.sms.ThreadListActivity", P), false);

    PressLog.clear();
    for (int i = 0; i < 25; i++) PressLog.add(1000 + i, "next", "via" + i, "sent");
    eq("bounded", PressLog.recent().size(), PressLog.MAX);
    eq("newest first", PressLog.recent().get(0).via, "via24");
    PressLog.Entry e = PressLog.add(5000, "pause", "PosterChanMusic session", "sent to the page");
    PressLog.settle(e, "answered");
    eq("settled", PressLog.recent().get(0).outcome, "answered");
    eq("text", PressLog.recent().get(0).toString(), "pause via PosterChanMusic session: answered");
    PressLog.settle(null, "x");   // a settle with nothing to settle is not a crash
    System.out.println(bad == 0 ? "OK" : ("BAD " + bad));
    System.exit(bad == 0 ? 0 : 1);
  }
}
'''

OLD_TASK_REMOVAL = r'''
package place.poster.app.music;
/** What shipped before: onTaskRemoved shut down on EVERY task removal. */
public final class TaskRemoval {
  public static boolean endsPlayer(String removedClass, String aliasTarget, String playerClass) { return true; }
}
'''


def _run(task_removal_src=None):
    with tempfile.TemporaryDirectory() as d:
        src = os.path.join(d, "src", "place", "poster", "app", "music")
        os.makedirs(src)
        shutil.copy(os.path.join(MUSIC, "PressLog.java"), src)
        if task_removal_src is None:
            shutil.copy(os.path.join(MUSIC, "TaskRemoval.java"), src)
        else:
            with open(os.path.join(src, "TaskRemoval.java"), "w") as f:
                f.write(task_removal_src)
        h = os.path.join(d, "src", "Harness.java")
        with open(h, "w") as f:
            f.write(HARNESS)
        out = os.path.join(d, "out")
        c = subprocess.run(["javac", "-nowarn", "-encoding", "UTF-8", "-d", out, h,
                            os.path.join(src, "PressLog.java"), os.path.join(src, "TaskRemoval.java")],
                           capture_output=True, text=True, timeout=120)
        assert c.returncode == 0, c.stderr
        return subprocess.run(["java", "-cp", out, "Harness"], capture_output=True, text=True, timeout=60)


@pytest.mark.skipif(not HAVE_JAVA, reason="javac/java required")
def test_the_task_rule_and_the_press_log_run():
    r = _run()
    assert r.returncode == 0 and r.stdout.strip().endswith("OK"), r.stdout + r.stderr


@pytest.mark.skipif(not HAVE_JAVA, reason="javac/java required")
def test_the_harness_fails_on_the_behaviour_that_shipped():
    """Fail-first: the old onTaskRemoved (shut down on ANY task) is caught by the same harness."""
    r = _run(OLD_TASK_REMOVAL)
    assert r.returncode != 0, "the harness cannot tell the old shutdown-on-any-task rule from the fix"
    assert "FAIL texts task" in r.stdout and "FAIL phone task" in r.stdout, r.stdout


def test_on_task_removed_asks_the_rule_before_shutting_down():
    body = re.search(r"public void onTaskRemoved\(Intent rootIntent\) \{(.*?)\n  \}", _code(_read(SERVICE_PATH)), re.S)
    assert body, "onTaskRemoved moved -- re-point this test"
    b = body.group(1)
    assert "TaskRemoval.endsPlayer(" in b, "onTaskRemoved shuts the session down for ANY task of the app"
    assert b.index("TaskRemoval.endsPlayer(") < b.index("shutdown()"), "shutdown() is not behind the rule"
    assert "targetActivity" in b, "an activity-alias of the player must count as the player"
    assert "MainActivity.class.getName()" in b


def test_every_press_is_recorded_and_logged_under_one_tag():
    svc = _code(_read(SERVICE_PATH))
    assert 'TAG = "PosterChanMusic"' in svc
    for fn in ("press", "hush"):
        body = re.search(r"private void %s\(final String action, final String via\) \{(.*?)\n  \}" % fn, svc, re.S)
        assert body, f"{fn}() lost its `via` -- the log could no longer say which session delivered a press"
        assert "record(action, via)" in body.group(1) and "settle(e," in body.group(1), fn
    assert "Log.i(TAG" in svc
    # Which KEY arrived at which session -- the one fact a seat cannot see.
    assert svc.count("onMediaButtonEvent") >= 1 and "keyCodeToString" in svc
    stay = _code(_read(STAY_PATH))
    assert "onMediaButtonEvent" in stay and "MusicService.TAG" in stay


def test_the_standby_pause_reaches_a_playing_page():
    """The standby session's onPause used to be a no-op -- the one press a driver needs when the page plays
    without a PosterChanMusic session. It is handed to MusicService, whose cold path pauses a listening page
    and never wakes the app for it."""
    stay = _code(_read(STAY_PATH))
    m = re.search(r"public void onPause\(\) \{([^}]*)\}", stay)
    assert m and "handOff(MusicService.ACTION_PAUSE)" in m.group(1), "standby PAUSE is still a no-op"
    assert "EXTRA_VIA" in stay, "the standby session's hand-offs are not labelled in the press log"
    svc = _code(_read(SERVICE_PATH))
    cold = re.search(r"if \(!foreground\) \{(.*?)\n    \}\n", svc, re.S)
    assert cold, "cold branch moved -- re-point this test"
    c = cold.group(1)
    assert "isPause(action, intent)" in c and 'emit("pause", 0)' in c
    pause_block = c[c.index("isPause(action, intent)"):c.index("if (want != null)")]
    assert "revive(" not in pause_block, "a pause must never open the app"


def test_status_reports_the_sessions_and_the_presses():
    plugin = _code(_read(PLUGIN_PATH))
    for key in ("presses", "sessionActive", "standbySession", "pageListening", "logTag"):
        assert f'"{key}"' in plugin, f"status() does not report {key}"
    assert plugin.count('r.put("listening"') == 1, "a key is put twice"


def test_the_device_test_exists_and_ci_runs_every_instrumented_test():
    t = _read(DEVICE_TEST)
    assert "@RunWith(AndroidJUnit4.class)" in t and "dispatchMediaKeyEvent" in t
    assert "getTransportControls" in t and "finishAndRemoveTask()" in t and "StayAwakeService" in t
    for k in ("KEYCODE_MEDIA_PAUSE", "KEYCODE_MEDIA_PLAY", "KEYCODE_MEDIA_NEXT", "KEYCODE_MEDIA_PREVIOUS",
              "KEYCODE_MEDIA_PLAY_PAUSE"):
        assert k in t, k
    sh = _read(os.path.join(ROOT, "scripts", "android_instrumented.sh"))
    # The whole :app androidTest set runs -- no class filter that could leave the new test out.
    assert ":app:connectedDebugAndroidTest" in sh and "testInstrumentationRunnerArguments.class" not in sh
    assert "PosterChanMusic:*" in sh, "CI does not keep the music press log from the emulator"
