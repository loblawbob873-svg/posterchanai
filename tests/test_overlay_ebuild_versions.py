"""EVERY EBUILD IN THE OVERLAY HAS A VERSION PORTAGE CAN READ — or portage silently ignores it.

`app-misc/posterchan-office` shipped as `posterchan-office-1.0.0_p7d478de5.ebuild`. PMS allows only
DIGITS after `_p`, so portage parsed that name as no version at all and skipped the file: `emerge
app-misc/posterchan-office` answered "there are no ebuilds to satisfy", gentoo.sh's package set failed
on the atom and fell back to one-at-a-time (naming it among the failures), and no PosterChanOS machine
ever had the office suite. Office on the desktop then fell through to the instance — "office broken on
desktop: instance membership settings not loaded".

Checked here with the PMS version grammar itself (the CI runners are not Gentoo, so portage is not
importable everywhere), plus: the distfile a `SRC_URI ... -> name` rename produces is the one the
Manifest records, and the session pulls the suite in so `update-posterchan` installs it.
"""
import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
OVERLAY = ROOT / 'os' / 'overlay'
EBUILDS = sorted(OVERLAY.glob('*/*/*.ebuild'))

# PMS 3.2: version = digits(.digits)* letter? (_alpha|_beta|_pre|_rc|_p digits?)* (-r digits)?
VERSION = re.compile(r'^\d+(\.\d+)*[a-z]?(_(alpha|beta|pre|rc|p)\d*)*(-r\d+)?$')


def test_the_overlay_has_ebuilds():
    assert len(EBUILDS) >= 5, EBUILDS


@pytest.mark.parametrize('ebuild', EBUILDS, ids=lambda p: p.name)
def test_every_ebuild_name_is_a_version_portage_reads(ebuild):
    pn = ebuild.parent.name
    stem = ebuild.name[:-len('.ebuild')]
    assert stem.startswith(pn + '-'), f'{ebuild.name} is not named after its directory {pn}'
    pv = stem[len(pn) + 1:]
    assert VERSION.match(pv), f'{ebuild.name}: "{pv}" is not a PMS version — portage ignores this file'


def _expand(text, ebuild):
    pn = ebuild.parent.name
    pvr = ebuild.name[len(pn) + 1:-len('.ebuild')]
    pv = re.sub(r'-r\d+$', '', pvr)
    env = {'PN': pn, 'PV': pv, 'PVR': pvr, 'P': f'{pn}-{pv}', 'PF': f'{pn}-{pvr}'}
    # Each variable's FIRST top-level assignment, quoted or not, may itself name variables (Gentoo's firefox
    # ebuild builds MOZ_P_DISTFILES from MOZ_PN and MOZ_PV_DISTFILES). Later assignments sit inside conditionals
    # (an ESR or _rc branch) and use ${VAR/pat/rep} forms this does not model, so the first one is the default.
    for name, quoted, bare in re.findall(r'^([A-Z_][A-Z0-9_]*)=(?:"([^"]*)"|([^\s"]*))\s*$', ebuild.read_text(), re.M):
        env.setdefault(name, quoted if quoted or not bare else bare)
    sub = lambda t: re.sub(r'\$\{(\w+)\}', lambda m: env.get(m.group(1), m.group(0)), t)
    for _ in range(6):
        text, before = sub(text), text
        if text == before:
            break
    return text


@pytest.mark.parametrize('ebuild', [e for e in EBUILDS if ' -> ' in e.read_text()], ids=lambda p: p.name)
def test_a_renamed_distfile_is_the_one_the_manifest_records(ebuild):
    src = re.search(r'^SRC_URI="([^"]*)"', ebuild.read_text(), re.M | re.S)
    assert src, ebuild
    names = [_expand(n, ebuild) for n in re.findall(r'->\s*(\S+)', src.group(1))]
    manifest = (ebuild.parent / 'Manifest').read_text()
    for name in names:
        assert '${' not in name, f'{ebuild.name}: could not expand {name}'
        assert re.search(r'^DIST ' + re.escape(name) + ' ', manifest, re.M), \
            f'{ebuild.name}: fetches {name}, which the Manifest does not record — the fetch is refused'


def test_the_session_installs_the_office_suite():
    """`update-posterchan` updates the session, and a dependency of the session is what an update
    installs: the ISO's package list alone never reached a machine installed before it."""
    shell = next(OVERLAY.glob('app-misc/posterchanos-shell/*.ebuild')).read_text()
    assert re.search(r'^IUSE="[^"]*\+office\b', shell, re.M), 'office must be a default-on flag'
    assert re.search(r'^\s*office\?\s*\(\s*app-misc/posterchan-office\s*\)', shell, re.M)
    assert list(OVERLAY.glob('app-misc/posterchan-office/*.ebuild'))
