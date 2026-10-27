"""`gentoo.sh <a command it does not know>` is an error, never the interactive installer menu.

Found 2026-10-27 by the Secure Boot test: on an install from the 20261019 ISO -- whose gentoo.sh predates
`anticheat` -- `sudo gentoo.sh anticheat` opened "POSTERCHANOS INSTALLER ... Your Choice:". With no terminal on
stdin the menu recursed until bash segfaulted (exit 139). Runs the SHIPPED script.
"""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run(*args, tmp):
    env = {"PATH": "/usr/bin:/bin", "HOME": str(tmp), "TERM": "dumb"}
    return subprocess.run(["bash", str(ROOT / "os/gentoo.sh"), *args], stdin=subprocess.DEVNULL,
                          capture_output=True, text=True, timeout=60, env=env)


def test_an_unknown_command_says_so_and_never_opens_the_menu(tmp_path):
    r = _run("no-such-command", tmp=tmp_path)
    assert r.returncode == 2, (r.returncode, r.stdout[-400:], r.stderr[-400:])
    assert "unknown command 'no-such-command'" in r.stderr and "update-posterchan" in r.stderr, r.stderr
    assert "Your Choice" not in r.stdout + r.stderr, "the installer menu opened for an unknown command"


def test_every_command_the_repo_invokes_is_one_the_script_has():
    """The other half: nothing we ship calls a command that would now be refused."""
    import re
    src = (ROOT / "os/gentoo.sh").read_text()
    known = set(re.findall(r'^(?:if|elif) \[ "\$1" = "([a-z0-9-]+)" \]', src, re.M))
    called = set()
    for p in [*ROOT.glob("os/bin/*"), *ROOT.glob("desktop/*.js"), ROOT / "scripts/publish_overlay.sh"]:
        if p.is_file():
            t = p.read_text(errors="replace")
            called |= set(re.findall(r"""(?:sudo|exec|bash)\s+(?:\S*/)?gentoo\.sh\s+["']?([a-z][a-z0-9-]+)""", t))
            called |= set(re.findall(r"""gentoo\.sh["'],\s*\[?["']([a-z][a-z0-9-]+)""", t))
    assert called, "found no invocations -- the scan is broken"
    assert called <= known, ("invoked but not a gentoo.sh command", sorted(called - known))
