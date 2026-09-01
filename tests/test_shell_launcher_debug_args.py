"""THE SHELL HAS TO BE OPENABLE WHEN IT WEDGES, OR THE FAULT IS UNFIXABLE BY CONSTRUCTION.

Reported as "my right monitor is still useless, nothing works, like frozen" — with the desktop
still PAINTING (a fresh screenshot showed live widgets), so it is an input/focus fault, not a
crash. Two plausible causes were checked against the machine and BOTH were wrong: the modal
backdrop does not cover the taskbar (`.os-root` is z-index 300, above every modal), and the
renderers were not spinning (CPU after a clean restart measured the same 32%/27% as before it).

What is actually known is written down in the shell already: `pcWM.focus(id)` moves compositor
focus to a native app and takes it from us — "measured on the real machine, `document.hasFocus()`
goes true → false and a `blur` event arrives 1ms later" — and `modal()`'s comment records a stale
browser→renderer focus handshake leaving the next thing you open unable to take a keystroke. When
the wedge was observed, compositor focus was on firefox-bin, not on either shell.

Confirming that needs the renderer's DOM at the moment it happens, and there was no way in:
`pc-shell-start` hardcoded its arguments. Hand-rolling the launcher instead is not an option worth
having — doing that once produced a black screen, because an ssh session has no WAYLAND_DISPLAY and
Electron then falls back to X11 and exits immediately.

So the launcher passes `PC_SHELL_EXTRA_ARGS` through. Empty on every normal boot; nothing about the
default path changes. This file holds that line, and holds it OFF by default: the flag it exists
for is an unauthenticated debugger with access to a session holding the user's keys.
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
#: This helper exists TWICE — os/bin is what a dev edits, the overlay copy is what the ebuild
#: installs — and a fix applied to one of them ships to nobody.
COPIES = {
    "os/bin": ROOT / "os/bin/pc-shell-start",
    "overlay": ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-shell-start",
}


@pytest.mark.parametrize("which", sorted(COPIES))
def test_the_launcher_is_valid_shell(which):
    """It is executed by the compositor's `exec` line, so a syntax error here is a black screen."""
    done = subprocess.run(["sh", "-n", str(COPIES[which])], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr


@pytest.mark.parametrize("which", sorted(COPIES))
def test_every_launch_of_the_shell_takes_the_extra_args(which):
    """BOTH launches — there is a second one in the retry path, and a debugger that attaches only
    when the first attempt happens to work is not much use on a machine that is misbehaving."""
    text = COPIES[which].read_text(encoding="utf-8")
    launches = re.findall(r'"\$PC_DESKTOP_LAUNCHER" --shell[^\n]*', text)
    assert len(launches) == 2, f"expected two launch lines, found {len(launches)}: {launches}"
    for line in launches:
        assert "${PC_SHELL_EXTRA_ARGS:-}" in line, (
            f"this launch cannot be given debug flags, so the fault is only diagnosable if it "
            f"happens to take the other path: {line}")


@pytest.mark.parametrize("which", sorted(COPIES))
def test_it_is_unquoted_on_purpose_and_says_so(which):
    """`"$PC_SHELL_EXTRA_ARGS"` would pass several flags as ONE argument, which Electron rejects.
    Unquoted is deliberate here and needs to survive the next person running shellcheck."""
    text = COPIES[which].read_text(encoding="utf-8")
    assert '"${PC_SHELL_EXTRA_ARGS' not in text, "the passthrough got quoted; multiple flags break"
    assert "SC2086" in text, "no note saying the word-splitting is intended"


@pytest.mark.parametrize("which", sorted(COPIES))
def test_the_debugger_is_not_on_by_default(which):
    """The whole reason this is an env var and not a flag in the file. The port is an
    unauthenticated debugger on loopback, and this session holds the user's keys."""
    text = COPIES[which].read_text(encoding="utf-8")
    launches = re.findall(r'"\$PC_DESKTOP_LAUNCHER" --shell[^\n]*', text)
    for line in launches:
        assert "remote-debugging-port" not in line, (
            "the shell ships with a debugger attached — anything on loopback can then drive the "
            "session, read the key and sign with it")


def test_the_two_copies_have_not_drifted():
    """The trap this repo has paid for before: os/bin is what gets edited, the overlay copy is what
    the ebuild installs, and a change to one of them reaches nobody."""
    a, b = COPIES["os/bin"].read_text(encoding="utf-8"), COPIES["overlay"].read_text(encoding="utf-8")
    assert a == b, "os/bin/pc-shell-start and the overlay copy have diverged"


@pytest.mark.skipif(not shutil.which("sh"), reason="no sh")
def test_the_passthrough_actually_splits_into_separate_arguments():
    """Runs the construct rather than reading it: the failure mode is one argument instead of two,
    and that is invisible in the source."""
    script = 'PC_SHELL_EXTRA_ARGS="--a --b"; set -- --shell ${PC_SHELL_EXTRA_ARGS:-}; echo $#'
    got = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=30)
    assert got.stdout.strip() == "3", got
    empty = subprocess.run(["sh", "-c", 'set -- --shell ${PC_SHELL_EXTRA_ARGS:-}; echo $#'],
                           capture_output=True, text=True, timeout=30)
    assert empty.stdout.strip() == "1", "an unset variable must add no argument at all"
