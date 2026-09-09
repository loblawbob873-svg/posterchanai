"""A LiveUSB is a COPY of the machine it was packed from, so Steam has to survive the copy.

`install-live` does not emerge anything -- liveISOinstall writes the squashfs onto the disk -- so
whatever Steam the finished machine has is whatever was packed into the image. That makes Steam
exactly the shape of the Firefox payload bug: a launcher script and a .desktop file are text and
survive anything, while the part that makes Steam RUN is the other ABI. Valve's client is
`ELF 32-bit LSB pie executable, Intel i386` asking for `/lib/ld-linux.so.2`; with no 32-bit loader
and libc it cannot be exec'd at all, and with no 32-bit libGL its games draw nothing.

So there are two halves and this pins both:

  * BEFORE the pack, the build host is asked whether it has `games-util/steam-launcher` at all.
    Not fatal -- Steam is an optional application on this profile and a rescue disc built without
    it is legitimate -- but never silent, because after a half-hour pack it is too late to fix.
  * AFTER the pack, an image built from a host that HAS Steam must contain the payload. That one
    IS fatal: host-has/image-hasn't means it was stripped on the way in.

Each assertion below runs the real bash out of os/gentoo.sh rather than grepping for it.
"""
import os
from pathlib import Path
import subprocess

SOURCE = Path(os.environ.get('PC_STEAM_INSTALLER_SOURCE',
                             Path(__file__).resolve().parents[1] / 'os/gentoo.sh'))

# Every path the image gate demands. The loader and libc are the two without which the 32-bit
# client cannot start; libGL is the one without which it starts and draws nothing.
PAYLOAD = (
    'usr/bin/steam',
    'usr/lib/steam/bin_steam.sh',
    'usr/lib/steam/bootstraplinux_ubuntu12_32.tar.xz',
    'usr/lib/ld-linux.so.2',
    'usr/lib/libc.so.6',
    'usr/lib/libGL.so.1',
)


def _image_gate() -> str:
    """The real `if [[ "$HOST_STEAM" = 1 ]]` block, lifted verbatim from the installer."""
    source = SOURCE.read_text()
    start = source.index('\t\tif [[ "$HOST_STEAM" = 1 ]]; then')
    end = source.index('\n\t\tfi', start) + len('\n\t\tfi')
    return source[start:end]


def _run(block: str, env: dict) -> str:
    result = subprocess.run(['bash', '-c', 'MISSING="";\n' + block + '\nprintf "%s" "$MISSING"'],
                            env={**os.environ, **env}, capture_output=True, text=True, timeout=10)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def test_the_image_gate_reads_the_packed_listing_before_the_verdict():
    """It has to run after the squashfs is listed and before MISSING is judged, or it says nothing."""
    source = SOURCE.read_text()
    gate = source.index('\t\tif [[ "$HOST_STEAM" = 1 ]]; then')
    assert source.index('LS="$(unsquashfs -l') < gate < source.index('if [[ -n "$MISSING" ]]', gate)


def test_every_missing_steam_payload_path_is_named():
    block = _image_gate()
    for absent in (None, *PAYLOAD):
        listing = '\n'.join('squashfs-root/' + p for p in PAYLOAD if p != absent)
        got = _run(block, {'LS': listing, 'HOST_STEAM': '1'})
        assert got == ('/' + absent if absent else ''), (
            f'with {absent} absent the gate reported {got!r}')


def test_a_launcher_without_its_32_bit_runtime_is_still_refused():
    """The failure this exists for: the wrapper and the tarball are there and nothing can run."""
    listing = '\n'.join('squashfs-root/' + p for p in PAYLOAD if not p.startswith('usr/lib/lib')
                        and p != 'usr/lib/ld-linux.so.2')
    got = _run(_image_gate(), {'LS': listing, 'HOST_STEAM': '1'})
    assert got.split() == ['/usr/lib/ld-linux.so.2', '/usr/lib/libc.so.6', '/usr/lib/libGL.so.1']


def test_an_image_from_a_host_without_steam_is_not_refused():
    """A rescue disc built from a machine that never installed Steam is a legitimate image."""
    assert _run(_image_gate(), {'LS': '', 'HOST_STEAM': '0'}) == ''


def _host_check() -> str:
    source = SOURCE.read_text()
    start = source.index('\tlocal HOST_STEAM=0')
    end = source.index('\n\tfi', start) + len('\n\tfi')
    return source[start:end].replace('local ', '')


def test_the_host_is_asked_before_the_pack_and_told_how_to_fix_it(tmp_path):
    """portageq decides it, and a `no` prints the one command that repairs it -- not silence."""
    block = _host_check()
    bin_dir = tmp_path / 'bin'
    bin_dir.mkdir()
    log = tmp_path / 'log'
    log.write_text('')
    for answer, has_steam in (('exit 0', '1'), ('exit 1', '0')):
        (bin_dir / 'portageq').write_text('#!/bin/sh\n' + answer + '\n')
        (bin_dir / 'portageq').chmod(0o755)
        result = subprocess.run(
            ['bash', '-c', f'LOG={log}; COLOR_YELLOW=""; COLOR_RESET="";\n'
                           + block + '\nprintf "HOST_STEAM=%s" "$HOST_STEAM"'],
            env={**os.environ, 'PATH': f'{bin_dir}:{os.environ["PATH"]}'},
            capture_output=True, text=True, timeout=10)
        assert result.returncode == 0, result.stderr
        assert f'HOST_STEAM={has_steam}' in result.stdout
        if has_steam == '0':
            assert 'gentoo.sh steam' in result.stdout, (
                'a host with no Steam is told nothing it can act on')
        else:
            assert 'gentoo.sh steam' not in result.stdout


def test_the_pre_pack_question_comes_before_the_pack():
    """Asked after mksquashfs it is a report on a finished image, not a chance to fix it."""
    source = SOURCE.read_text()
    assert source.index('\tlocal HOST_STEAM=0') < source.index('\tif ! mksquashfs / "$WORK/iso/LiveOS/squashfs.img"')
