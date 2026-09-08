"""Drive the real lifecycle failure scan; ANR stack capture must never turn failure into success."""
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / 'scripts/android_device_checks.sh'


@pytest.mark.parametrize('case', ['clean', 'anr', 'unavailable', 'service_failure'])
def test_actual_scan_captures_anr_trace_without_changing_verdict(tmp_path, case):
    text = SCRIPT.read_text()
    scan = text[text.index('crash_scan() {'):text.index('\ncrash_scan launch')]
    capture = text[text.index('capture_logcat() {'):text.index('\ncapture_before_teardown()')]
    log = ('E ActivityManager: ANR in place.poster.app (place.poster.app/.MainActivity)\n'
           if case in ('anr', 'unavailable') else
           'E ActivityManager: ForegroundServiceStartNotAllowed\n' if case == 'service_failure' else
           'I ActivityManager: place.poster.app running\n')
    (tmp_path / 'source-log.txt').write_text(log)
    harness = r'''
set -uo pipefail
PKG=place.poster.app;FAILED=0
require_device(){ :; }
fail(){ FAILED=1; }
ok(){ :; }
adb(){
 if [ "$1" = logcat ]; then cat "$OUT/source-log.txt"; return; fi
 if [ "$*" = "shell dumpsys activity lastanr-traces" ]; then
   if [ "$CASE" = unavailable ]; then return 124; fi
   echo '"main" tid=1 Blocked';echo 'at place.poster.app.example.Frame.render(Frame.java:42)';return;
 fi
 return 2
}
timeout(){
 printf '%s\n' "$*" >> "$OUT/timeout-args.txt"
 [ "$1" = --kill-after=2s ] && { [ "$2" = 15s ] || [ "$2" = 20s ]; } || return 2
 shift 2; "$@"
}
CAPTURE
SCAN
crash_scan screen-off
printf '%s' "$FAILED" > "$OUT/verdict.txt"
'''.replace('CAPTURE', capture).replace('SCAN', scan)
    result = subprocess.run(['bash', '-c', harness], env={**os.environ, 'OUT': str(tmp_path), 'CASE': case},
                            capture_output=True, text=True, timeout=5)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'verdict.txt').read_text() == ('0' if case == 'clean' else '1')
    artifact = tmp_path / 'pc-device-anr-screen-off.txt'
    if case == 'anr':
        assert 'Frame.java:42' in artifact.read_text()
    elif case == 'unavailable':
        assert 'unavailable or timed out' in artifact.read_text()
    else:
        assert not artifact.exists()
    if artifact.exists():
        assert '--kill-after=2s 15s adb shell' in (tmp_path / 'timeout-args.txt').read_text()
