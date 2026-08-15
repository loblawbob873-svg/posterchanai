"""The native QR scanner: wired, named the same on both sides, and never the ONLY way in.

WHY IT EXISTS. The in-app scanner is jsQR decoding a canvas frame scaled to a fixed pixel budget,
and `scripts/check_qr_scan.py` measures it failing on a primal.net-shaped code (v19, 93x93 modules)
below about 40% frame fill — while the phone's own camera app reads the same code off the same
screen every time. Four builds were spent on framing and camera constraints before accepting that
the gap is structural: a native decoder gets the sensor, a WebView gets a downscaled bitmap.

None of this can be driven here — no device, and Gradle only runs on CI — so what is guarded is the
wiring, which is where a Capacitor plugin fails SILENTLY:

  * not registered in MainActivity. A plugin that lives in this app is not auto-discovered, so
    `Capacitor.Plugins.QrScan` is absent, the client's guarded lookup falls through to the old
    scanner, and the fix ships and appears to do nothing.
  * the Java and the JS disagree on the plugin NAME, which fails the same way and just as quietly.
  * the dependency goes missing, and the class does not resolve at build time.
  * the web fallback gets deleted. Then a browser, the desktop build and a cancelled scan have no
    scanner at all — trading a bad scanner for none.
"""
import os
import re

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ANDROID = os.path.join(ROOT, "mobile", "android", "app")
JAVA = os.path.join(ANDROID, "src", "main", "java", "place", "poster", "app")


def _read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as fh:
        return fh.read()


PLUGIN = _read(JAVA, "scan", "QrScanPlugin.java")
MAIN = _read(JAVA, "MainActivity.java")
GRADLE = _read(ANDROID, "build.gradle")
MANIFEST = _read(ANDROID, "src", "main", "AndroidManifest.xml")
APPJS = _read(ROOT, "static", "js", "client", "app.js")


def test_the_plugin_is_registered_or_it_does_not_exist_at_all():
    assert "place.poster.app.scan.QrScanPlugin.class" in MAIN, (
        "QrScanPlugin is not registered in MainActivity — Capacitor will not expose it and the "
        "client silently keeps using the scanner that cannot read these codes"
    )


def test_both_sides_agree_on_the_plugin_name():
    m = re.search(r'@CapacitorPlugin\(name\s*=\s*"([^"]+)"\)', PLUGIN)
    assert m, "QrScanPlugin has no @CapacitorPlugin(name=…)"
    name = m.group(1)
    assert re.search(r"_capPlugin\(\s*'" + re.escape(name) + r"'\s*,\s*'scan'\s*\)", APPJS), (
        f"the client does not look the plugin up under its Java name ({name!r})"
    )


def test_the_decoder_is_bundled_and_needs_nothing_from_play_services():
    assert "com.journeyapps:zxing-android-embedded" in GRADLE, (
        "the native decoder dependency is gone — QrScanPlugin will not compile"
    )
    assert "play-services-code-scanner" not in GRADLE and "mlkit" not in GRADLE.lower(), (
        "ML Kit needs Play Services, which a de-Googled phone does not have — the whole reason "
        "zxing was chosen. If this is deliberate, the fallback story has to be rewritten too."
    )
    assert 'android.permission.CAMERA' in MANIFEST, "no CAMERA permission for the capture activity"


def test_the_web_scanner_is_still_there_and_still_reachable():
    """The native path is an ADDITION. A browser, the desktop build and a cancelled scan need this."""
    body = APPJS[APPJS.index("async function openQrScanner()"):]
    body = body[:body.index("function qrManualPrompt(")]
    assert "getUserMedia" in body, "the camera fallback was removed with the native path added"
    assert "qrManualPrompt" in body, "the paste fallback is gone"
    # The native branch must not `return` on an empty result — that is the user pressing back, and
    # returning there strands them on a screen with no scanner.
    native = body[:body.index("if(!navigator.mediaDevices")]
    assert re.search(r"if\(txt\)\{", native), (
        "the native branch acts on an empty result — backing out of the scanner would then be "
        "treated as a scan rather than falling through to the web scanner"
    )


def test_only_a_pairing_link_is_acted_on():
    """Same rule as the camera path: this screen is 'sign in another device', so a bunker:// read
    here would be the opposite direction and must not be followed."""
    body = APPJS[APPJS.index("async function openQrScanner()"):]
    native = body[:body.index("if(!navigator.mediaDevices")]
    assert "nostrconnect:" in native, "the native branch does not check the scheme"
    assert "bunker" not in native, (
        "the native branch acts on a bunker:// link — that is the direction where somebody else "
        "signs for US, and it has its own screen"
    )
