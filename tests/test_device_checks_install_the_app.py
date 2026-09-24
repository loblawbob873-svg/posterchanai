"""scripts/android_device_checks.sh must install the APP, never the instrumentation APK.

Both are built under a *debug* path, and `find` lists them in directory order, not name order. On the
run for 1892c1750 the androidTest APK came first: it was installed, the app never was, and the check
failed at launch with "Activity class {place.poster.app/…MainActivity} does not exist" — which blocked
the APK release. The selection line is RUN here against a tree holding only the instrumentation APK,
so the answer does not depend on which order a filesystem happens to return.
"""

import os
import re
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _selection_line():
    src = open(os.path.join(ROOT, "scripts", "android_device_checks.sh"), encoding="utf-8").read()
    m = re.search(r"^APK=\$\(find .*\)$", src, re.M)
    assert m, "the script no longer picks its APK with find"
    return m.group(0)


def _pick(tmp_path, files):
    for f in files:
        p = tmp_path / f
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(b"apk")
    r = subprocess.run(["bash", "-c", _selection_line() + '; printf "%s" "$APK"'],
                       cwd=str(tmp_path), capture_output=True, text=True, timeout=30)
    return r.stdout


TEST_APK = "mobile/android/app/build/outputs/apk/androidTest/debug/app-debug-androidTest.apk"
APP_APK = "mobile/android/app/build/outputs/apk/debug/app-debug.apk"


def test_the_instrumentation_apk_is_never_taken_for_the_app(tmp_path):
    assert _pick(tmp_path, [TEST_APK]) == "", "the androidTest APK would be installed as the app"


def test_the_app_is_found_beside_it(tmp_path):
    assert _pick(tmp_path, [TEST_APK, APP_APK]) == APP_APK
