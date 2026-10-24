"""Android Auto reaches the music player only through a MEDIA BROWSER.

Reported 2026-10-23: "media controls worked before Android Auto took over (ie skip songs)". Over plain
Bluetooth the car's keys are AVRCP commands Android routes to the playing media session, which the app has
always had. Under Android Auto the steering wheel and the car's screen drive AA's own media UI, which
controls one app chosen from those declaring a MediaBrowserService plus the
`com.google.android.gms.car.application` meta-data -- and this app declared neither, so nothing the driver
pressed could reach it.

These are the off-device floor for the deploy gate (it has no emulator), read from the real manifest as
XML. The behaviour itself -- discover, connect, press through the browser's token, the page's audio
answers -- is MusicAndroidAutoDeviceTest on the emulator (scripts/emulator_tests_on_nas.sh, CI).
"""
import os
import re
import xml.etree.ElementTree as ET

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MAIN = os.path.join(ROOT, "mobile", "android", "app", "src", "main")
MANIFEST = os.path.join(MAIN, "AndroidManifest.xml")
SERVICE = os.path.join(MAIN, "java", "place", "poster", "app", "music", "MusicService.java")
DEVICE_TEST = os.path.join(ROOT, "mobile", "android", "app", "src", "androidTest", "java", "place", "poster",
                           "app", "music", "MusicAndroidAutoDeviceTest.java")
A = "{http://schemas.android.com/apk/res/android}"


def _app():
    return ET.parse(MANIFEST).getroot().find("application")


def _music_service():
    for s in _app().findall("service"):
        if s.get(A + "name") == ".music.MusicService":
            return s
    raise AssertionError("MusicService is not declared")


def _code(path):
    with open(path, encoding="utf-8") as f:
        java = f.read()
    java = re.sub(r"/\*.*?\*/", "", java, flags=re.S)
    return re.sub(r"//[^\n]*", "", java)


def test_the_music_service_is_an_exported_media_browser():
    s = _music_service()
    actions = {a.get(A + "name") for f in s.findall("intent-filter") for a in f.findall("action")}
    assert "android.media.browse.MediaBrowserService" in actions, \
        "no MediaBrowserService intent-filter: Android Auto has nothing to bind"
    assert s.get(A + "exported") == "true", "Android Auto is another app; an unexported browser cannot be bound"
    assert re.search(r"class\s+MusicService\s+extends\s+MediaBrowserServiceCompat\b", _code(SERVICE))


def test_android_auto_is_told_this_is_a_media_app():
    metas = {m.get(A + "name"): m.get(A + "resource") for m in _app().findall("meta-data")}
    res = metas.get("com.google.android.gms.car.application")
    assert res and res.startswith("@xml/"), "no com.google.android.gms.car.application meta-data on <application>"
    desc = ET.parse(os.path.join(MAIN, "res", "xml", res[len("@xml/"):] + ".xml")).getroot()
    assert desc.tag == "automotiveApp"
    assert any(u.get("name") == "media" for u in desc.findall("uses"))


def test_the_session_is_handed_to_the_browser_and_answers_its_presses():
    code = _code(SERVICE)
    assert "setSessionToken(session.getSessionToken())" in code
    for cb in ("onPlayFromMediaId", "onPlayFromSearch", "onSkipToNext", "onSkipToPrevious", "onPause", "onPlay"):
        assert re.search(r"public void %s\(" % cb, code), cb
    for act in ("ACTION_PLAY_FROM_MEDIA_ID", "ACTION_SKIP_TO_NEXT", "ACTION_SKIP_TO_PREVIOUS"):
        assert "PlaybackStateCompat." + act in code, act
    assert re.search(r"BrowserRoot\s+onGetRoot\(", code) and re.search(r"void\s+onLoadChildren\(", code)


def test_an_exported_service_ignores_intents_from_other_apps():
    """Exported for Android Auto means any app can startService it; only our own stamped intents count."""
    code = _code(SERVICE)
    m = re.search(r"public int onStartCommand\(.*?\{(.*?)ACTION_DISMISS\.equals", code, flags=re.S)
    assert m, "onStartCommand not found"
    assert "own(intent)" in m.group(1), "the secret check must come before any action is acted on"
    senders = []
    for dirpath, _, files in os.walk(os.path.join(MAIN, "java")):
        for f in files:
            if f.endswith(".java"):
                p = os.path.join(dirpath, f)
                if re.search(r"new Intent\([^)]*MusicService\.class\)", _code(p)) and not p.endswith("MusicService.java"):
                    senders.append(p)
    assert not senders, "intents built without MusicService.intent() are ignored by the service: %s" % senders


def test_the_device_test_exists():
    with open(DEVICE_TEST, encoding="utf-8") as f:
        src = f.read()
    for name in ("androidAutoCanFindTheAppAsAMediaApp", "androidAutoSkipsPausesAndPlaysTheTrack",
                 "musicStartedAfterAndroidAutoConnectedSurvivesAndroidAutoLeaving",
                 "anotherAppCannotDriveTheServiceThroughItsExportedIntents"):
        assert "public void %s(" % name in src, name
