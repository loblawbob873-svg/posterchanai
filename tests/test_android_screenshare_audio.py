"""A phone screen share controls the MIC and the SCREEN'S SOUND separately.

    "When I go live with my phone sharing screen, the 'mute' button mutes my mic AND the screen
     audio. I want to control it individually: one option for the microphone and another for the
     screen (if it is enabled)."

Measured, not guessed: ScreenShareService built its stream as `new RtmpStream(..., new
NoVideoSource(), mic)` with `mic = new MicrophoneSource(DEFAULT)` and never changed the audio source.
The microphone was the share's ONLY input — what viewers heard of a game was the speaker picked up by
that mic — so `setMuted()` silenced both, and there was nothing a second button could have toggled.

The fix sends the phone's own playback as its own input (RootEncoder 2.7.2's MixAudioSource: the mic +
AudioPlaybackCapture through the MediaProjection the share already holds, Android 10+), each half on
its own volume. The decision and the two mute states live in ScreenAudioPlan, a pure class, so this
file COMPILES AND RUNS it with plain javac; the service/plugin wiring is guarded by source checks and,
where this box has the Gradle cache, by compiling the real package against the real RootEncoder,
Capacitor and android.jar (signatures read with javap: MixAudioSource(MediaProjection,
MediaProjection.Callback, int), setMicrophoneVolume/setInternalVolume(float),
StreamBase.changeAudioSource(AudioSource)). CI's assembleDebug is the last word on the APK.
"""
import glob
import os
import re
import shutil
import subprocess
import tempfile
import zipfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PKG = os.path.join(ROOT, "mobile", "android", "app", "src", "main", "java",
                   "place", "poster", "app", "screenshare")


def _read(name):
    with open(os.path.join(PKG, name), encoding="utf-8") as fh:
        return fh.read()


SERVICE = _read("ScreenShareService.java")
PLUGIN = _read("ScreenSharePlugin.java")

javac_missing = pytest.mark.skipif(not (shutil.which("javac") and shutil.which("java")),
                                   reason="no JDK on this node")


def _run_plan(driver_body):
    """Compile the SHIPPED ScreenAudioPlan.java with a driver in its package and run it."""
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "src", "place", "poster", "app", "screenshare")
        os.makedirs(src)
        shutil.copy(os.path.join(PKG, "ScreenAudioPlan.java"), src)
        with open(os.path.join(src, "Driver.java"), "w") as fh:
            fh.write("package place.poster.app.screenshare;\n"
                     "public class Driver { public static void main(String[] a) {\n"
                     + driver_body + "\n} }\n")
        out = os.path.join(tmp, "out")
        r = subprocess.run(["javac", "-d", out] + glob.glob(os.path.join(src, "*.java")),
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        r = subprocess.run(["java", "-cp", out, "place.poster.app.screenshare.Driver"],
                           capture_output=True, text=True)
        assert r.returncode == 0, r.stdout + r.stderr
        return dict(line.split("=", 1) for line in r.stdout.split())


@javac_missing
class TestTheDecisionRuns:
    def test_android_10_with_the_mic_mixes_in_the_screen(self):
        out = _run_plan("""
          System.out.println("q=" + ScreenAudioPlan.choose(29, true));
          System.out.println("v=" + ScreenAudioPlan.choose(35, true));
          System.out.println("p=" + ScreenAudioPlan.choose(28, true));
          System.out.println("nomic=" + ScreenAudioPlan.choose(35, false));
        """)
        assert out == {"q": "MIX", "v": "MIX", "p": "MIC", "nomic": "MIC"}

    def test_muting_the_mic_leaves_the_screen_on_air_and_back(self):
        out = _run_plan("""
          ScreenAudioPlan p = new ScreenAudioPlan(false, false);
          p.useSource(ScreenAudioPlan.Source.MIX);
          p.setMicMuted(true);
          System.out.println("mic=" + p.micVolume());
          System.out.println("screen=" + p.screenVolume());
          p.setMicMuted(false); p.setScreenMuted(true);
          System.out.println("mic2=" + p.micVolume());
          System.out.println("screen2=" + p.screenVolume());
          p.setMicMuted(true);
          System.out.println("both_mic=" + p.micVolume());
          System.out.println("both_screen=" + p.screenVolume());
        """)
        assert out == {"mic": "0.0", "screen": "1.0", "mic2": "1.0", "screen2": "0.0",
                       "both_mic": "0.0", "both_screen": "0.0"}

    def test_mutes_given_before_the_source_exists_are_kept(self):
        """The pre-air rule: the states arrive from the start Intent BEFORE the mix is swapped in."""
        out = _run_plan("""
          ScreenAudioPlan p = new ScreenAudioPlan(true, true);
          p.useSource(ScreenAudioPlan.Source.MIX);
          System.out.println("mic=" + p.micVolume());
          System.out.println("screen=" + p.screenVolume());
        """)
        assert out == {"mic": "0.0", "screen": "0.0"}

    def test_no_screen_audio_means_none_is_claimed(self):
        out = _run_plan("""
          ScreenAudioPlan p = new ScreenAudioPlan(false, false);
          p.useSource(ScreenAudioPlan.Source.MIC);
          System.out.println("has=" + p.hasScreenAudio());
          System.out.println("screen=" + p.screenVolume());
          System.out.println("mic=" + p.micVolume());
        """)
        assert out == {"has": "false", "screen": "0.0", "mic": "1.0"}


class TestTheServiceWiring:
    def test_the_screen_sound_is_its_own_input(self):
        assert "import com.pedro.encoder.input.sources.audio.MixAudioSource;" in SERVICE
        assert re.search(r"new MixAudioSource\(\s*projection\s*,", SERVICE)
        assert "stream.changeAudioSource(" in SERVICE
        assert "ScreenAudioPlan.choose(Build.VERSION.SDK_INT, recordAudioGranted())" in SERVICE

    def test_mutes_are_volumes_never_the_whole_mix(self):
        """MixAudioSource.mute() silences BOTH inputs — the reported bug, one layer down."""
        assert "setMicrophoneVolume(audio.micVolume())" in SERVICE
        assert "setInternalVolume(audio.screenVolume())" in SERVICE
        assert not re.search(r"\bm(ix)?\.mute\(\)", SERVICE)

    def test_both_mutes_are_applied_before_going_on_air(self):
        body = SERVICE[SERVICE.index("public int onStartCommand"):]
        body = body[:body.index("private int serviceTypes")]
        swap = body.index("useScreenAudio();")
        mic = body.index("audio.setMicMuted(intent.getBooleanExtra(EXTRA_MUTED")
        scr = body.index("audio.setScreenMuted(intent.getBooleanExtra(EXTRA_SCREEN_MUTED")
        apply_ = body.index("applyAudio();")
        air = body.index("startOnAir(url);")
        assert swap < mic < apply_ < air and scr < apply_, \
            "a mute applied after startStream puts real audio on air for the round trip"

    def test_the_fallback_to_mic_reapplies_the_mute_before_retrying(self):
        body = SERVICE[SERVICE.index("private void startOnAir"):]
        body = body[:body.index("private void applyAudio")]
        assert body.index("applyAudio();") < body.rindex("stream.startStream(url);")

    def test_set_muted_is_still_the_mic(self):
        body = SERVICE[SERVICE.index("public void setMuted"):]
        body = body[:body.index("}")]
        assert "audio.setMicMuted(value)" in body and "Screen" not in body


class TestThePluginWiring:
    def test_set_screen_muted_rejects_without_a_service(self):
        body = PLUGIN[PLUGIN.index("public void setScreenMuted"):]
        body = body[:body.index("call.resolve(ret);")]
        assert 'call.reject(' in body.split("svc.setScreenMuted")[0]
        assert 'ret.put("screenAudio", svc.hasScreenAudio())' in body
        assert 'ret.put("muted", svc.isScreenMuted())' in body

    def test_start_carries_the_screen_mute(self):
        assert 'EXTRA_SCREEN_MUTED, Boolean.TRUE.equals(call.getBoolean("screenMuted", false))' in PLUGIN

    def test_status_reports_both_inputs(self):
        assert "public void audioState(PluginCall call)" in PLUGIN
        for k in ('"micMuted"', '"screenAudio"', '"screenMuted"'):
            assert k in PLUGIN
        iss = PLUGIN[PLUGIN.index("public void isStreaming"):]
        assert "audioOf(svc)" in iss[:iss.index("}")]


# ---------------------------------------------------------------- real-library compile (floor)
_GRADLE = os.path.expanduser("~/.gradle/caches/modules-2/files-2.1")


def _first(pattern):
    hits = [h for h in glob.glob(os.path.join(_GRADLE, pattern)) if "sources" not in h]
    return sorted(hits)[0] if hits else None


def _real_classpath(tmp):
    import androidcompile as ac  # noqa: E402  (tests/ is on sys.path under pytest's rootdir)
    jar = ac.android_jar()
    parts = {
        "core": "androidx.core/core/1.*/*/*.aar",
        "activity": "androidx.activity/activity/1.*/*/*.aar",
        "common": "com.*pedroSG94.RootEncoder/common/2.7.2/*/*.aar",
        "rtmp": "com.*pedroSG94.RootEncoder/rtmp/2.7.2/*/*.aar",
        "encoder": "com.*pedroSG94.RootEncoder/encoder/2.7.2/*/*.aar",
        "library": "com.*pedroSG94.RootEncoder/library/2.7.2/*/*.aar",
    }
    cp = [jar] if jar else []
    for k, pat in parts.items():
        aar = _first(pat)
        if not aar:
            return None
        d = os.path.join(tmp, k)
        with zipfile.ZipFile(aar) as z:
            z.extract("classes.jar", d)
        cp.append(os.path.join(d, "classes.jar"))
    kstd = _first("org.jetbrains.kotlin/kotlin-stdlib/2.*/*/kotlin-stdlib-2.*[0-9].jar")
    cap = []
    # A git worktree has no node_modules of its own; the main checkout's Capacitor build is the same jar.
    for base in (ROOT, os.path.expanduser("~/posterchanai")):
        cap = cap or glob.glob(os.path.join(base, "mobile", "node_modules", "@capacitor", "android",
                                            "capacitor", "build", "**", "classes.jar"), recursive=True)
    if not (jar and kstd and cap):
        return None
    return cp + [kstd, cap[0]]


@javac_missing
def test_the_package_compiles_against_the_real_rootencoder():
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    with tempfile.TemporaryDirectory() as tmp:
        cp = _real_classpath(tmp)
        if cp is None:
            pytest.skip("no Gradle cache / Capacitor build here — CI's assembleDebug compiles this")
        stub = os.path.join(tmp, "src", "place", "poster", "app")
        os.makedirs(stub)
        with open(os.path.join(stub, "MainActivity.java"), "w") as fh:
            fh.write("package place.poster.app;\npublic class MainActivity extends android.app.Activity {}\n")
        r = subprocess.run(["javac", "-nowarn", "-d", os.path.join(tmp, "out"), "-cp", os.pathsep.join(cp),
                            os.path.join(stub, "MainActivity.java")]
                           + glob.glob(os.path.join(PKG, "*.java")),
                           capture_output=True, text=True, timeout=300)
        assert r.returncode == 0, r.stdout + r.stderr
