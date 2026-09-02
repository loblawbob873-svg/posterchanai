"""A POSTERCHANOS MACHINE MUST COME UP AS A DESKTOP, INCLUDING ON A BAD BOOT.

One restart came up as the ordinary single-column client — no launcher, no taskbar, no way to start
a browser. It could not be reproduced by hand, and the honest thing at the time was to record it
rather than change the boot path on a guess (a speculative boot-landing guard has broken a shipped
build here before). This file is the reproduction that was missing.

`restore()` decides synchronously, and `PCOSShell.available()` only answers true once the ASYNC
`detect()` has asked the compositor. Until then the PosterChanOS branch is skipped and the
fall-through applies BOTH the remembered `osMode` and `fits()` — a size check. Driving the shipped
os.js:

    OS · detect answered, wide    desktop=true
    OS · detect answered, narrow  desktop=true     (the shell branch skips the size check)
    OS · detect PENDING, wide     desktop=true     (the remembered preference carries it)
    OS · detect PENDING, NARROW   desktop=FALSE    <- and nothing ever re-decides
    OS · pending, narrow, no pref desktop=FALSE

Both conditions at once is exactly what that teardown produced: a slow compositor socket AND a
window without its real size. Every other combination is fine, which is why it happened once.

The fix asks properly — `detect()` is awaited and the desktop is entered when the answer arrives.
It is additive: `enter()` is idempotent and refuses inside a window, and a browser has no compositor
bridge, so detect resolves false there. The browser rows below are identical before and after, and
that is asserted rather than assumed.
"""
from __future__ import annotations

import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "tests/client/boot_desktop_decision_sim.js"


def run(os_js: Path | None = None) -> dict[str, bool]:
    env = None
    if os_js is not None:
        import os as _os
        env = dict(_os.environ, PC_OS_JS=str(os_js))
    done = subprocess.run(["node", str(SIM)], capture_output=True, text=True, timeout=180,
                          cwd=ROOT, env=env)
    assert done.returncode == 0, done.stderr[-1500:]
    out = {}
    for line in done.stdout.splitlines():
        m = re.match(r"\s*(.+?)\s*->\s*desktop=(true|false)\s*$", line)
        assert m or "ERROR" not in line, line
        if m:
            out[m.group(1).strip()] = m.group(2) == "true"
    assert out, done.stdout
    return out


@pytest.fixture(scope="module")
def now():
    return run()


def test_a_slow_compositor_and_an_unsized_window_still_give_a_desktop(now):
    """THE BUG. Both conditions together, which is what that boot had."""
    assert now["OS · detect PENDING, NARROW"] is True, (
        "the machine boots to the single-column client when detect() has not answered and the "
        "window is not yet at full size — an operating system that can run one program")


def test_a_machine_with_no_remembered_preference_still_gets_a_desktop(now):
    """PosterChanOS has no windowed mode to fall back to; the preference is not consulted there."""
    assert now["OS · pending, narrow, nopref"] is True


@pytest.mark.parametrize("case", ["OS · detect answered, wide", "OS · detect answered, narrow",
                                  "OS · detect PENDING, wide"])
def test_the_paths_that_already_worked_still_work(case, now):
    assert now[case] is True


@pytest.mark.parametrize("case,expect", [
    ("browser · wide, pref on", True),      # an explicit Desktop choice is honoured
    ("browser · wide, no pref", False),     # a first-time visitor sees the client, not a simulation
    ("browser · narrow, pref on", False),   # too small to be usable
])
def test_a_browser_is_completely_unaffected(case, expect, now):
    """The fix must not turn a website into a desktop. A browser has no compositor bridge, so
    detect() resolves false and the new branch does nothing."""
    assert now[case] is expect


def test_this_reproduction_can_fail():
    """MUTATION, against the real pre-fix file rather than a hand-edit — and it must fail ONLY on
    the two OS rows, or the fix changed something it had no business changing."""
    head = subprocess.run(["git", "show", "HEAD:static/js/client/os.js"],
                          cwd=ROOT, capture_output=True, text=True, timeout=60)
    assert head.returncode == 0, head.stderr[-400:]
    import tempfile
    with tempfile.TemporaryDirectory() as td:
        old = Path(td) / "os.js"
        old.write_text(head.stdout, encoding="utf-8")
        before = run(old)
    after = run()
    differ = {k for k in after if before.get(k) != after[k]}
    if not differ:
        pytest.skip("HEAD already carries the fix — the mutation has nothing to compare against")
    assert differ == {"OS · detect PENDING, NARROW", "OS · pending, narrow, nopref"}, (
        f"the fix changed rows it should not have: {sorted(differ)}")
    for row in ("browser · wide, pref on", "browser · wide, no pref", "browser · narrow, pref on"):
        assert before[row] == after[row], f"browser behaviour changed at {row}"
