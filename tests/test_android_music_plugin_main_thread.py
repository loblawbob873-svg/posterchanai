"""The music service is only ever changed on the main thread, and CI's device run holds no skipped case.

Found in CI's emulator logcat (2026-10-24, run 40128423159 attempt 2): the APP PROCESS crashed with

    NullPointerException ... MediaSessionCompat.getSessionToken() on a null object reference
      at MusicService.notification  <- publish <- apply <- MusicPlugin.push <- MusicPlugin.update

Capacitor runs plugin methods on its own plugin thread, and MusicPlugin called MusicService.apply() /
ack() / shutdown() straight from there, while onDestroy() runs on the main thread and sets
`session = null` -- in the middle of a publish(), whose first session call had just succeeded. A crashed
process takes the media session with it, which is what a car's buttons talk to.

And the same run was refused by the evidence rule for a SKIPPED case: the performance probe, made to skip
unless named, so that it would stop timing out. A skip is not verified behaviour; the probe is now left out
of the default run instead.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MUSIC = ROOT / "mobile/android/app/src/main/java/place/poster/app/music"
PLUGIN = (MUSIC / "MusicPlugin.java").read_text()
SERVICE = (MUSIC / "MusicService.java").read_text()

# Plugin reads of the service's state, which change nothing (Music → Details).
READS = {"sessionActive", "isPlaying", "webSilenceMs", "webGone"}


def _calls_on_svc(src):
    """Every `svc.<method>(` and `svc::<method>` in the plugin, with whether it is handed to runOnMain."""
    out = []
    for m in re.finditer(r"svc(?:\.|::)(\w+)", src):
        name = m.group(1)
        if name == "runOnMain":
            continue
        line_start = src.rfind("\n", 0, m.start()) + 1
        before = src[line_start:m.start()]
        out.append((name, "runOnMain(" in before))
    return out


def test_every_plugin_call_that_changes_the_music_service_runs_on_the_main_thread():
    calls = _calls_on_svc(PLUGIN)
    assert {n for n, _ in calls} >= {"apply", "ack", "shutdown"}, ("the plugin no longer calls these; re-read it", calls)
    off_main = [n for n, on_main in calls if n not in READS and not on_main]
    assert not off_main, ("MusicPlugin changes MusicService from Capacitor's plugin thread", off_main)


def test_run_on_main_runs_on_the_main_looper_and_publish_tolerates_a_released_session():
    body = SERVICE[SERVICE.index("void runOnMain(Runnable r)"):][:300]
    assert "Looper.getMainLooper()" in body and "handler.post(r)" in body, body
    assert re.search(r"Handler handler = new Handler\(Looper\.getMainLooper\(\)\)", SERVICE)
    publish = SERVICE[SERVICE.index("private void publish() {"):][:300]
    assert "if (session == null) return;" in publish, "a call queued before onDestroy must not touch a released session"


def test_ci_leaves_the_performance_probe_out_of_the_device_run_rather_than_skipping_it():
    script = (ROOT / "scripts/android_instrumented.sh").read_text()
    gradle = re.search(r"\./gradlew :app:connectedDebugAndroidTest(?:[^\n]*\\\n)*[^\n]*", script).group(0)
    assert "notClass=place.poster.app.AppPerformanceProbeDeviceTest" in gradle, gradle
    # and the run still refuses skipped cases -- the rule that caught it stays
    assert "'skipped'" in script
