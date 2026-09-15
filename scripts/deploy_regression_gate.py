#!/usr/bin/env python3
"""Run the required desktop regression checks before pushing or publishing a build.

Uses isolated browser profiles, Electron sessions and fake filesystem/network adapters.
A skipped test is missing coverage, so it blocks this gate just like a failed test.
"""
from pathlib import Path
import argparse
import hashlib
import json
import os
import runpy
import signal
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
# Inputs exercised by this gate. The overlay updater may legitimately change its
# package pin after testing; it does not change any of these client/test inputs.
INPUTS = ('app', 'static', 'desktop', 'mobile', 'templates', 'scripts', 'tests',
          'os/overlay/gui-libs/posterchan-wayfire-shell',
          'os/overlay/app-misc/posterchanos-shell',
          '.github/workflows/desktop.yml', '.github/workflows/android.yml',
          '.github/workflows/android-emulator.yml', 'sync.sh')
TESTS = (
    'tests/test_deploy_regression_gate.py',
    'tests/test_desktop_workflow_test_triggers.py',
    'tests/test_deploy_process_cleanup.py',
    'tests/test_sync_publish_failures.py',
    'tests/test_sync_nas_fetch_retry.py',
    'tests/test_wayfire_pointer_confinement_runtime.py::test_the_option_is_declared_on_and_the_package_that_carries_it_is_required',
    'tests/test_android_mms_draft_copy_ownership.py',
    'tests/test_android_mms_receiver_lifecycle.py',
    'tests/test_android_mms_result_mapping.py',
    'tests/test_android_mms_retry_runtime.py',
    'tests/test_android_sms_share_runtime.py',
    'tests/test_android_sms_share_caption.py',
    'tests/test_android_composer_belongs_to_the_conversation.py',
    'tests/test_android_instrumented_evidence.py',
    'tests/test_android_publish_gate.py',
    'tests/test_reminder_history_window.py',
    'tests/test_reminder_notifications.py',
    'tests/client/test_reminder_notifications_runtime.py',
    'tests/client/test_reminder_cache_expiry.py',
    'tests/client/test_notification_author_route_full_app.py',
    'tests/client/test_desktop_notification_history_full_app.py',
    'tests/client/test_android_early_launcher_full_app.py',
    'tests/test_calendar_move_api.py',
    'tests/client/test_calendar_default_and_move_full_app.py',
    'tests/client/test_files_follow_theme.py',
    'tests/client/test_browser_startup_diagnostics.py',
    'tests/client/test_browser_devtools_readiness.py',
    'tests/client/test_desktop_offline_full_app.py::test_failed_browser_check_still_closes_chrome_before_removing_profile',
    'tests/client/test_desktop_offline_full_app.py::test_start_keyboard_result_survives_refresh_and_has_visible_focus',
    'tests/client/test_saved_theme_reaches_open_files.py',
    'tests/client/test_dm_delivery.py',
    'tests/client/test_sms_live_notifications.py',
    'tests/client/test_sms_notification_routes.py',
    'tests/client/test_sms_notification_route_full_app.py',
    'tests/test_desktop_tag_readback.py',
    'tests/test_native_window_reload_ci.py',
    'tests/client/test_preview_native_controls.py',
    'tests/client/test_office_close_returns_to_files.py',
    'tests/client/test_office_close_files_full_app.py',
    'tests/client/test_detached_sync_writer.py',
    'tests/client/test_sync_tick.py',
    'tests/test_native_window_reload_electron.py',
)


def source_fingerprint(root):
    def git(*args):
        return subprocess.run(['git', *args], cwd=root, check=True, capture_output=True,
                              timeout=30).stdout
    digest = hashlib.sha256(git('rev-parse', '--verify', 'HEAD'))
    digest.update(git('diff', '--no-ext-diff', '--no-textconv', '--binary', 'HEAD', '--', *INPUTS))
    # git diff omits new, unstaged source files. Include them without following symlinks.
    for name in sorted(git('ls-files', '--others', '--exclude-standard', '-z', '--', *INPUTS).split(b'\0')):
        if not name:
            continue
        path = Path(root) / os.fsdecode(name)
        digest.update(b'\0' + name + b'\0')
        content = os.fsencode(os.readlink(path)) if path.is_symlink() else path.read_bytes()
        digest.update(hashlib.sha256(content).digest())
    return digest.hexdigest()


def verify_receipt(path, root=ROOT):
    try:
        expected = json.loads(Path(path).read_text())['fingerprint']
        if expected != source_fingerprint(root):
            raise ValueError('tested source changed; rerun deployment checks')
        unstaged = subprocess.run(['git', 'ls-files', '--others', '--exclude-standard', '-z',
                                   '--', *INPUTS], cwd=root, check=True, capture_output=True,
                                  timeout=30).stdout
        if unstaged:
            raise ValueError('stage new source files and rerun checks before deploying; git commit -a omits them')
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as error:
        print('[regressions] ABORT: cannot verify tested source: ' + str(error))
        return 1
    print('[regressions] tested source is unchanged')
    return 0



def _run_required_tests(command, root, env, log, timeout=180):
    # Share the suite runner's owned process-group cleanup and file capture. Pipes
    # can stay open in orphaned browsers after pytest exits or is interrupted.
    captured = runpy.run_path(str(Path(__file__).with_name('checkall.py')))['_captured']
    previous = signal.getsignal(signal.SIGTERM)
    def interrupted(_signum, _frame):
        raise KeyboardInterrupt
    signal.signal(signal.SIGTERM, interrupted)
    try:
        return captured(command, root, dict(env, PC_GATE_MANAGED_PROCESSES='1'), timeout, log)
    except KeyboardInterrupt:
        return 130, '[regressions] required tests interrupted\n'
    finally:
        signal.signal(signal.SIGTERM, previous)


def run_gate(root=ROOT, receipt=None):
    try:
        before = source_fingerprint(root)
    except (OSError, subprocess.SubprocessError) as error:
        print('[regressions] ABORT: cannot identify source under test: ' + str(error))
        return 1
    with tempfile.TemporaryDirectory(prefix='pc-deploy-regressions-') as directory:
        report = Path(directory) / 'results.xml'
        env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PC_REQUIRE_NATIVE_IPC_TEST='1')
        # Developer filters and mutation-test overrides must not change what a release tests.
        for name in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'PC_SYNC_TEST_SOURCE',
                     'PC_OFFICE_TEST_SOURCE', 'PC_OFFLINE_APP_ROOT', 'PC_NATIVE_MAIN_SOURCE',
                     'PC_MMS_SOURCE_ROOT', 'PC_SMS_TEST_SOURCE'):
            env.pop(name, None)
        command = [sys.executable, '-m', 'pytest', '--noconftest', '-o', 'addopts=',
                   '-q', '-ra', '--junitxml=' + str(report), *TESTS]
        try:
            code, output = _run_required_tests(command, root, env, Path(directory) / 'pytest.log')
        except (OSError, subprocess.TimeoutExpired) as error:
            print('[regressions] ABORT: required tests could not finish: ' + str(error))
            return 1
        print(output, end='')
        if code:
            print('[regressions] ABORT: pytest exited ' + str(code))
            return 1
        try:
            cases = list(ET.parse(report).getroot().iter('testcase'))
        except (OSError, ET.ParseError) as error:
            print('[regressions] ABORT: missing or invalid test results: ' + str(error))
            return 1
        if not cases:
            print('[regressions] ABORT: no test cases ran')
            return 1
        incomplete = [case for case in cases if any(case.find(tag) is not None
                                                   for tag in ('skipped', 'failure', 'error'))]
        if incomplete:
            names = ', '.join(case.get('name', '?') for case in incomplete)
            print('[regressions] ABORT: required coverage failed or was skipped: ' + names)
            return 1
        try:
            if before != source_fingerprint(root):
                raise ValueError('source changed while tests were running; rerun deployment checks')
            if receipt:
                Path(receipt).write_text(json.dumps({'fingerprint': before}))
        except (OSError, ValueError, subprocess.SubprocessError) as error:
            print('[regressions] ABORT: ' + str(error))
            return 1
        print('[regressions] PASS: ' + str(len(cases)) + ' required cases, none skipped')
        return 0


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    action = parser.add_mutually_exclusive_group()
    action.add_argument('--receipt', help='write a successful source fingerprint for this deploy')
    action.add_argument('--verify', help='check a receipt immediately before committing')
    args = parser.parse_args()
    raise SystemExit(verify_receipt(args.verify) if args.verify else run_gate(receipt=args.receipt))
