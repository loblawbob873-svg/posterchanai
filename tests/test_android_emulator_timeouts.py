from pathlib import Path
import os
import subprocess
import pytest


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
    launch = DEVICE.split("launch_main()", 1)[1].split("\n}", 1)[0]
    assert "am start -W" not in launch
    assert "timeout --kill-after=2s 10s adb shell am start" in launch
    assert "timeout --kill-after=2s 5s adb shell dumpsys activity activities" in launch
    assert "require_device" in launch
    assert 'am start --user 0 -n "$PKG/$PKG.MainActivity" || true' in launch
    assert 'grep -Fq "$PKG/.MainActivity"' in launch
    assert 'grep -Fq "$PKG/$PKG.MainActivity"' in launch
    assert "adb kill-server" in DEVICE and "adb start-server" in DEVICE
    assert "launch failed after ADB restart" in DEVICE


def test_emulator_memory_is_bounded_and_build_daemon_is_gone_before_boot():
    """The AVD and a 1.5 GB Gradle daemon must not compete until the host silently kills QEMU."""
    assert WORKFLOW.count("ram-size: 1536M") == 2
    build = WORKFLOW.split("- name: Build debug APK", 1)[1].split("- name:", 1)[0]
    assert "./gradlew --stop" in build


def test_verdict_boot_never_restores_mutable_cached_snapshot_state():
    assert "key: avd-gles-1536-v2-" in WORKFLOW
    run = WORKFLOW.split("- name: Run the device checks", 1)[1]
    options = run.split("emulator-options:", 1)[1].splitlines()[0]
    assert "-no-snapshot " in options and "-wipe-data " in options
    assert "-no-snapshot-save" not in options


def test_a_disappeared_emulator_ends_lifecycle_diagnostics_promptly():
    assert "require_device()" in DEVICE
    crash_scan = DEVICE.split("crash_scan()", 1)[1].split("}", 1)[0]
    assert "require_device" in crash_scan
    assert "timeout --kill-after=2s 5s adb get-state" in DEVICE


def test_diagnostic_logcat_cannot_hang_after_the_emulator_disconnects():
    assert "timeout --kill-after=5s 20s adb logcat -d" in INSTRUMENTED


def test_composer_focus_precondition_comes_from_native_webview_input():
    """Programmatic JS focus is not a user gesture and Chromium may correctly refuse it."""
    setup = COMPOSER.split('ready.contains("ready-for-touch")', 1)[0]
    assert "MotionEvent.ACTION_DOWN" in COMPOSER and "MotionEvent.ACTION_UP" in COMPOSER
    assert "underTest.dispatchTouchEvent(down)" in COMPOSER
    assert "native tap did not focus the Concord textarea" in COMPOSER
    repaint = COMPOSER.split("native tap did not focus", 1)[1]
    assert "window.__ccDeviceResult='pending';const a=" in repaint
    assert 'data-cc-channel=\\"general\\"' in COMPOSER
    assert "a.closest('.cc-app')" in COMPOSER
    assert "inputs.find(x=>" in COMPOSER
    assert "if(channel){channel.click();setTimeout(seed,100);return;}" in COMPOSER
    assert "opened=true" not in COMPOSER
    assert "composer stayed hidden" in COMPOSER
    assert "Concord route did not stay active" in COMPOSER
    assert "a.focus()" not in setup


def test_emulator_uses_supported_gles_renderer_in_every_boot():
    options=[line for line in WORKFLOW.splitlines() if 'emulator-options:' in line]
    assert len(options)==2
    assert all('-gpu swiftshader -feature -Vulkan' in line for line in options)
    assert 'swiftshader_indirect' not in WORKFLOW


def test_disappeared_emulator_keeps_host_memory_and_kernel_diagnostics():
    assert 'Capture emulator host diagnostics' in WORKFLOW
    assert 'free -m > /tmp/pc-emulator-host.txt' in WORKFLOW
    assert 'sudo dmesg -T' in WORKFLOW
    assert '/tmp/pc-emulator-host.txt' in WORKFLOW.split('- name: Upload logcat',1)[1]


@pytest.mark.parametrize('mode', ['good', 'empty', 'error', 'hang', 'dead'])
def test_lifecycle_crash_scan_requires_a_complete_nonempty_device_capture(tmp_path, mode):
    """Execute the shipped shell functions against healthy, dead and wedged ADB transports."""
    bindir = tmp_path / 'bin'; bindir.mkdir()
    adb = bindir / 'adb'
    adb.write_text('''#!/bin/bash
if [ "$1" = get-state ]; then
  [ "$CAPTURE_MODE" = dead ] && exit 1
  echo device; exit 0
fi
case "$CAPTURE_MODE" in
 good) echo 'I ActivityManager: resumed place.poster.app/.MainActivity';;
 empty) exit 0;;
 error) echo 'adb: device offline' >&2; exit 1;;
 hang) exec sleep 5;;
esac
''')
    adb.chmod(0o755)
    timer = bindir / 'timeout'
    timer.write_text('''#!/bin/bash
echo "$1 $2" >> "$TIMEOUT_LOG"
shift 2
exec /usr/bin/timeout --kill-after=0.05s 0.05s "$@"
''')
    timer.chmod(0o755)
    prelude = DEVICE[:DEVICE.index('\nAPK=')]
    scan = DEVICE[DEVICE.index('crash_scan()'):DEVICE.index('\ncrash_scan launch')]
    script = prelude + '\nOUT="$TEST_OUTPUT"\n' + scan + '\ncrash_scan launch\nexit "$FAILED"\n'
    env = os.environ | {'PATH': str(bindir) + ':' + os.environ['PATH'],
                        'CAPTURE_MODE': mode, 'TEST_OUTPUT': str(tmp_path),
                        'TIMEOUT_LOG': str(tmp_path / 'timeouts')}
    result = subprocess.run(['bash', '-c', script], env=env, text=True,
                            capture_output=True, timeout=3)
    if mode == 'good':
        assert result.returncode == 0, result.stderr
        assert 'ok: no crash during: launch' in result.stdout
    else:
        assert result.returncode != 0
        assert 'ok: no crash' not in result.stdout
        assert ('emulator disappeared' if mode == 'dead' else 'crash checks did not run') in result.stdout
    limits = (tmp_path / 'timeouts').read_text()
    assert '--kill-after=2s 5s' in limits
    if mode != 'dead':
        assert '--kill-after=2s 20s' in limits
        assert (tmp_path / 'pc-device-logcat-launch.txt').exists()
    if mode == 'error':
        assert 'adb: device offline' in (tmp_path / 'pc-device-logcat-launch.txt').read_text()


def test_lifecycle_unbounded_adb_commands_and_exit_diagnostics_are_guarded():
    assert 'adb() { timeout --kill-after=2s 30s adb "$@"; }' in DEVICE
    assert 'timeout --kill-after=2s 120s adb install' in DEVICE
    assert 'trap capture_before_teardown EXIT' in DEVICE
    assert "trap 'exit 143' TERM" in DEVICE
    assert 'pc-device-host-before-teardown.txt' in DEVICE
    assert 'timeout --kill-after=2s 15m adb logcat -v threadtime' in DEVICE
    assert 'capture_logcat full || true' in DEVICE
