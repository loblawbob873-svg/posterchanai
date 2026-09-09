"""Overlay inputs stay aligned with Gentoo's stable 0.10.1 package."""
import hashlib
import os
from pathlib import Path
import re
import subprocess
import tarfile

import pytest

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / 'os/overlay/gui-wm/wayfire'
PATCH = 'wayfire-0.10.1-preserve-keyboard.patch'
# Existing Gentoo Manifest provenance, not generated from the overlay under test.
INPUT_SHA512 = {
    'wayfire-0.10.0-fix-musl.patch': '19b5e01a218337ecff239a3543ef047a59fc2bfdd42404614144dece910eac7717d3c7b770ea71ec28dcb531a74c5173d87a3042745858c05861853fcc7d3faa',
    'wayfire-session-2': '380708daacc92cf8c94c9fee9c1e374dddb90f83438277c360d904dd34cf30634f90caf9e53ca3511b679f499f0044e35f80516bea1f1bbe8057fae85cad9963',
    'wayfire-session.desktop': '0c7fd2f04c5b2c413bda02f2c43090dc8c64503d372e8eb19df8a4d7190f6ba703db672753bfa0629a2f627b505886c33f874a933cc6cf7f876caf60a4e70039',
    'wayfire.env': '2118195fb4ceb6a994043a4bd5608ee9bb104dd769cf3ffba449b053fa05a6e7464ab9c3f812bd0f9bf4ed73eb7f5e2afa1ee48373765b8429317efa11089cd6',
}


def test_stable_revision_preserves_complete_upstream_ebuild():
    text = (PACKAGE / 'wayfire-0.10.1-r1.ebuild').read_text()
    addition = '\t"${FILESDIR}"/${PN}-0.10.1-preserve-keyboard.patch\n'
    assert text.count(addition) == 1
    original = text.replace(addition, '')
    assert hashlib.sha512(original.encode()).hexdigest() == '1aa29c17014043e9a19f9ef7fe1507664d139dae4b3bdd06e5269afebc9ab858683c2488e5ad722b8767235314ac56006f69e7a70b6f8977f5764c8162ec5c86'
    assert 'gui-wm' in (ROOT / 'os/overlay/profiles/categories').read_text().splitlines()
    assert sorted(p.name for p in PACKAGE.glob('*.ebuild')) == ['wayfire-0.10.1-r1.ebuild']


def test_shell_update_requires_the_fixed_keyboard_package():
    shell = (ROOT / 'os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild').read_text()
    dependencies = shell.split('RDEPEND="', 1)[1].split('"', 1)[0].split()
    assert '>=gui-wm/wayfire-0.10.1-r1' in dependencies
    assert 'gui-wm/wayfire' not in dependencies


@pytest.mark.parametrize('name,digest', INPUT_SHA512.items())
def test_upstream_support_inputs_unchanged(name, digest):
    assert hashlib.sha512((PACKAGE / 'files' / name).read_bytes()).hexdigest() == digest


def test_patch_reference_has_a_real_source_diff_and_no_leftover_placeholder():
    files = {p.name for p in (PACKAGE / 'files').iterdir()}
    assert files == set(INPUT_SHA512) | {PATCH}
    patch = (PACKAGE / 'files' / PATCH).read_text()
    assert re.search(r'^--- a/.+\n\+\+\+ b/.+\n@@', patch, re.M)
    assert 'placeholder' not in patch.lower()
    assert '\n+' in patch and '\n-' in patch


def test_thin_manifest_pins_only_exact_release_archive():
    lines = (PACKAGE / 'Manifest').read_text().splitlines()
    assert len(lines) == 1
    parts = lines[0].split()
    assert parts[:3] == ['DIST', 'wayfire-0.10.1.tar.xz', '941092']
    assert parts[3:] == ['BLAKE2B', '857610f2e4df4f9b1e6a39d10f3d935b8948ac8811723ac376f48afd9376098574a1da8ab22b8507dcc896ca1bb6e32041242853bd0c371473f05022e6e25e14', 'SHA512', '39cca8c57220fac80ce8103e8b0468a108dca16e5a4cacb3c5bdb778e47a31adf3263d77c67ce19cbb026ff5c3300d05b53a2639379bd0d28569161a04c73c32']


def test_release_archive_hashes_and_both_patches_apply(tmp_path):
    archive = os.environ.get('PC_WAYFIRE_RELEASE_ARCHIVE')
    if not archive:
        pytest.skip('PC_WAYFIRE_RELEASE_ARCHIVE must name the upstream release tarball')
    data = Path(archive).read_bytes()
    parts = (PACKAGE / 'Manifest').read_text().split()
    assert len(data) == int(parts[2])
    assert hashlib.blake2b(data).hexdigest() == parts[4]
    assert hashlib.sha512(data).hexdigest() == parts[6]
    with tarfile.open(archive) as tar:
        tar.extractall(tmp_path, filter='data')
    source = tmp_path / 'wayfire-0.10.1'
    for name in [PATCH, 'wayfire-0.10.0-fix-musl.patch']:
        subprocess.run(['patch', '--batch', '--fuzz=0', '-p1', '-i', str(PACKAGE / 'files' / name)], cwd=source, check=True, capture_output=True)
