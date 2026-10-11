"""The installer's 'no supported Python' message names THIS distro's package command.

2026-10-10 E2E: ./install.sh --nostr-only on a fresh Debian stopped at "No supported Python found" and told the user
to run `sudo emerge -av dev-lang/python:3.13` -- Gentoo's package manager, on Debian. Runs the shipped helper under
bash for every distro detect.sh can name."""
import os
import subprocess

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SETUP = os.path.join(ROOT, "scripts", "install", "setup.sh")


def _hint(distro):
    script = 'print_error(){ :; }; print_warning(){ :; }; print_step(){ :; }; print_success(){ :; }; ' \
             'source "%s"; DISTRO=%s; python_install_hint' % (SETUP, distro)
    return subprocess.run(["bash", "-c", script], capture_output=True, text=True, timeout=30).stdout


def test_each_distro_gets_its_own_package_command():
    want = {"debian": "apt install", "gentoo": "emerge", "fedora": "dnf install", "arch": "pacman", "suse": "zypper"}
    for distro, cmd in want.items():
        out = _hint(distro)
        assert cmd in out, (distro, out)
        for other, other_cmd in want.items():
            if other != distro:
                assert other_cmd not in out, "%s was told to use %r: %s" % (distro, other_cmd, out)


def test_an_unknown_distro_is_not_given_some_other_distros_command():
    out = _hint("unknown")
    assert out.strip() and not any(c in out for c in ("emerge", "apt install", "dnf", "pacman", "zypper")), out
