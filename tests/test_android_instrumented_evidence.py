"""The shipped gate must require fresh executed-test evidence, not merely Gradle success."""
import json
import os
from pathlib import Path
import subprocess

import pytest

ROOT = Path(__file__).resolve().parents[1]
PASS = '<testsuite tests="1" failures="0" errors="0" skipped="0"><testcase classname="Device" name="works"/></testsuite>'


def run_gate(tmp_path, reports, *, gradle_status=0, device=True, source=True, stale=False, gradle_log='', manifests=None):
    android = tmp_path / 'mobile/android'
    android.mkdir(parents=True)
    if source:
        file = android / 'app/src/androidTest/Test.java'
        file.parent.mkdir(parents=True)
        file.write_text('// fixture')
    apk = android / 'app/build/outputs/apk/debug/app-debug.apk'
    apk.parent.mkdir(parents=True)
    apk.write_bytes(b'fixture')
    if stale:
        old = android / 'app/build/outputs/androidTest-results/connected/TEST-stale.xml'
        old.parent.mkdir(parents=True)
        old.write_text(PASS)
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    adb = bindir / 'adb'
    adb.write_text('''#!/bin/sh
case "$1" in
 devices) printf 'List of devices attached\n'; [ "$DEVICE" = 1 ] && printf 'fixture\tdevice\n';;
esac
exit 0
''')
    adb.chmod(0o755)
    gradle = android / 'gradlew'
    gradle.write_text('''#!/usr/bin/env python3
import json, os
from pathlib import Path
Path(os.environ['GRADLE_RAN']).touch()
root=Path('app/build/outputs/androidTest-results/connected')
for name,text in json.loads(os.environ['REPORTS']).items():
    file=root/name;file.parent.mkdir(parents=True,exist_ok=True);file.write_text(text)
for name,text in json.loads(os.environ['MANIFESTS']).items():
    file=Path('app/build/intermediates')/name;file.parent.mkdir(parents=True,exist_ok=True);file.write_text(text)
print(os.environ['GRADLE_LOG'])
raise SystemExit(int(os.environ['GRADLE_STATUS']))
''')
    gradle.chmod(0o755)
    script = tmp_path / 'runner.sh'
    # Existing runner fixtures relocate only artifact paths; all commands and policy remain shipped.
    script.write_text((ROOT / 'scripts/android_instrumented.sh').read_text().replace('/tmp/pc-', str(tmp_path / 'pc-')))
    result = subprocess.run(['bash', str(script)], cwd=tmp_path, capture_output=True, text=True, timeout=15,
        env=os.environ | {'PATH': str(bindir) + os.pathsep + os.environ['PATH'],
            'DEVICE': '1' if device else '0', 'REPORTS': json.dumps(reports),
            'MANIFESTS': json.dumps(manifests or {}),
            'GRADLE_STATUS': str(gradle_status), 'GRADLE_LOG': gradle_log,
            'GRADLE_RAN': str(tmp_path / 'gradle-ran'), 'GITHUB_STEP_SUMMARY': str(tmp_path / 'summary')})
    return result


def test_failed_run_retains_main_and_instrumentation_merged_manifests(tmp_path):
    manifests = {
        'merged_manifests/debug/processDebugManifest/AndroidManifest.xml': '<manifest package="place.poster.app"/>',
        'packaged_manifests/debugAndroidTest/processDebugAndroidTestManifest/AndroidManifest.xml':
            '<manifest package="place.poster.app.test"><application/></manifest>',
    }
    result = run_gate(tmp_path, {}, gradle_status=7, manifests=manifests)
    assert result.returncode == 7, result.stdout + result.stderr
    for name, content in manifests.items():
        captured = tmp_path / 'pc-androidtest/app/build/intermediates' / name
        assert captured.read_text() == content


@pytest.mark.parametrize('reports', [
    {'TEST-one.xml': PASS},
    {'emulator/TEST-one.xml': PASS, 'emulator/TEST-two.xml': PASS.replace('works', 'alsoWorks')},
    {'TEST-one.xml': '<testsuites>' + PASS + '</testsuites>'},
])
def test_real_reports_are_required_and_accepted(tmp_path, reports):
    result = run_gate(tmp_path, reports)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'zero failures/errors/skips' in result.stdout
    assert 'instrumentation verified' in (tmp_path / 'summary').read_text()


@pytest.mark.parametrize('xml', [
    '', '<testsuite>', '<not-junit/>', '<testsuite tests="0"/>',
    '<testsuites tests="2">' + PASS + '</testsuites>',
    '<testsuite tests="1"/>', PASS.replace('tests="1"', 'tests="2"'),
    PASS.replace('tests="1"', 'tests="invalid"'),
    PASS.replace('failures="0"', 'failures="1"'),
    PASS.replace('errors="0"', 'errors="1"'),
    PASS.replace('skipped="0"', 'skipped="1"'),
    PASS.replace('name="works"/>', 'name="works"><failure message="broken"/></testcase>'),
    PASS.replace('name="works"/>', 'name="works"><error/></testcase>'),
    PASS.replace('name="works"/>', 'name="works"><skipped message="no camera"/></testcase>'),
    PASS.replace('name="works"', 'name="works" status="notrun"'),
    PASS.replace('name="works"', ''),
])
def test_gradle_success_cannot_hide_incomplete_or_invalid_results(tmp_path, xml):
    result = run_gate(tmp_path, {'TEST-result.xml': xml})
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'Incomplete Android device coverage' in result.stdout
    assert 'evidence failed' in (tmp_path / 'summary').read_text()


def test_a_passing_report_cannot_hide_a_skipped_report(tmp_path):
    skipped = PASS.replace('name="works"/>', 'name="conditional"><skipped/></testcase>')
    result = run_gate(tmp_path, {'TEST-pass.xml': PASS, 'TEST-skipped.xml': skipped})
    assert result.returncode == 1
    assert 'Device.conditional: skipped' in result.stdout


@pytest.mark.parametrize('stale', [False, True])
def test_missing_fresh_reports_fail_even_with_previous_green_xml(tmp_path, stale):
    result = run_gate(tmp_path, {}, stale=stale)
    assert result.returncode == 1
    assert 'No fresh instrumentation XML' in result.stdout
    assert not list((tmp_path / 'mobile/android/app/build/outputs/androidTest-results').rglob('*.xml'))


@pytest.mark.parametrize('status', [1, 7])
def test_gradle_failure_is_not_overwritten_by_passing_or_missing_evidence(tmp_path, status):
    result = run_gate(tmp_path, {'TEST-result.xml': PASS}, gradle_status=status)
    assert result.returncode == status
    assert 'instrumentation verified' not in result.stdout


@pytest.mark.parametrize('source,device', [(False, True), (True, False)])
def test_no_source_or_device_is_not_a_pass_and_never_runs_gradle(tmp_path, source, device):
    result = run_gate(tmp_path, {}, source=source, device=device)
    assert result.returncode == 2
    assert 'DID NOT RUN' in result.stdout
    assert not (tmp_path / 'gradle-ran').exists()


def test_lost_device_gradle_error_remains_infrastructure_exit_two(tmp_path):
    result = run_gate(tmp_path, {}, gradle_status=1, gradle_log='No connected devices')
    assert result.returncode == 2
    assert 'DID NOT RUN' in result.stdout


GMS_KILL = ('09-24 17:45:44.055   523  2109 I ActivityManager: Killing 5032:place.poster.app/u0a192 (adj 0): '
            'depends on provider com.google.android.gms/.fonts.provider.FontsProvider in dying proc '
            'com.google.android.gms.persistent (adj -10000)')


def run_attempts(tmp_path, attempts):
    """The shipped runner against a device whose instrumented runs go the way `attempts` says: one
    (gradle exit, Play Services killed the app?) per gradle invocation, in order."""
    android = tmp_path / 'mobile/android'
    (android / 'app/src/androidTest').mkdir(parents=True)
    (android / 'app/src/androidTest/Test.java').write_text('// fixture')
    apk = android / 'app/build/outputs/apk/debug/app-debug.apk'
    apk.parent.mkdir(parents=True)
    apk.write_bytes(b'fixture')
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    state = tmp_path / 'attempt'
    (bindir / 'adb').write_text(f'''#!/bin/sh
case "$1" in
 devices) printf 'List of devices attached\\nfixture\\tdevice\\n';;
 logcat)
   case "$*" in
     *" -c"*) : ;;
     *"-v threadtime"*) n=$(cat {state} 2>/dev/null || echo 0)
        python3 -c "import json,sys;a=json.loads(sys.argv[1]);n=int(sys.argv[2]);print(sys.argv[3] if 0<n<=len(a) and a[n-1][1] else '')" '{json.dumps(attempts)}' "$n" '{GMS_KILL}';;
   esac;;
esac
exit 0
''')
    (bindir / 'adb').chmod(0o755)
    gradle = android / 'gradlew'
    gradle.write_text(f'''#!/usr/bin/env python3
import json
from pathlib import Path
state=Path({str(state)!r}); n=int(state.read_text()) + 1 if state.exists() else 1; state.write_text(str(n))
status, killed = json.loads({json.dumps(json.dumps(attempts))})[n-1]
root=Path('app/build/outputs/androidTest-results/connected'); root.mkdir(parents=True, exist_ok=True)
if status == 0:
    (root/'TEST-one.xml').write_text({PASS!r})
print('Instrumentation run failed due to Process crashed.' if killed else '')
raise SystemExit(status)
''')
    gradle.chmod(0o755)
    script = tmp_path / 'runner.sh'
    script.write_text((ROOT / 'scripts/android_instrumented.sh').read_text().replace('/tmp/pc-', str(tmp_path / 'pc-')))
    result = subprocess.run(['bash', str(script)], cwd=tmp_path, capture_output=True, text=True, timeout=30,
        env=os.environ | {'PATH': str(bindir) + os.pathsep + os.environ['PATH'],
                          'GITHUB_STEP_SUMMARY': str(tmp_path / 'summary')})
    return result, int(state.read_text()) if state.exists() else 0


def test_play_services_killing_the_app_once_earns_one_rerun_and_a_real_pass(tmp_path):
    """Measured on run 36035831617: GMS restarted, the ActivityManager killed the app holding its fonts
    provider, and the APK workflow -- which refuses to publish on anything but a device pass -- stopped
    shipping for 17 hours on that one hiccup. The same device is asked once more."""
    result, runs = run_attempts(tmp_path, [[1, True], [0, False]])
    assert runs == 2, result.stdout + result.stderr
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'Instrumented tests re-run' in result.stdout
    assert 'instrumentation verified' in (tmp_path / 'summary').read_text()


def test_play_services_killing_it_twice_is_still_did_not_run(tmp_path):
    result, runs = run_attempts(tmp_path, [[1, True], [1, True]])
    assert runs == 2
    assert result.returncode == 2, result.stdout + result.stderr
    assert 'DID NOT RUN' in result.stdout


def test_a_real_failure_is_never_rerun(tmp_path):
    """Only that one cause earns another attempt: re-running an ordinary failure until it passes is
    how a flaky bug gets shipped."""
    result, runs = run_attempts(tmp_path, [[1, False], [0, False]])
    assert runs == 1, result.stdout
    assert result.returncode == 1
