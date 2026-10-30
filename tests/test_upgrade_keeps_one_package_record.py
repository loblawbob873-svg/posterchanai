"""An upgrade from a newer ISO leaves ONE record per installed package, never the old version's and the new one's.

"we should make sure our gui installer can update the os from a newer iso usb" (2026-10-30). The first GUI upgrade run
showed the upgrade copies the new system over the old one without --delete (on purpose: Wi-Fi, keys, /etc/posterchan
and what the owner installed are kept) -- and that the package database came along twice: the desktop, server, relay,
shell session and PosterFox change version every release, so an upgraded machine recorded both versions of each, and
Portage then treated the current one as an upgrade still to do (reproduced on the build VM). Runs the shipped
pc_dedupe_pkgdb against an old and a new /var/db/pkg.
"""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / 'os' / 'gentoo.sh').read_text()


def _fn():
    start = SRC.index('pc_dedupe_pkgdb() {')
    return SRC[start:SRC.index('\n}\n', start) + 3]


def _pkg(root, cat, pf, slot):
    d = root / 'var/db/pkg' / cat / pf
    d.mkdir(parents=True)
    (d / 'SLOT').write_text(slot + '\n')
    (d / 'PF').write_text(pf + '\n')


def test_an_upgrade_keeps_one_record_per_package_and_nothing_else_changes(tmp_path):
    new, old = tmp_path / 'image', tmp_path / 'disk'
    for root in (new, old):
        (root / 'var/db/pkg').mkdir(parents=True)
    # the image (the newer ISO)
    _pkg(new, 'app-misc', 'posterchan-relay-1.0.20261030224310', '0')
    _pkg(new, 'dev-lang', 'python-3.14.7', '3.14')
    _pkg(new, 'media-fonts', 'font-adobe-100dpi-1.0.4', '0')
    _pkg(new, 'dev-libs', 'icu-78.1', '0/78.1')
    # the machine being upgraded: after the copy it holds the image's records AND its own
    for cat, pf, slot in (('app-misc', 'posterchan-relay-1.0.20261030224310', '0'),
                          ('app-misc', 'posterchan-relay-1.0.20261029000000', '0'),   # the old version: goes
                          ('dev-lang', 'python-3.14.7', '3.14'),
                          ('dev-lang', 'python-3.13.9', '3.13'),                      # another slot: stays
                          ('media-fonts', 'font-adobe-100dpi-1.0.4', '0'),
                          ('media-fonts', 'font-adobe-100dpi-1.0.3', '0'),            # old: goes
                          ('media-fonts', 'font-adobe-75dpi-1.0.3', '0'),             # a different package: stays
                          ('dev-libs', 'icu-78.1', '0/78.1'),
                          ('dev-libs', 'icu-77.1-r1', '0/77.1'),                      # same slot, other subslot: goes
                          ('app-editors', 'vim-9.1.1000', '0')):                      # the owner's own: stays
        _pkg(old, cat, pf, slot)
    stub = tmp_path / 'bin'
    stub.mkdir()
    (stub / 'sudo').write_text('#!/bin/sh\nexec "$@"\n')
    (stub / 'sudo').chmod(0o755)
    r = subprocess.run(['bash', '-c', _fn() + '\npc_dedupe_pkgdb "$1" "$2"', '_', str(new), str(old)],
                       capture_output=True, text=True, env=dict(os.environ, PATH='%s:%s' % (stub, os.environ['PATH'])))
    assert r.returncode == 0, r.stderr
    left = sorted('%s/%s' % (p.parent.name, p.name) for p in (old / 'var/db/pkg').glob('*/*'))
    assert left == ['app-editors/vim-9.1.1000', 'app-misc/posterchan-relay-1.0.20261030224310',
                    'dev-lang/python-3.13.9', 'dev-lang/python-3.14.7', 'dev-libs/icu-78.1',
                    'media-fonts/font-adobe-100dpi-1.0.4', 'media-fonts/font-adobe-75dpi-1.0.3'], left


def test_it_runs_after_every_copy_of_the_system():
    body = SRC[SRC.index('liveISOinstall() {'):]
    body = body[:body.index('\n}\n')]
    assert body.index('pc_dedupe_pkgdb "$ROOTSRC" "$TARGET"') > body.index('The copy did not complete')
