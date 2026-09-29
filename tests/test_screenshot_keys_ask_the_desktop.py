"""Print / Shift+Print / Ctrl+Shift+S show the desktop's screenshot prompt on PosterChanOS.

Reported: "PosterChanOS Screenshot -> when screenshot keys are pressed, prompt user with nice
cyberpunk-style UI to save to Pictures or select region" — "not done yet" — "i use ctrl shift s".
The prompt existed (osshell.js shotPrompt) and was never reached: Wayfire grabs those keys and runs
/usr/local/bin/pc-screenshot, which went straight to slurp + grim. Measured on a PosterChanOS desktop
(/etc/wayfire.ini: KEY_SYSRQ, <shift> KEY_SYSRQ and <ctrl> <shift> KEY_S all run pc-screenshot).

These RUN the shipped script under sh with stub grim / slurp / pc-wayfire-action on PATH.
"""
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-screenshot"
WAYFIRE = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/wayfire.ini").read_text()
OS_JS = (ROOT / "static/js/client/os.js").read_text()

pytestmark = pytest.mark.skipif(not shutil.which("sh"), reason="sh required")


def _world(tmp_path, *, shell_up=True):
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    log = tmp_path / "calls.log"

    def stub(name, body):
        p = bin_ / name
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(0o755)

    stub("pc-wayfire-action", f'echo "action $1" >>"{log}"\n' + ("exit 0\n" if shell_up else "exit 1\n"))
    stub("slurp", f'echo slurp >>"{log}"; echo "10,10 100x100"\n')
    stub("grim", f'echo "grim $*" >>"{log}"; for a; do last=$a; done; printf "PNG" >"$last"\n')
    stub("notify-send", "exit 0\n")
    stub("wl-copy", "cat >/dev/null\n")
    env = {"PATH": f"{bin_}:/usr/bin:/bin", "HOME": str(tmp_path), "XDG_RUNTIME_DIR": str(tmp_path),
           "UID": str(os.getuid())}
    return env, log


def _run(tmp_path, mode, env=None, **kw):
    base, log = _world(tmp_path, **kw)
    extra = env or {}
    env = base
    r = subprocess.run(["sh", str(SCRIPT), mode], env={**env, **extra}, capture_output=True, text=True, timeout=30)
    return r, (log.read_text() if log.exists() else ""), list((tmp_path / "Pictures" / "Screenshots").glob("*.png")) \
        if (tmp_path / "Pictures" / "Screenshots").exists() else []


@pytest.mark.parametrize("mode,action", [("region", "pc:shot:region"), ("screen", "pc:shot")])
def test_with_the_desktop_running_the_keys_open_its_prompt(tmp_path, mode, action):
    r, calls, files = _run(tmp_path, mode)
    assert r.returncode == 0, r.stderr
    assert f"action {action}" in calls, "the desktop was never asked — the prompt cannot appear"
    assert "grim" not in calls and "slurp" not in calls, "captured behind the prompt's back"
    assert not files


def test_with_no_desktop_the_keys_still_take_a_screenshot(tmp_path):
    r, calls, files = _run(tmp_path, "region", shell_up=False)
    assert r.returncode == 0, r.stderr
    assert "slurp" in calls and "grim" in calls
    assert len(files) == 1, "no desktop to ask must never mean no screenshot"


def test_scripts_can_skip_the_prompt(tmp_path):
    r, calls, files = _run(tmp_path, "screen", env={"PC_SCREENSHOT_DIRECT": "1"})
    assert "action" not in calls and "grim" in calls and len(files) == 1


def test_every_screenshot_key_runs_this_script_and_the_desktop_handles_what_it_sends():
    """Print, Shift+Print and Ctrl+Shift+S all end here, and both actions it sends are ones os.js
    actually handles — an action nobody handles would swallow the key silently."""
    for key in ("KEY_SYSRQ", "<shift> KEY_SYSRQ", "<ctrl> <shift> KEY_S"):
        assert re.search(r"binding_screenshot\w* = " + re.escape(key) + r"\s*$", WAYFIRE, re.M), key
    for action in ("pc:shot", "pc:shot:region"):
        assert f"p === '{action}'" in OS_JS, f"{action} is sent and nothing on the desktop handles it"
