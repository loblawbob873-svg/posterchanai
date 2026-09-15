"""Keep package-installed kernels selectable without reprovisioning the boot chain."""
from pathlib import Path
import subprocess

import pytest

SOURCE = Path(__file__).resolve().parents[1] / 'os/gentoo.sh'
MID = '0123456789abcdef0123456789abcdef'


def setup(root, token='gentoo', default=None):
    (root / 'etc/kernel').mkdir(parents=True)
    (root / 'boot/loader').mkdir(parents=True)
    (root / 'etc/machine-id').write_text(MID + '\n')
    (root / 'boot/loader/loader.conf').write_text(
        'default ' + (default or MID + '-*') + '\ntimeout 1\n')
    if token is not None:
        (root / 'etc/kernel/entry-token').write_text(token + '\n')


def run(root, stubs=''):
    source = SOURCE.read_text()
    start = source.index('alignKernelEntryToken() {')
    body = source[start:source.index('\nupdateOS() {', start)]
    body = body.replace('/etc/', str(root / 'etc') + '/').replace('/boot/', str(root / 'boot') + '/')
    return subprocess.run(['bash', '-c', stubs + '\n' + body + '\nalignKernelEntryToken'],
                          capture_output=True, text=True, timeout=5)


@pytest.mark.parametrize('token', [None, '', 'gentoo', MID])
def test_stock_entry_token_matches_existing_default(tmp_path, token):
    setup(tmp_path, token)
    loader = (tmp_path / 'boot/loader/loader.conf').read_bytes()
    for _ in range(2):
        result = run(tmp_path)
        assert result.returncode == 0, result.stderr
        assert (tmp_path / 'etc/kernel/entry-token').read_text() == MID + '\n'
        assert (tmp_path / 'boot/loader/loader.conf').read_bytes() == loader
    assert list((tmp_path / 'etc/kernel').iterdir()) == [tmp_path / 'etc/kernel/entry-token']


@pytest.mark.parametrize('token,default', [('custom', MID + '-*'), ('gentoo', 'custom-*')])
def test_operator_configuration_is_preserved(tmp_path, token, default):
    setup(tmp_path, token, default)
    result = run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'etc/kernel/entry-token').read_text() == token + '\n'


@pytest.mark.parametrize('kind', ['symlink', 'directory'])
def test_nonregular_token_is_rejected(tmp_path, kind):
    setup(tmp_path, None)
    token = tmp_path / 'etc/kernel/entry-token'
    target = tmp_path / 'operator-token'
    target.write_text('gentoo\n')
    if kind == 'symlink':
        token.symlink_to(target)
    else:
        token.mkdir()
    result = run(tmp_path)
    assert result.returncode != 0
    assert target.read_text() == 'gentoo\n'
    assert 'non-regular' in result.stderr


def test_failed_atomic_replacement_preserves_original(tmp_path):
    setup(tmp_path)
    result = run(tmp_path, 'mv() { return 23; }')
    assert result.returncode != 0
    assert (tmp_path / 'etc/kernel/entry-token').read_text() == 'gentoo\n'
    assert list((tmp_path / 'etc/kernel').iterdir()) == [tmp_path / 'etc/kernel/entry-token']


def test_invalid_machine_id_cannot_become_boot_path(tmp_path):
    setup(tmp_path)
    (tmp_path / 'etc/machine-id').write_text('../invalid\n')
    result = run(tmp_path)
    assert result.returncode != 0
    assert (tmp_path / 'etc/kernel/entry-token').read_text() == 'gentoo\n'


def test_system_without_systemd_boot_configuration_is_unchanged(tmp_path):
    result = run(tmp_path)
    assert result.returncode == 0, result.stderr
    assert list(tmp_path.iterdir()) == []
