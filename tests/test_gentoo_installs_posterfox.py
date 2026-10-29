"""gentoo.sh installs PosterFox -- from its signed binary only -- and makes it the browser of every link.

"remove firefox-bin from the gentoo.sh and replace it with posterfox after all done" (2026-10-28). The installer's own
lines, RUN under bash with `emerge`/`chroot` stubbed, and the pinned key checked with the real gpg: a fingerprint
that does not match the key it sits beside would make every PosterFox package unverifiable on every machine.
"""
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SRC = (ROOT / "os/gentoo.sh").read_text()


def _fn(name):
    a = SRC.index(name + "() {")
    return SRC[a:SRC.index("\n}\n", a) + 3]


def _run_install_browser(tmp_path, posterfox_ok):
    bindir = tmp_path / "usr/bin"
    bindir.mkdir(parents=True)
    (bindir / "emerge").write_text(
        f'#!/bin/sh\necho "$@" >> {tmp_path}/emerge.log\ncase "$*" in *posterfox*) exit {0 if posterfox_ok else 1};; esac\nexit 0\n')
    (bindir / "emerge").chmod(0o755)
    script = _fn("installBrowser").replace("/usr/bin/emerge", str(bindir / "emerge")) + "\ninstallBrowser\n"
    r = subprocess.run([shutil.which("bash"), "-c", script], capture_output=True, text=True, timeout=20)
    return r, (tmp_path / "emerge.log").read_text().splitlines()


def test_posterfox_comes_from_its_binary_and_never_compiles(tmp_path):
    r, calls = _run_install_browser(tmp_path, True)
    assert r.returncode == 0, r.stderr
    assert calls == ["--getbinpkgonly --noreplace www-client/posterfox"], calls


def test_no_binary_still_leaves_a_browser_and_says_so(tmp_path):
    r, calls = _run_install_browser(tmp_path, False)
    assert calls[-1] == "--noreplace www-client/firefox-bin", calls
    assert "installing Firefox instead" in r.stdout and "gentoo.sh browser" in r.stdout


def test_the_browser_is_in_neither_package_set():
    for name in ("BASE_PACKAGES", "POSTERCHANOS_PACKAGES"):
        for m in re.finditer(r'^%s="([^"]*)"' % name, SRC, re.M):
            assert "firefox" not in m.group(1) and "posterfox" not in m.group(1), (name, "a browser in the -uDN set")
    assert "\nelif [ \"$1\" = \"browser\" ]; then\n\tinstallBrowser" in SRC
    assert SRC.index("gentoo.sh browser;") < SRC.index("gentoo.sh steam; then")


def test_the_binhost_is_verified_and_preferred():
    repo = _fn("gentooRepo")
    for line in ('echo "[posterchan]"', 'echo "sync-uri = https://gentoo.poster.place/posterchan-binpkgs"',
                 'echo "verify-signature = true"', 'echo "priority = 10000"'):
        assert line in repo, line
    assert "www-client/posterfox" in SRC[SRC.index("MASKED_PACKAGES+=("):SRC.index("MASKED_PACKAGES+=(www-client/posterfox)") + 40]


@pytest.mark.skipif(not shutil.which("gpg"), reason="gpg required")
def test_the_pinned_key_is_the_pinned_fingerprint(tmp_path):
    fpr = re.search(r'^POSTERCHAN_BINPKG_FPR="([0-9A-F]{40})"', SRC, re.M).group(1)
    key = re.search(r"<<'POSTERCHAN_KEY'\n(.*?)\nPOSTERCHAN_KEY\n", SRC, re.S).group(1)
    (tmp_path / "k.asc").write_text(key + "\n")
    home = tmp_path / "gnupg"
    home.mkdir(mode=0o700)
    out = subprocess.run(["gpg", "--homedir", str(home), "--batch", "--with-colons", "--show-keys", str(tmp_path / "k.asc")],
                         capture_output=True, text=True, timeout=30)
    fprs = [l.split(":")[9] for l in out.stdout.splitlines() if l.startswith("fpr:")]
    assert fprs and fprs[0] == fpr, (fprs, fpr)


def test_trusting_the_key_imports_and_lsigns_it_in_portages_keyring(tmp_path):
    (tmp_path / "etc/portage/gnupg").mkdir(parents=True)
    log = tmp_path / "chroot.log"
    stub = tmp_path / "chroot"
    stub.write_text(f'#!/bin/sh\nshift\necho "$@" >> {log}\n')
    stub.chmod(0o755)
    script = (f'TARGET={tmp_path}\nchroot(){{ {stub} "$@"; }}\n'
              + SRC[SRC.index('POSTERCHAN_BINPKG_FPR="'):SRC.index('\n', SRC.index('POSTERCHAN_BINPKG_FPR="'))] + "\n"
              + _fn("trustPosterchanBinhost") + "trustPosterchanBinhost\n")
    r = subprocess.run([shutil.which("bash"), "-c", script], capture_output=True, text=True, timeout=20)
    assert r.returncode == 0, r.stdout + r.stderr
    calls = log.read_text()
    assert "--import /etc/portage/gnupg/posterchanos-binpkgs.asc" in calls
    assert "--quick-lsign-key 7B690AA70C09AD1AD7748CF5B6F8DA9DDF2EF936" in calls
    assert "BEGIN PGP PUBLIC KEY BLOCK" in (tmp_path / "etc/portage/gnupg/posterchanos-binpkgs.asc").read_text()


def test_the_session_accepts_either_browser_and_the_browser_comes_first():
    """posterchanos-shell used to REQUIRE firefox-bin, so the ISO could not drop it; it accepts PosterFox or firefox-bin
    now. The browser step must run before the shell package is emerged, or Portage would pick PosterFox THROUGH the
    shell's dependency -- outside installBrowser's binary-only guard."""
    eb = next((ROOT / "os/overlay/app-misc/posterchanos-shell").glob("posterchanos-shell-*.ebuild")).read_text()
    assert "|| ( www-client/posterfox www-client/firefox-bin )" in eb
    assert not re.search(r"^\twww-client/firefox-bin$", eb, re.M)
    assert SRC.index("gentoo.sh browser; then") < SRC.index("/usr/bin/gentoo.sh posterchan-shell")
