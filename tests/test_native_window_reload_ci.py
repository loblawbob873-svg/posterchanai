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
