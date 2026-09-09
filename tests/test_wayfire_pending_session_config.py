"""Execute the shipped config builder as a regular user against root-owned fixtures."""
import os
from pathlib import Path
import subprocess
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture
def setup(tmp_path):
    if os.geteuid() == 0:
        pytest.skip('This regression specifically exercises the unprivileged session')
    if subprocess.run(['sudo', '-n', 'true'], capture_output=True).returncode:
        pytest.skip('Root-owned fixture setup requires passwordless sudo')
    config = tmp_path / 'etc'
    config.mkdir()
    source = config / 'wayfire.ini'
    original = '[core]\nplugins = move resize custom # keep comment\n[command]\nbinding = <super> KEY_F9\ncommand = my-custom-command\n'
    source.write_text(original)
    pending = config / '._cfg0000_wayfire.ini'
    pending.write_text('[core]\nplugins = posterchan-shell view-shot force-fullscreen\n[command]\ncommand = unwanted-package-default\n')
    library = tmp_path / 'lib'
    library.mkdir()
    for name in ('posterchan-shell', 'view-shot'):
        (library / ('lib' + name + '.so')).write_bytes(b'fixture')
    subprocess.run(['sudo', '-n', 'chown', '-R', '0:0', str(config), str(library)], check=True)
    body = (ROOT / 'os/bin/pc-compositor-session').read_text().split("<<'PC_SESSION_CONFIG_PY'\n", 1)[1].split('\nPC_SESSION_CONFIG_PY', 1)[0]
    # Only redirect the installed library paths into the root-owned fixture.
    body = body.replace('"/usr/lib64/wayfire", "/usr/lib/wayfire"', repr(str(library)) + ',')
    def run(explicit=False):
        result = subprocess.run([sys.executable, '-c', body, str(source), str(tmp_path / 'runtime.ini'), 'explicit' if explicit else ''], capture_output=True, text=True)
        assert result.returncode == 0, result.stderr
        assert source.read_text() == original
        assert pending.exists()  # never mv or delete package-manager state
        return (tmp_path / 'runtime.ini').read_text()
    yield config, source, pending, library, run
    subprocess.run(['sudo', '-n', 'chown', '-R', f'{os.getuid()}:{os.getgid()}', str(config), str(library)], check=True)


def test_nonroot_pending_preserves_custom_config_and_loads_only_required_plugins(setup):
    config, source, pending, library, run = setup
    assert source.stat().st_uid == pending.stat().st_uid == 0
    assert not os.access(config, os.W_OK)
    result = run()
    assert 'plugins = move resize custom posterchan-shell view-shot # keep comment' in result
    assert 'command = my-custom-command' in result
    assert 'force-fullscreen' not in result
    assert 'unwanted-package-default' not in result
    assert run() == result  # no duplicate additions on repeat preparation


def test_explicit_override_is_byte_exact(setup):
    _, source, _, _, run = setup
    assert run(True) == source.read_text()


def test_untrusted_pending_is_ignored(setup):
    _, source, pending, _, run = setup
    subprocess.run(['sudo', '-n', 'chown', str(os.getuid()), str(pending)], check=True)
    assert run() == source.read_text()


def test_missing_plugin_binary_does_not_claim_available_method(setup):
    _, _, _, library, run = setup
    subprocess.run(['sudo', '-n', 'rm', str(library / 'libposterchan-shell.so')], check=True)
    result = run()
    assert 'view-shot' in result
    assert 'posterchan-shell' not in result


def test_symlink_pending_is_ignored(setup):
    _, source, pending, _, run = setup
    target = pending.with_name('other.ini')
    subprocess.run(['sudo', '-n', 'mv', str(pending), str(target)], check=True)
    subprocess.run(['sudo', '-n', 'ln', '-s', str(target), str(pending)], check=True)
    assert run() == source.read_text()


def test_mirrored_launcher_is_identical():
    assert (ROOT / 'os/bin/pc-compositor-session').read_bytes() == (ROOT / 'os/overlay/app-misc/posterchanos-shell/files/pc-compositor-session').read_bytes()


def test_plugin_directory_does_not_claim_available_method(setup):
    _, _, _, library, run = setup
    binary = library / 'libposterchan-shell.so'
    subprocess.run(['sudo', '-n', 'rm', str(binary)], check=True)
    subprocess.run(['sudo', '-n', 'mkdir', str(binary)], check=True)
    result = run()
    assert 'view-shot' in result
    assert 'posterchan-shell' not in result
