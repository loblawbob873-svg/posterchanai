"""The PosterChan ringtone reaches the phone: the Android half of User Settings → Ringtone.

"we need to make it easy somehow for the android launcher users to set that" / "maybe User Settings ->
Ring Tone". RingtonePlugin stores the sound under Ringtones/ (so Android's own picker lists it) and makes it
the default ringtone, asking once for "Modify system settings". Its DECISIONS are pure Java and run here
with javac; the rest is wiring that has failed silently before in this repo -- a plugin never registered, a
permission never declared, an asset the bundle build never copied (static/fonts once went missing exactly
that way) -- so each of those is asserted, and the sound itself is decoded.
"""
import os
import shutil
import subprocess
import tempfile

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANDROID = os.path.join(ROOT, "mobile", "android", "app")
PKG = os.path.join(ANDROID, "src", "main", "java", "place", "poster", "app", "ringtone")
MAIN = open(os.path.join(ANDROID, "src", "main", "java", "place", "poster", "app", "MainActivity.java")).read()
MANIFEST = open(os.path.join(ANDROID, "src", "main", "AndroidManifest.xml")).read()


def _run_rules(body):
    if shutil.which("javac") is None or shutil.which("java") is None:
        pytest.skip("no JDK")
    with tempfile.TemporaryDirectory() as tmp:
        src = os.path.join(tmp, "Driver.java")
        open(src, "w").write("package place.poster.app.ringtone;\npublic class Driver {\n"
                             "  public static void main(String[] a){\n%s\n  }\n}\n" % body)
        out = os.path.join(tmp, "out"); os.makedirs(out)
        c = subprocess.run(["javac", "-nowarn", "-d", out, os.path.join(PKG, "RingtoneRules.java"), src],
                           capture_output=True, text=True, timeout=300)
        assert c.returncode == 0, c.stderr[-2000:]
        r = subprocess.run(["java", "-cp", out, "place.poster.app.ringtone.Driver"], capture_output=True, text=True, timeout=120)
        assert r.returncode == 0, r.stderr[-2000:]
        return r.stdout.strip().split("\n")


def test_a_ringtone_file_name_can_never_become_a_path():
    got = _run_rules('\n'.join(f'System.out.println(RingtoneRules.name({a}, {m}));' for a, m in (
        ('"posterchan-cyberpunk"', '"audio/ogg"'), ('"../../etc/passwd"', '"audio/ogg"'),
        ('"My Tone!.mp3"', '"audio/mpeg"'), ('""', '"audio/ogg"'), ('null', '"video/mp4"'))))
    assert got == ["posterchan-cyberpunk.ogg", "etc-passwd.ogg", "My-Tone.mp3", "posterchan-ringtone.ogg",
                   "posterchan-ringtone.ogg"], got
    assert all("/" not in g and ".." not in g for g in got)


def test_the_outcome_says_what_happened_and_what_is_still_needed():
    got = _run_rules('\n'.join(f'System.out.println(RingtoneRules.outcome({s}, {w}, {d}));' for s, w, d in (
        ("true", "true", "true"), ("true", "false", "true"), ("true", "false", "false"), ("false", "true", "true"))))
    assert got == ["set", "needs-permission", "saved", "failed"], got


def test_the_plugin_is_registered_and_its_permission_declared():
    assert "registerPlugin(place.poster.app.ringtone.RingtonePlugin.class)" in MAIN
    assert 'android.permission.WRITE_SETTINGS' in MANIFEST
    src = open(os.path.join(PKG, "RingtonePlugin.java")).read()
    assert '@CapacitorPlugin(name = "Ringtone")' in src
    assert "ACTION_MANAGE_WRITE_SETTINGS" in src and "setActualDefaultRingtoneUri" in src
    assert "IS_RINGTONE" in src and "RingtoneRules.folder(kind)" in src


def test_both_app_bundles_carry_the_sound_and_it_is_real_audio():
    for script in ("mobile/build-www.sh", "desktop/build-www.sh"):
        assert 'static/sounds/* www/static/sounds/' in open(os.path.join(ROOT, script)).read(), script
    for ext in ("ogg", "mp3", "m4r"):
        path = os.path.join(ROOT, "static", "sounds", "posterchan-cyberpunk." + ext)
        assert os.path.getsize(path) > 100_000, path
        if shutil.which("ffprobe"):
            d = subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                               capture_output=True, text=True).stdout.strip()
            assert 15.5 < float(d) < 16.5, (path, d)
