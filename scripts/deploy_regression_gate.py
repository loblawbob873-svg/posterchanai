#!/usr/bin/env python3
"""Run the required desktop regression checks before pushing or publishing a build.

Uses isolated browser profiles, Electron sessions and fake filesystem/network adapters.
A skipped test is missing coverage, so it blocks this gate just like a failed test.
"""
from pathlib import Path
import os
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
TESTS = (
    'tests/test_deploy_regression_gate.py',
    'tests/test_desktop_tag_readback.py',
    'tests/test_native_window_reload_ci.py',
    'tests/client/test_preview_native_controls.py',
    'tests/client/test_office_close_returns_to_files.py',
    'tests/client/test_office_close_files_full_app.py',
    'tests/client/test_detached_sync_writer.py',
    'tests/client/test_sync_tick.py',
    'tests/test_native_window_reload_electron.py',
)


def run_gate(root=ROOT):
    with tempfile.TemporaryDirectory(prefix='pc-deploy-regressions-') as directory:
        report = Path(directory) / 'results.xml'
        env = dict(os.environ, PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', PC_REQUIRE_NATIVE_IPC_TEST='1')
        # Developer filters and mutation-test overrides must not change what a release tests.
        for name in ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'PC_SYNC_TEST_SOURCE',
                     'PC_OFFICE_TEST_SOURCE', 'PC_OFFLINE_APP_ROOT', 'PC_NATIVE_MAIN_SOURCE'):
            env.pop(name, None)
        command = [sys.executable, '-m', 'pytest', '--noconftest', '-o', 'addopts=',
                   '-q', '-ra', '--junitxml=' + str(report), *TESTS]
        try:
            result = subprocess.run(command, cwd=root, env=env, timeout=180,
                                    capture_output=True, text=True)
        except (OSError, subprocess.TimeoutExpired) as error:
            print('[regressions] ABORT: required tests could not finish: ' + str(error))
            return 1
        print(result.stdout, end='')
        if result.stderr:
            print(result.stderr, file=sys.stderr, end='')
        if result.returncode:
            print('[regressions] ABORT: pytest exited ' + str(result.returncode))
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
        print('[regressions] PASS: ' + str(len(cases)) + ' required cases, none skipped')
        return 0


if __name__ == '__main__':
    raise SystemExit(run_gate())
