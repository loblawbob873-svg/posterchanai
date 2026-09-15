"""Run the actual gate with controlled pytest results, and prove sync stops before side effects."""
from pathlib import Path
import os
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
GATE = ROOT / 'scripts/deploy_regression_gate.py'


@pytest.mark.parametrize(('code', 'report', 'passed'), [
    (0, '<testsuites><testsuite><testcase name="works"/></testsuite></testsuites>', True),
    (0, '<testsuites><testsuite><testcase name="needs chrome"><skipped/></testcase></testsuite></testsuites>', False),
    (0, '<testsuites><testsuite><testcase name="broken"><failure/></testcase></testsuite></testsuites>', False),
    (0, '<testsuites><testsuite><testcase name="broken"><error/></testcase></testsuite></testsuites>', False),
    (0, '<testsuites/>', False),
    (0, '<broken', False),
    (0, None, False),
    (1, '<testsuites><testcase name="works"/></testsuites>', False),
    (2, None, False),
    (5, None, False),
])
def test_gate_requires_completed_passing_tests(tmp_path, code, report, passed):
    # This module stands in for pytest, not the gate: invoke the shipped script and
    # let its real subprocess invocation, XML parser and exit status run unchanged.
    (tmp_path / 'pytest.py').write_text(
        'import sys\nfrom pathlib import Path\n'
        'target = next(a.split("=",1)[1] for a in sys.argv if a.startswith("--junitxml="))\n'
        + f'report = {report!r}\n'
        + 'if report is not None: Path(target).write_text(report)\n'
        + f'raise SystemExit({code})\n')
    result = subprocess.run([sys.executable, str(GATE)], env=dict(os.environ, PYTHONPATH=str(tmp_path)),
                            capture_output=True, text=True, timeout=10)
    assert (result.returncode == 0) is passed, result.stdout + result.stderr
    assert ('[regressions] PASS:' in result.stdout) is passed


def test_sync_cannot_push_or_restart_after_a_failed_gate(tmp_path):
    (tmp_path / 'sync.sh').write_text((ROOT / 'sync.sh').read_text())
    python = tmp_path / 'venv-unified/bin/python'
    python.parent.mkdir(parents=True)
    python.write_text('#!/bin/sh\necho "regression runner failed"\nexit 1\n')
    python.chmod(0o755)
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    for name in ('git', 'ssh', 'sudo', 'curl'):
        guard = bin_dir / name
        guard.write_text('#!/bin/sh\necho forbidden >> "$SIDE_EFFECT_LOG"\nkill -TERM "$PPID"\nexit 99\n')
        guard.chmod(0o755)
    sentinel = tmp_path / 'side-effects'
    result = subprocess.run(['bash', 'sync.sh'], cwd=tmp_path,
                            env=dict(os.environ, PATH=str(bin_dir)+':'+os.environ['PATH'],
                                     SIDE_EFFECT_LOG=str(sentinel)),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'required regression checks did not pass' in result.stdout
    assert not sentinel.exists(), 'deployment reached an external operation after failing tests'


def test_gate_clears_filters_and_alternate_source_overrides(tmp_path):
    overrides = ('PYTEST_ADDOPTS', 'PYTEST_PLUGINS', 'PC_SYNC_TEST_SOURCE',
                 'PC_OFFICE_TEST_SOURCE', 'PC_OFFLINE_APP_ROOT', 'PC_NATIVE_MAIN_SOURCE')
    (tmp_path / 'pytest.py').write_text(
        'import os,sys\nfrom pathlib import Path\n'
        + f'assert not any(name in os.environ for name in {overrides!r})\n'
        + 'assert os.environ["PC_REQUIRE_NATIVE_IPC_TEST"] == "1"\n'
        + 'assert "--noconftest" in sys.argv\n'
        + 'target=next(a.split("=",1)[1] for a in sys.argv if a.startswith("--junitxml="))\n'
        + 'Path(target).write_text(\'<testsuites><testcase name="environment"/></testsuites>\')\n')
    env = dict(os.environ, PYTHONPATH=str(tmp_path), **{name:'incorrect-source-or-filter' for name in overrides})
    result = subprocess.run([sys.executable, str(GATE)], env=env, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stdout + result.stderr
