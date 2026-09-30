"""The release gate must fail rather than skip when its runtime is unavailable."""
import importlib.util
from pathlib import Path
import pytest

ROOT = Path(__file__).resolve().parents[1]


def test_required_electron_gate_fails_when_binary_is_missing(monkeypatch, tmp_path):
    spec = importlib.util.spec_from_file_location('native_ipc_gate', ROOT/'tests/test_native_window_reload_electron.py')
    fixture = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(fixture)
    monkeypatch.setenv('PC_REQUIRE_NATIVE_IPC_TEST', '1')
    monkeypatch.setattr(Path, 'is_file', lambda self: False)
    with pytest.raises(pytest.fail.Exception, match='Electron runtime is required'):
        fixture.test_real_electron_sync_reply_survives_native_child_reload(tmp_path)


def test_desktop_linux_runs_required_real_ipc_gate_before_build():
    workflow = (ROOT/'.github/workflows/desktop.yml').read_text()
    start = workflow.index('      - name: Verify required desktop regressions')
    end = workflow.index('      - name: Build', start)
    gate = workflow[start:end]
    assert workflow.index('      - name: Install deps') < start < end
    assert "if: matrix.name == 'linux'" in gate
    assert "PC_REQUIRE_NATIVE_IPC_TEST: '1'" in gate
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD: '1'" in gate
    assert 'continue-on-error' not in gate
    assert 'set -euo pipefail' in gate
    install = workflow[workflow.index('      - name: Install desktop regression packages'):start]
    assert "if: matrix.name == 'linux'" in install and 'xvfb libgtk-3-0t64' in install
    assert 'apt-get' not in gate, "the package install is back inside the tests' own time limit"
    assert 'scripts/deploy_regression_gate.py' in gate
    assert 'scripts/deploy-regression-requirements.txt' in gate
    assert 'tests/test_native_window_reload_electron.py' in (ROOT/'scripts/deploy_regression_gate.py').read_text()
    from tests.test_desktop_workflow_test_triggers import _included, _paths
    assert _included('tests/test_native_window_reload_electron.py', _paths())


@pytest.mark.parametrize('installer_status', [0, 17])
@pytest.mark.parametrize('gate_status', [0, 29])
def test_clean_ci_bootstraps_binary_and_never_runs_gate_after_download_failure(tmp_path, installer_status, gate_status):
    """Run the shipped shell step with fresh npm metadata and controlled OS/bootstrap boundaries."""
    import os
    import subprocess
    import textwrap
    workflow = (ROOT/'.github/workflows/desktop.yml').read_text()
    gate = workflow.split('      - name: Verify required desktop regressions',1)[1].split('      - name: Build',1)[0]
    run = textwrap.dedent(gate.split('        run: |\n',1)[1])
    bindir=tmp_path/'bin';bindir.mkdir()
    package=tmp_path/'desktop/node_modules/electron';package.mkdir(parents=True)
    (package/'install.js').write_text('// npm package installed; executable deliberately absent\n')
    def executable(name, body):
        path=bindir/name;path.write_text('#!/bin/sh\nset -eu\n'+body);path.chmod(0o755)
    executable('sudo', 'exit 0\n')
    executable('node', f'''test "$1" = desktop/node_modules/electron/install.js
exit_code={installer_status}
[ "$exit_code" -eq 0 ] || exit "$exit_code"
mkdir -p desktop/node_modules/electron/dist
printf '#!/bin/sh\\nexit 0\\n' > desktop/node_modules/electron/dist/electron
chmod +x desktop/node_modules/electron/dist/electron
''')
    executable('python3', '''test "$1" = -m && test "$2" = venv
mkdir -p "$3/bin"
printf '#!/bin/sh\\nexit 0\\n' > "$3/bin/pip"
cat > "$3/bin/python" <<'SH'
#!/bin/sh
set -eu
test -x desktop/node_modules/electron/dist/electron
: > pytest-ran
exit "$GATE_STATUS"
SH
chmod +x "$3/bin/pip" "$3/bin/python"
''')
    # Chrome is a controlled OS prerequisite in this bootstrap-only fixture.
    run = 'test(){ if [ "$*" = "-x /opt/google/chrome/chrome" ]; then return 0; else builtin test "$@"; fi; };\n' + run
    result=subprocess.run(['bash','-c',run],cwd=tmp_path,env={**os.environ,'PATH':str(bindir)+os.pathsep+os.environ['PATH'],'RUNNER_TEMP':str(tmp_path/'runner'),'GATE_STATUS':str(gate_status)},text=True,capture_output=True,timeout=10)
    assert result.returncode==(installer_status or gate_status), result.stdout+result.stderr
    assert (tmp_path/'pytest-ran').exists() == (installer_status==0)
    assert "timeout --kill-after=5s 120s node desktop/node_modules/electron/install.js" in run
    assert "node-version: '22'" in workflow


def _install_step():
    workflow = (ROOT/'.github/workflows/desktop.yml').read_text()
    step = workflow.split('      - name: Install desktop regression packages', 1)[1].split('      - name: ', 1)[0]
    import textwrap
    return step, textwrap.dedent(step.split('        run: |\n', 1)[1])


@pytest.mark.parametrize('failures,expect_ok', [(0, True), (2, True), (3, False)])
def test_a_stalled_mirror_is_retried_and_never_charged_to_the_tests(tmp_path, failures, expect_ok):
    """Twice on 2026-09-30 one apt connection stalled and the desktop build failed with no test run.
    RUN the shipped install step against an apt-get that fails `failures` times: it must retry, pass
    apt its download timeouts, and fail loudly only when every attempt failed."""
    import os
    import subprocess
    step, run = _install_step()
    assert 'timeout-minutes:' in step
    bindir = tmp_path/'bin'; bindir.mkdir()
    counter = tmp_path/'count'
    (bindir/'sudo').write_text('#!/bin/sh\nexec "$@"\n'); (bindir/'sudo').chmod(0o755)
    (bindir/'sleep').write_text('#!/bin/sh\nexit 0\n'); (bindir/'sleep').chmod(0o755)
    (bindir/'apt-get').write_text(f"""#!/bin/sh
echo "$@" >> {tmp_path}/args
case "$*" in *" install "*|*" update "*) ;; esac
case "$*" in *install*)
  n=$(cat {counter} 2>/dev/null || echo 0); n=$((n+1)); echo $n > {counter}
  [ $n -gt {failures} ] || exit 100 ;;
esac
exit 0
""")
    (bindir/'apt-get').chmod(0o755)
    res = subprocess.run(['bash', '-c', run], cwd=tmp_path, capture_output=True, text=True, timeout=20,
                         env={**os.environ, 'PATH': str(bindir) + os.pathsep + os.environ['PATH']})
    assert (res.returncode == 0) == expect_ok, res.stdout + res.stderr
    args = (tmp_path/'args').read_text()
    assert 'Acquire::http::Timeout=30' in args and 'Acquire::Retries=5' in args, args
    installs = [l for l in args.splitlines() if ' install -y ' in l]
    assert len(installs) == min(failures + 1, 3), args
    if not expect_ok:
        assert 'no test ran' in res.stdout, res.stdout
