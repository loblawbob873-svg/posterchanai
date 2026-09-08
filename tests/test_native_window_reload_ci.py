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
    start = workflow.index('      - name: Verify real native window reload IPC')
    end = workflow.index('      - name: Build', start)
    gate = workflow[start:end]
    assert workflow.index('      - name: Install deps') < start < end
    assert "if: matrix.name == 'linux'" in gate
    assert "PC_REQUIRE_NATIVE_IPC_TEST: '1'" in gate
    assert "PYTEST_DISABLE_PLUGIN_AUTOLOAD: '1'" in gate
    assert 'continue-on-error' not in gate
    assert 'set -euo pipefail' in gate
    assert 'xvfb libgtk-3-0t64' in gate
    assert '-m pytest --noconftest -o addopts= tests/test_native_window_reload_electron.py -q -rA' in gate
    assert "      - 'tests/test_native_window_reload_electron.py'" in workflow


@pytest.mark.parametrize('installer_status', [0, 17])
def test_clean_ci_bootstraps_binary_and_never_runs_gate_after_download_failure(tmp_path, installer_status):
    """Run the shipped shell step with fresh npm metadata and controlled OS/bootstrap boundaries."""
    import os
    import subprocess
    import textwrap
    workflow = (ROOT/'.github/workflows/desktop.yml').read_text()
    gate = workflow.split('      - name: Verify real native window reload IPC',1)[1].split('      - name: Build',1)[0]
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
SH
chmod +x "$3/bin/pip" "$3/bin/python"
''')
    result=subprocess.run(['bash','-c',run],cwd=tmp_path,env={**os.environ,'PATH':str(bindir)+os.pathsep+os.environ['PATH'],'RUNNER_TEMP':str(tmp_path/'runner')},text=True,capture_output=True,timeout=10)
    assert result.returncode==installer_status, result.stdout+result.stderr
    assert (tmp_path/'pytest-ran').exists() == (installer_status==0)
    assert "timeout --kill-after=5s 120s node desktop/node_modules/electron/install.js" in run
    assert "node-version: '22'" in workflow
