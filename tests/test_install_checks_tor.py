"""install.sh names Tor before the app has to.

Measured on a fresh Debian install (2026-10-29): the installer finished clean, and the app's first start logged
"[TOR] Tor binary not found. Install: apt install tor" -- the dependency check had never mentioned it. Tor is optional,
so the check WARNS with the command for the distribution and never fails the install. The installer's own block, RUN
under bash with `tor` present and absent.
"""
import os
import shutil
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEPS = (ROOT / "scripts/install/deps.sh").read_text()


def _run(tmp_path, have_tor, distro="debian"):
    a = DEPS.index("    # Tor. Optional")
    block = DEPS[a:DEPS.index("    # Check for pax-utils", a)]
    bindir = tmp_path / "bin"
    bindir.mkdir()
    if have_tor:
        (bindir / "tor").write_text("#!/bin/sh\nexit 0\n")
        (bindir / "tor").chmod(0o755)
    script = ('print_success(){ echo "OK $*"; }\nprint_warning(){ echo "WARN $*"; }\nDISTRO=%s\n' % distro) + block
    r = subprocess.run([shutil.which("bash"), "-c", script], capture_output=True, text=True, timeout=10,
                       env={**os.environ, "PATH": f"{bindir}:/usr/bin:/bin" if have_tor else f"{bindir}"})
    return r


def test_missing_tor_is_a_warning_with_the_command(tmp_path):
    r = _run(tmp_path, False)
    assert r.returncode == 0, r.stderr
    assert "WARN tor not found" in r.stdout and "apt install tor" in r.stdout, r.stdout


def test_gentoo_gets_its_own_atom(tmp_path):
    r = _run(tmp_path, False, "gentoo")
    assert "emerge -av net-vpn/tor" in r.stdout, r.stdout


def test_installed_tor_is_reported_found(tmp_path):
    r = _run(tmp_path, True)
    assert "OK tor found" in r.stdout and "WARN" not in r.stdout, r.stdout
