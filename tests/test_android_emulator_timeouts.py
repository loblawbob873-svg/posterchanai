from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WORKFLOW = (ROOT / ".github/workflows/android-emulator.yml").read_text()
DEVICE = (ROOT / "scripts/android_device_checks.sh").read_text()
INSTRUMENTED = (ROOT / "scripts/android_instrumented.sh").read_text()
COMPOSER = (ROOT / "mobile/android/app/src/androidTest/java/place/poster/app/push/ConcordComposerDeviceTest.java").read_text()


def test_lifecycle_and_instrumented_device_checks_have_independent_bounds():
    """A single hung adb/test command must not consume the job timeout and lose diagnostics."""
    assert "timeout --kill-after=30s 15m bash scripts/android_device_checks.sh; a=$?" in WORKFLOW
    assert "timeout --kill-after=30s 25m bash scripts/android_instrumented.sh; b=$?" in WORKFLOW
    assert "device=$a instrumented=$b" in WORKFLOW


def test_device_diagnostics_still_upload_after_a_timeout():
    tail = WORKFLOW.split("timeout --kill-after=30s 15m", 1)[1]
    assert tail.count("if: always()") >= 3
    assert "Upload logcat" in tail
    assert "Upload instrumented test report" in tail
    assert "Upload screenshots" in tail


def test_first_activity_launch_cannot_hang_the_entire_device_gate():
    assert "launch_main()" in DEVICE
    assert "timeout --kill-after=5s 30s adb shell am start -W" in DEVICE
    assert "adb kill-server" in DEVICE and "adb start-server" in DEVICE
    assert "launch failed after ADB restart" in DEVICE


def test_diagnostic_logcat_cannot_hang_after_the_emulator_disconnects():
    assert "timeout --kill-after=5s 20s adb logcat -d" in INSTRUMENTED


def test_composer_focus_precondition_comes_from_native_webview_input():
    """Programmatic JS focus is not a user gesture and Chromium may correctly refuse it."""
    setup = COMPOSER.split('ready.contains("ready-for-touch")', 1)[0]
    assert "MotionEvent.ACTION_DOWN" in COMPOSER and "MotionEvent.ACTION_UP" in COMPOSER
    assert "underTest.dispatchTouchEvent(down)" in COMPOSER
    assert "native tap did not focus the Concord textarea" in COMPOSER
    assert 'data-cc-channel=\\"general\\"' in COMPOSER
    assert "composer stayed hidden" in COMPOSER
    assert "Concord route did not stay active" in COMPOSER
    assert "a.focus()" not in setup
