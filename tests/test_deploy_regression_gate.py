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
                 'PC_OFFICE_TEST_SOURCE', 'PC_OFFLINE_APP_ROOT', 'PC_NATIVE_MAIN_SOURCE',
                 'PC_MMS_SOURCE_ROOT', 'PC_SMS_TEST_SOURCE')
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


@pytest.fixture
def source_gate(tmp_path, monkeypatch):
    import importlib.util
    spec = importlib.util.spec_from_file_location('source_gate', GATE)
    gate = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gate)
    repo = tmp_path / 'repository'
    repo.mkdir()
    def git(*args):
        subprocess.run(['git', *args], cwd=repo, check=True, capture_output=True)
    git('init', '-q')
    git('config', 'user.name', 'Regression Test')
    git('config', 'user.email', 'regression@example.invalid')
    (repo / 'static').mkdir()
    (repo / 'static/code.js').write_text('original code')
    git('add', '.')
    git('commit', '-qm', 'fixture')
    runner = tmp_path / 'runner'
    runner.mkdir()
    (runner / 'pytest.py').write_text('''import os,sys
from pathlib import Path
if os.environ.get('MUTATE_TEST_SOURCE') == 'tracked':
    Path('static/code.js').write_text('code edited during tests')
if os.environ.get('MUTATE_TEST_SOURCE') == 'untracked':
    Path('static/new.js').write_text('new code during tests')
report=next(a.split('=',1)[1] for a in sys.argv if a.startswith('--junitxml='))
Path(report).write_text('<testsuites><testcase name="passing fixture"/></testsuites>')
''')
    monkeypatch.setenv('PYTHONPATH', str(runner))
    return gate, repo, tmp_path / 'receipt.json', git


@pytest.mark.parametrize('mutation', ['tracked', 'untracked'])
def test_source_changed_during_passing_tests_cannot_get_a_receipt(source_gate, monkeypatch, mutation):
    gate, repo, receipt, _ = source_gate
    monkeypatch.setenv('MUTATE_TEST_SOURCE', mutation)
    assert gate.run_gate(repo, receipt) == 1
    assert not receipt.exists()


@pytest.mark.parametrize('mutation', ['tracked', 'untracked', 'commit'])
def test_change_after_tests_invalidates_receipt(source_gate, mutation):
    gate, repo, receipt, git = source_gate
    assert gate.run_gate(repo, receipt) == 0
    assert gate.verify_receipt(receipt, repo) == 0
    if mutation == 'commit':
        git('commit', '--allow-empty', '-qm', 'concurrent work')
    else:
        (repo / ('static/code.js' if mutation == 'tracked' else 'static/new.js')).write_text('later edit')
    assert gate.verify_receipt(receipt, repo) == 1


def test_overlay_pin_update_keeps_client_test_receipt_valid(source_gate):
    gate, repo, receipt, _ = source_gate
    assert gate.run_gate(repo, receipt) == 0
    overlay = repo / 'os/overlay/package.ebuild'
    overlay.parent.mkdir(parents=True)
    overlay.write_text('new verified package pin')
    assert gate.verify_receipt(receipt, repo) == 0


@pytest.mark.parametrize('content', [None, '', '{}', 'null'])
def test_missing_or_invalid_receipt_cannot_authorize_push(source_gate, content):
    gate, repo, receipt, _ = source_gate
    if content is not None:
        receipt.write_text(content)
    assert gate.verify_receipt(receipt, repo) == 1


def test_new_source_used_by_tests_must_be_staged_for_deployment(source_gate):
    gate, repo, receipt, git = source_gate
    (repo / 'static/new.js').write_text('new module required by the app')
    assert gate.run_gate(repo, receipt) == 0
    assert gate.verify_receipt(receipt, repo) == 1
    git('add', 'static/new.js')
    assert gate.run_gate(repo, receipt) == 0
    assert gate.verify_receipt(receipt, repo) == 0


def test_sync_stops_before_commit_when_test_receipt_is_stale(tmp_path):
    (tmp_path / 'sync.sh').write_text((ROOT / 'sync.sh').read_text())
    python = tmp_path / 'venv-unified/bin/python'
    python.parent.mkdir(parents=True)
    python.write_text('''#!/bin/sh
shift  # the gate script
while [ $# -gt 0 ]; do
  case "$1" in
  --receipt) echo tested > "$2"; exit 0 ;;
  --verify) echo stale source; exit 1 ;;
  esac
  shift
done
exit 99
''')
    python.chmod(0o755)
    bindir = tmp_path / 'bin'
    bindir.mkdir()
    git = bindir / 'git'
    git.write_text('''#!/bin/sh
if [ "$1" = rev-parse ]; then echo 1234567890abcdef; exit 0; fi
echo forbidden >> "$SIDE_EFFECT_LOG"
kill -TERM "$PPID"
exit 99
''')
    git.chmod(0o755)
    sentinel = tmp_path / 'side-effects'
    result = subprocess.run(['bash', 'sync.sh'], cwd=tmp_path,
                            env=dict(os.environ, SKIP_LINT='1',
                                     PATH=str(bindir)+':'+os.environ['PATH'], SIDE_EFFECT_LOG=str(sentinel)),
                            capture_output=True, text=True, timeout=10)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'source changed after testing' in result.stdout
    assert not sentinel.exists(), 'stale test results reached a commit or push'


@pytest.mark.parametrize('relative', ['app/routers/calendar.py',
    'os/overlay/gui-libs/posterchan-wayfire-shell/files/posterchan-shell.cpp',
    'os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild'])
def test_backend_or_compositor_edit_invalidates_test_receipt(source_gate, relative):
    gate, repo, receipt, git = source_gate
    backend = repo / relative
    backend.parent.mkdir(parents=True)
    backend.write_text('original calendar handler')
    git('add', relative)
    git('commit', '-qm', 'calendar fixture')
    assert gate.run_gate(repo, receipt) == 0
    backend.write_text('changed after calendar tests passed')
    assert gate.verify_receipt(receipt, repo) == 1


# ---- the full suite ------------------------------------------------------------------------------
# The required list alone let unlisted tests rot for days against shipped code while every deploy
# passed. `--full` runs every discovered file; these drive the shipped gate with a stand-in pytest.

FULL_STANDIN = '''import os,sys
from pathlib import Path
report=next(a.split('=',1)[1] for a in sys.argv if a.startswith('--junitxml='))
ids=[a for a in sys.argv[1:] if '.py' in a and not a.startswith('-')]
files=[a.split('::')[0] for a in ids]
retry=any('::' in a for a in ids)
fail=os.environ.get('FULL_FAIL_FILE','') or os.environ.get('FULL_FLAKY_FILE','')
if os.environ.get('FULL_FLAKY_FILE') and retry:
    fail=''   # alone, the flaky test passes
crash=os.environ.get('FULL_CRASH_FILE','')
full='-o' in sys.argv and '--noconftest' not in sys.argv
if full and crash and crash in files:
    raise SystemExit(2)
cases=''.join('<testcase classname="%s" name="t" file="%s" time="0.5">%s</testcase>'
              % (f[:-3].replace('/','.'), f, '<failure/>' if (full and f==fail) else '') for f in files) \
      or '<testcase name="required"/>'
Path(report).write_text('<testsuites><testsuite>'+cases+'</testsuite></testsuites>')
log=os.environ.get('FULL_SEEN')
if full and log:
    with open(log,'a') as out: out.write('\\n'.join(files)+'\\n')
raise SystemExit(1 if (full and fail in files) else 0)
'''


def _full_env(tmp_path, **extra):
    (tmp_path / 'pytest.py').write_text(FULL_STANDIN)
    return dict(os.environ, PYTHONPATH=str(tmp_path), XDG_CACHE_HOME=str(tmp_path / 'cache'),
                FULL_SEEN=str(tmp_path / 'seen'), **extra)


def test_full_mode_runs_every_discovered_test_file_exactly_once(tmp_path):
    result = subprocess.run([sys.executable, str(GATE), '--full'], env=_full_env(tmp_path),
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    seen = [line for line in (tmp_path / 'seen').read_text().splitlines() if line]
    discovered = sorted(str(p.relative_to(ROOT)) for p in (ROOT / 'tests').rglob('test_*.py')
                        if '__pycache__' not in p.parts)
    assert sorted(seen) == discovered, 'a test file was skipped or run twice'
    assert 'full suite' in result.stdout


def test_a_failure_anywhere_in_the_full_suite_blocks_the_deploy_and_names_it(tmp_path):
    receipt = tmp_path / 'receipt.json'
    victim = 'tests/client/test_a_text_you_can_notice.py'
    result = subprocess.run([sys.executable, str(GATE), '--full', '--receipt', str(receipt)],
                            env=_full_env(tmp_path, FULL_FAIL_FILE=victim),
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'test_a_text_you_can_notice' in result.stdout
    assert not receipt.exists(), 'a failed full suite left a receipt that could authorize a push'


def test_a_shard_that_cannot_complete_blocks_the_deploy(tmp_path):
    result = subprocess.run([sys.executable, str(GATE), '--full'],
                            env=_full_env(tmp_path, FULL_CRASH_FILE='tests/test_deploy_regression_gate.py'),
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 1, result.stdout + result.stderr
    assert 'did not complete' in result.stdout


def test_sync_always_runs_the_full_suite():
    text = (ROOT / 'sync.sh').read_text()
    assert 'deploy_regression_gate.py --full --receipt' in text


def test_shards_cover_every_file_once_and_balance_by_duration(source_gate):
    gate = source_gate[0]
    files = [f'tests/test_{i}.py' for i in range(20)]
    durations = {f: (100.0 if i < 3 else 1.0) for i, f in enumerate(files)}
    shards = gate.plan_shards(files, 3, durations)
    assert sorted(f for shard in shards for f in shard) == sorted(files)
    heavy = [sum(1 for f in shard if durations[f] == 100.0) for shard in shards]
    assert heavy == [1, 1, 1], 'the three slow files were not spread across the shards'


def test_a_test_that_fails_only_under_parallel_load_is_reported_not_blocking(tmp_path):
    victim = 'tests/client/test_a_text_you_can_notice.py'
    result = subprocess.run([sys.executable, str(GATE), '--full'],
                            env=_full_env(tmp_path, FULL_FLAKY_FILE=victim),
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 0, result.stdout + result.stderr
    assert 'FLAKY UNDER LOAD' in result.stdout and 'test_a_text_you_can_notice' in result.stdout


def test_the_full_suite_runs_async_tests_with_their_plugin(source_gate, tmp_path):
    """The required pass sets PYTEST_DISABLE_PLUGIN_AUTOLOAD; handed to the full suite, every
    `@pytest.mark.anyio` test failed and sync.sh could never deploy. Real pytest, real plugin."""
    gate = source_gate[0]
    root = tmp_path / 'suite'
    (root / 'tests').mkdir(parents=True)
    (root / 'tests/test_async.py').write_text(
        'import pytest\n@pytest.fixture\ndef anyio_backend(): return "asyncio"\n'
        '@pytest.mark.anyio\nasync def test_async_runs():\n    assert True\n')
    env = {k: v for k, v in os.environ.items() if k != 'PYTHONPATH'}
    env.update(PYTEST_DISABLE_PLUGIN_AUTOLOAD='1', XDG_CACHE_HOME=str(tmp_path / 'cache'))
    ok, message = gate.run_full_suite(str(root), env, str(tmp_path), jobs=1)
    assert ok, message
