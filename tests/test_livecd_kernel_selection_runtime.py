"""Execute the ISO's kernel selection against a private boot/modules filesystem."""
import os
from pathlib import Path
import subprocess

import pytest


SOURCE = Path(__file__).resolve().parents[1] / "os/gentoo.sh"
RUNNING = "6.18.43-gentoo-dist"
NEWER = "6.18.48-gentoo-dist"
UNSET = object()


def select(tmp_path, *, requested=UNSET, kernels=(), modules=(), image_modules=None):
    source = SOURCE.read_text()
    start = source.index('\tlocal KVER KERNEL\n', source.index('liveCD()'))
    end = source.index('\n\techo -e "${COLOR_YELLOW}Kernel:', start)
    block = source[start:end]
    # Only redirect host filesystem paths; squashfs listing paths remain image-relative.
    block = block.replace('/boot', str(tmp_path / 'boot'))
    for path in ('/lib/modules', '/usr/lib/modules'):
        block = block.replace('"' + path, '"' + str(tmp_path) + path)
        block = block.replace('find ' + path, 'find ' + str(tmp_path) + path)
    for version in modules:
        (tmp_path / 'lib/modules' / version).mkdir(parents=True)
    for version, layout in kernels:
        path = {'bls': f'boot/machine/{version}/linux',
                'classic': f'boot/vmlinuz-{version}',
                'modules': f'lib/modules/{version}/vmlinuz'}[layout]
        target = tmp_path / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(version)
    env = os.environ.copy()
    env.pop('PC_ISO_KERNEL', None)
    if requested is not UNSET:
        env['PC_ISO_KERNEL'] = requested
    image_modules = modules if image_modules is None else image_modules
    env['LS'] = '\n'.join(['squashfs-root/usr/lib/firmware'] + [
        f'squashfs-root/usr/lib/modules/{v}/kernel' for v in image_modules])
    script = '''
uname() { echo "6.18.43-gentoo-dist"; }
unsquashfs() { :; }
_lcd_fail() { echo "FAILED: $*"; }
LOG=/dev/null
select_kernel() {
''' + block + '\nprintf "SELECTED=%s\\n" "$KVER"\n}\nselect_kernel\n'
    return subprocess.run(['bash', '-c', script], env=env, text=True,
                          capture_output=True, timeout=5)


@pytest.mark.parametrize('layout', ['bls', 'classic', 'modules'])
def test_explicit_newer_kernel_without_reboot(tmp_path, layout):
    result = select(tmp_path, requested=NEWER,
                    kernels=[(RUNNING, 'bls'), (NEWER, layout)], modules=[RUNNING, NEWER])
    assert result.returncode == 0, result.stdout + result.stderr
    assert f'SELECTED={NEWER}' in result.stdout


def test_default_still_prefers_running_kernel(tmp_path):
    result = select(tmp_path, kernels=[(RUNNING, 'bls'), (NEWER, 'bls')],
                    modules=[RUNNING, NEWER])
    assert result.returncode == 0, result.stdout + result.stderr
    assert f'SELECTED={RUNNING}' in result.stdout


def test_default_falls_back_to_newest_matched_pair(tmp_path):
    result = select(tmp_path, kernels=[(NEWER, 'classic')], modules=[RUNNING, NEWER])
    assert result.returncode == 0, result.stdout + result.stderr
    assert f'SELECTED={NEWER}' in result.stdout


@pytest.mark.parametrize('requested', ['', '../boot/linux', '*', '6.18; echo bad', '6.18\n48'])
def test_invalid_explicit_version_fails(tmp_path, requested):
    result = select(tmp_path, requested=requested, kernels=[(RUNNING, 'bls')], modules=[RUNNING])
    assert result.returncode != 0
    assert 'Invalid PC_ISO_KERNEL' in result.stdout
    assert 'SELECTED=' not in result.stdout


def test_explicit_missing_kernel_does_not_fall_back(tmp_path):
    result = select(tmp_path, requested=NEWER, kernels=[(RUNNING, 'bls')],
                    modules=[RUNNING, NEWER])
    assert result.returncode != 0
    assert 'No kernel found' in result.stdout
    assert 'SELECTED=' not in result.stdout


def test_explicit_kernel_requires_installed_modules(tmp_path):
    result = select(tmp_path, requested=NEWER, kernels=[(NEWER, 'bls')], modules=[RUNNING])
    assert result.returncode != 0
    assert 'no matching' in result.stdout


def test_explicit_classic_name_cannot_match_different_version_suffix(tmp_path):
    result = select(tmp_path, requested=NEWER, kernels=[(NEWER + '-other', 'classic')],
                    modules=[NEWER])
    assert result.returncode != 0
    assert 'No kernel found' in result.stdout


@pytest.mark.parametrize('image_modules', [[], ['6x18x48-gentoo-dist']])
def test_selected_modules_must_be_in_image_exactly(tmp_path, image_modules):
    result = select(tmp_path, requested=NEWER, kernels=[(NEWER, 'bls')],
                    modules=[NEWER], image_modules=image_modules)
    assert result.returncode != 0
    assert 'image does not contain' in result.stdout
