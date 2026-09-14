"""Execute the installer policy functions without changing host Portage configuration."""
from pathlib import Path
import subprocess
import shlex

import pytest

SOURCE = (Path(__file__).resolve().parents[1] / 'os/gentoo.sh').read_text()
POLICY = ('gui-libs/wlroots:0.19 x11-backend vulkan\n'
          'net-libs/gnutls pkcs11 tools\n')


def functions(base):
    start = SOURCE.index('unmaskPackages() {')
    end = SOURCE.index('\nupdateOS() {', start)
    return SOURCE[start:end].replace('/etc/portage', str(base))


def run(base, script, stdin=None):
    return subprocess.run(['bash', '-eu', '-c', functions(base) + '\n' + script],
                          input=stdin, capture_output=True, text=True, timeout=10)


@pytest.mark.parametrize('kind', ['absent', 'file', 'directory'])
def test_refresh_preserves_operator_policy_and_other_managed_flags(tmp_path, kind):
    base = tmp_path / 'portage'
    base.mkdir()
    path = base / 'package.use'
    original = '# operator policy\nmedia-libs/mesa -vulkan\n'
    if kind == 'file':
        path.write_text(original)
    elif kind == 'directory':
        path.mkdir()
        (path / 'local').write_text(original)
        (path / 'posterchan-update-abi').write_text('dev-libs/libffi abi_x86_32\n')
    result = run(base, 'refreshUpdateDependencyPolicy\nrefreshUpdateDependencyPolicy')
    assert result.returncode == 0, result.stderr
    assert (path / 'posterchan-update-deps').read_text() == POLICY
    assert (path / 'posterchan-update-deps').stat().st_mode & 0o777 == 0o644
    if kind != 'absent':
        assert (path / ('00-local' if kind == 'file' else 'local')).read_text() == original
    if kind == 'directory':
        assert (path / 'posterchan-update-abi').read_text() == 'dev-libs/libffi abi_x86_32\n'
    assert not list(path.glob('.posterchan-update-deps.*'))


def test_install_uses_same_dependency_policy(tmp_path):
    result = run(tmp_path, 'SPECIAL_PACKAGE_USE=(); MASKED_PACKAGES=(); '
                 'LICENSED_PACKAGES=(); PINNED_PACKAGES=(); unmaskPackages')
    assert result.returncode == 0, result.stderr
    assert (tmp_path / 'package.use/posterchan-update-deps').read_text() == POLICY


@pytest.mark.parametrize('kind', ['file', 'directory', 'dangling'])
def test_symlinked_configuration_is_rejected_without_changing_target(tmp_path, kind):
    target = tmp_path / 'target'
    if kind == 'file':
        target.write_text('local flags\n')
    elif kind == 'directory':
        target.mkdir()
    (tmp_path / 'package.use').symlink_to(target)
    result = run(tmp_path, 'refreshUpdateDependencyPolicy')
    assert result.returncode != 0
    assert 'symlinked' in result.stderr
    if kind == 'file':
        assert target.read_text() == 'local flags\n'
    elif kind == 'directory':
        assert list(target.iterdir()) == []
    else:
        assert not target.exists()


def test_failed_write_keeps_previous_policy_and_cleans_tempfile(tmp_path):
    path = tmp_path / 'package.use'
    path.mkdir()
    managed = path / 'posterchan-update-deps'
    managed.write_text('old policy\n')
    result = run(tmp_path, 'chmod() { return 73; }; refreshUpdateDependencyPolicy')
    assert result.returncode != 0
    assert managed.read_text() == 'old policy\n'
    assert not list(path.glob('.posterchan-update-deps.*'))


def test_only_skipped_abi32_conflicts_are_returned_sorted_and_once(tmp_path):
    diagnostic = '''media-libs/irrelevant:0
  requires abi_x86_32 before skipped section
WARNING: One or more updates/rebuilds have been skipped due to a dependency conflict:
media-libs/libglvnd:0
  media-libs/libglvnd[abi_x86_32(-)] required by installed mesa
sys-firmware/edk2-bin:0
  ~sys-firmware/edk2-bin-202408 required by qemu
app-misc/another:0
  ABI_X86="(64) -32" incompatible for unrelated reason
media-libs/cairo:0
  >=media-libs/cairo-1.18[abi_x86_32(-)] required by installed pango
media-libs/libglvnd:0
  abi_x86_32 required by second consumer
'''
    result = run(tmp_path, 'skippedABI32Packages', diagnostic)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ['media-libs/cairo', 'media-libs/libglvnd']


@pytest.mark.parametrize('diagnostic', ['', 'All packages are up to date.\n',
    'WARNING: One or more updates/rebuilds have been skipped due to a dependency conflict:\n'
    'sys-firmware/edk2-bin:0\n  ~sys-firmware/edk2-bin-202408 required by qemu\n'])
def test_no_abi_skips_means_no_policy_candidates(tmp_path, diagnostic):
    result = run(tmp_path, 'skippedABI32Packages', diagnostic)
    assert result.returncode == 0, result.stderr
    assert result.stdout == ''


ABI_WARNING = '''WARNING: One or more updates/rebuilds have been skipped due to a dependency conflict:
media-libs/cairo:0
  media-libs/cairo[abi_x86_32(-)] required by installed pango
sys-firmware/edk2-bin:0
  ~sys-firmware/edk2-bin-202408 required by qemu
'''


def prepare(tmp_path, behavior):
    """Use the real function and filesystem; replace only the package manager executable."""
    stub = '''
/usr/bin/emerge() {
  printf '%s\\n' "$*" >> ''' + str(tmp_path / 'calls') + '''
  ''' + behavior + '''
}
prepareUpdateDependencies
'''
    return run(tmp_path, stub)


def test_successful_resolution_with_skips_is_retried_with_persisted_abi_policy(tmp_path):
    path = tmp_path / 'package.use'
    path.mkdir()
    abi = path / 'posterchan-update-abi'
    abi.write_text('dev-libs/libffi abi_x86_32\n')
    result = prepare(tmp_path, '''
if ! grep -q '^media-libs/cairo abi_x86_32$' ''' + shlex.quote(str(abi)) + '''; then
  printf '%s' ''' + shlex.quote(ABI_WARNING) + '''
fi
return 0
''')
    assert result.returncode == 0, result.stderr
    assert abi.read_text() == 'dev-libs/libffi abi_x86_32\nmedia-libs/cairo abi_x86_32\n'
    assert (tmp_path / 'calls').read_text().splitlines() == ['-puDN @world'] * 2
    assert 'sys-firmware/edk2-bin' in result.stdout  # upstream pin remains visible


def test_resolution_failure_never_guesses_flags_even_if_warning_is_present(tmp_path):
    result = prepare(tmp_path, "printf '%s' " + shlex.quote(ABI_WARNING) + '; return 17')
    assert result.returncode == 17
    assert (tmp_path / 'calls').read_text().splitlines() == ['-puDN @world']
    assert not (tmp_path / 'package.use/posterchan-update-abi').exists()


def test_nonconverging_abi_resolution_stops_and_deduplicates_flags(tmp_path):
    result = prepare(tmp_path, "printf '%s' " + shlex.quote(ABI_WARNING) + '; return 0')
    assert result.returncode != 0
    assert 'did not converge' in result.stderr
    assert (tmp_path / 'calls').read_text().splitlines() == ['-puDN @world'] * 4
    assert (tmp_path / 'package.use/posterchan-update-abi').read_text() == 'media-libs/cairo abi_x86_32\n'


def test_unrelated_upstream_pin_does_not_need_abi_policy(tmp_path):
    warning = ABI_WARNING[ABI_WARNING.index('sys-firmware/'):]
    warning = ABI_WARNING.splitlines()[0] + '\n' + warning
    result = prepare(tmp_path, "printf '%s' " + shlex.quote(warning) + '; return 0')
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / 'package.use/posterchan-update-abi').exists()
    assert 'edk2-bin' in result.stdout
    assert (tmp_path / 'calls').read_text().splitlines() == ['-puDN @world']


def test_later_binary_use_warnings_do_not_attribute_abi_to_last_skipped_pin(tmp_path):
    diagnostic = ABI_WARNING + '''
The following packages are causing rebuilds:

  (dev-util/glslang-1.4.350.0-1:0/16.3::gentoo, binary scheduled for merge)

!!! The following binary packages have been ignored due to non matching USE:

    =media-libs/vulkan-loader-1.4.350.0 -abi_x86_32
'''
    result = run(tmp_path, 'skippedABI32Packages', diagnostic)
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ['media-libs/cairo']


@pytest.mark.parametrize('kind', ['symlink', 'directory'])
def test_abi_policy_refuses_nonregular_destination(tmp_path, kind):
    path = tmp_path / 'package.use'
    path.mkdir()
    abi = path / 'posterchan-update-abi'
    if kind == 'directory':
        abi.mkdir()
    else:
        target = tmp_path / 'operator-policy'
        target.write_text('preserve me\n')
        abi.symlink_to(target)
    result = prepare(tmp_path, "printf '%s' " + shlex.quote(ABI_WARNING) + '; return 0')
    assert result.returncode != 0
    assert (tmp_path / 'calls').read_text().splitlines() == ['-puDN @world']
    if kind == 'symlink':
        assert target.read_text() == 'preserve me\n'
    else:
        assert list(abi.iterdir()) == []
