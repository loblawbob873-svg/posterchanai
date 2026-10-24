"""A device run in which the app crashed fails, even when every test passed.

The music service's cross-thread crash (2026-10-24, run 40128423159 attempt 2) went through 168 green instrumented
tests: gradle printed "Logcat of last crash" and the job only failed because of an unrelated skipped case. These run
the SHIPPED scripts/android_crash_verdict.py on that run's real text and on the shapes that must NOT count.
"""
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERDICT = ROOT / "scripts/android_crash_verdict.py"

# Verbatim from run 40128423159 attempt 2 (gradle's own output, timestamps stripped).
GRADLE_CRASH = """emulator-5554 - 14 Tests 169/168 completed. (1 skipped) (0 failed)
Finished 169 tests on emulator-5554 - 14
> Task :app:connectedDebugAndroidTest
Logcat of last crash:
Process: place.poster.app, PID: 5317
java.lang.RuntimeException: java.lang.reflect.InvocationTargetException
\tat com.getcapacitor.Bridge.lambda$callPluginMethod$0(Bridge.java:830)
\tat android.os.HandlerThread.run(HandlerThread.java:67)
Caused by: java.lang.reflect.InvocationTargetException
\tat java.lang.reflect.Method.invoke(Native Method)
Caused by: java.lang.NullPointerException: Attempt to invoke virtual method 'android.support.v4.media.session.MediaSessionCompat$Token android.support.v4.media.session.MediaSessionCompat.getSessionToken()' on a null object reference
\tat place.poster.app.music.MusicService.notification(MusicService.java:978)
\tat place.poster.app.music.MusicPlugin.update(MusicPlugin.java:87)
\t... 10 more
BUILD SUCCESSFUL in 12m 33s
"""

# logcat -b crash -v threadtime, the other source.
LOGCAT_CRASH = """10-24 20:41:02.115  5317  5402 E AndroidRuntime: FATAL EXCEPTION: CapacitorPlugins
10-24 20:41:02.115  5317  5402 E AndroidRuntime: Process: place.poster.app, PID: 5317
10-24 20:41:02.115  5317  5402 E AndroidRuntime: java.lang.NullPointerException: getSessionToken() on a null object reference
10-24 20:41:02.115  5317  5402 E AndroidRuntime: \tat place.poster.app.music.MusicService.notification(MusicService.java:978)
"""

CLEAN = """emulator-5554 - 14 Tests 167/167 completed. (0 skipped) (0 failed)
Finished 167 tests on emulator-5554 - 14
BUILD SUCCESSFUL in 12m 2s
"""

SOMEONE_ELSES = """10-24 20:41:02.115  1201  1250 E AndroidRuntime: FATAL EXCEPTION: main
10-24 20:41:02.115  1201  1250 E AndroidRuntime: Process: com.google.android.gms.persistent, PID: 1201
10-24 20:41:02.115  1201  1250 E AndroidRuntime: java.lang.IllegalStateException: something in Play Services
10-24 20:41:03.000  1300  1300 E AndroidRuntime: FATAL EXCEPTION: main
10-24 20:41:03.000  1300  1300 E AndroidRuntime: Process: place.poster.app2, PID: 1300
"""

# Our package named in a log line that is NOT a crash report (a test logging the process, the ActivityManager).
NOT_A_CRASH = """10-24 20:41:02.115  600  700 I ActivityManager: Start proc 5317:place.poster.app/u0a190 for top-activity
10-24 20:41:02.200  5317 5317 I PosterChan: Process: place.poster.app, PID: 5317 is up
"""


def _run(tmp_path, *texts):
    paths = []
    for i, t in enumerate(texts):
        p = tmp_path / ("log%d.txt" % i)
        p.write_text(t)
        paths.append(str(p))
    return subprocess.run([sys.executable, str(VERDICT), *paths, str(tmp_path / "missing.txt")],
                          capture_output=True, text=True, timeout=30)


def test_the_real_crash_from_gradles_log_fails_the_run_and_names_the_cause(tmp_path):
    r = _run(tmp_path, GRADLE_CRASH)
    assert r.returncode == 1, r.stdout
    assert "::error title=The app crashed during the device run::" in r.stdout
    assert "NullPointerException" in r.stdout.splitlines()[0], "the annotation must name the root cause"
    assert "MusicService.notification" in r.stdout


def test_the_crash_buffer_alone_fails_the_run_and_one_crash_is_reported_once(tmp_path):
    r = _run(tmp_path, LOGCAT_CRASH)
    assert r.returncode == 1, r.stdout
    both = _run(tmp_path, GRADLE_CRASH, LOGCAT_CRASH)
    assert both.returncode == 1


def test_a_clean_run_another_apps_crash_and_a_mere_mention_do_not_fail_it(tmp_path):
    for text in (CLEAN, SOMEONE_ELSES, NOT_A_CRASH, ""):
        r = _run(tmp_path, text)
        assert r.returncode == 0, (text[:80], r.stdout)


def test_the_device_run_consults_the_verdict_after_its_evidence_and_keeps_the_worse_answer():
    script = (ROOT / "scripts/android_instrumented.sh").read_text()
    tail = script[script.index("PYRESULT\nevidence=$?"):]
    assert 'python3 "$HERE/android_crash_verdict.py" /tmp/pc-instrumented.log /tmp/pc-androidtest/logcat-instrumented-crash.txt || evidence=1' in tail
    assert tail.rstrip().endswith('exit "$evidence"')
    # HERE is taken before the script leaves the repo root.
    assert script.index('HERE=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)') < script.index("cd mobile/android || exit 1")
    # The crash buffer it reads is the one the script captures.
    assert "adb logcat -d -b crash -v threadtime \\\n    > /tmp/pc-androidtest/logcat-instrumented-crash.txt" in script
