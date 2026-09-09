"""Play Services restarting must not end this app, and must not be read as a test failure.

Measured on CI run 34411867870 — two of five emulator runs went red on
`AppViewsLaunchSmokeTest` with "Instrumentation run failed due to Process crashed" and, until an
unfiltered logcat existed, no cause in any artifact. It was never a crash:

    Killing 4939:place.poster.app (adj 0): depends on provider
    com.google.android.gms/.fonts.provider.FontsProvider
    in dying proc com.google.android.gms.persistent (adj -10000)

androidx.emoji2's startup initializer (arriving transitively through appcompat) asks GMS for a
downloadable font, which binds this process to a provider inside GMS's persistent process. When
that process dies, the ActivityManager kills everything holding one of its providers. On a phone
that is the app vanishing with no crash and nothing in any log whenever Play Services updates.
"""
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
MANIFEST = ROOT / "mobile/android/app/src/main/AndroidManifest.xml"
XML = MANIFEST.read_text(encoding="utf-8")
SCRIPT = (ROOT / "scripts/android_instrumented.sh").read_text(encoding="utf-8")
TOOLS = "{http://schemas.android.com/tools}"
ANDROID = "{http://schemas.android.com/apk/res/android}"


def _startup_provider():
    for provider in ET.fromstring(XML).iter("provider"):
        if provider.get(ANDROID + "name") == "androidx.startup.InitializationProvider":
            return provider
    return None


def test_the_emoji_font_initializer_is_removed_from_the_startup_provider():
    provider = _startup_provider()
    assert provider is not None, (
        "nothing overrides androidx.startup.InitializationProvider, so emoji2's initializer runs "
        "and binds this process to a provider inside com.google.android.gms.persistent")
    removed = [m.get(ANDROID + "name") for m in provider.iter("meta-data")
               if m.get(TOOLS + "node") == "remove"]
    assert "androidx.emoji2.text.EmojiCompatInitializer" in removed, removed


def test_the_override_merges_rather_than_replacing_the_whole_provider():
    """`tools:node="replace"` would drop every OTHER library's initializer with it — androidx
    lifecycle, work-manager and profileinstaller all register here."""
    assert _startup_provider().get(TOOLS + "node") == "merge"


def test_the_manifest_declares_the_tools_namespace_it_uses():
    """Without the xmlns the tools: attributes are inert and the initializer runs anyway — the
    manifest still parses and still builds, which is how this would look fixed and not be."""
    assert 'xmlns:tools="http://schemas.android.com/tools"' in XML


def test_a_gms_induced_kill_is_reported_as_infrastructure_not_as_a_failing_test():
    """Exit 2, the same verdict a missing emulator gets. NOT exit 0: a run that could not ask is
    never a pass. Matched against the LOGCAT, because gradle never sees the reason."""
    # The command that detects it, not the prose explaining it.
    grep = [ln for ln in SCRIPT.splitlines()
            if "grep" in ln and "place" in ln and "gms" in ln and not ln.lstrip().startswith("#")]
    assert grep, "nothing in the runner detects a GMS-induced kill"
    detect = grep[0]
    assert "Killing" in detect, detect
    # It must read the UNFILTERED buffer; the tag-filtered one cannot see an ActivityManager line.
    window = SCRIPT[SCRIPT.index(detect):SCRIPT.index(detect) + 500]
    assert "logcat-instrumented-full.txt" in window, window
    assert 'skip "Play Services died' in window, window
