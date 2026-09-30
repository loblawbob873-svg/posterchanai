"""Choosing an area: only slurp's own "selection cancelled" is a change of mind; any other failure is
said out loud (desktop/screenshot.js). It used to read every slurp failure as Escape and return
nothing — on screen, a button that does nothing ("selecting region does nothing")."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]


def _capture(tmp_path, stderr, code):
    slurp = tmp_path / "slurp"
    slurp.write_text(f"#!/bin/sh\necho '{stderr}' >&2\nexit {code}\n")
    slurp.chmod(0o755)
    js = ("require(" + json.dumps(str(ROOT / "desktop/screenshot.js")) + ").capture({mode:'region'})"
          ".then(r=>process.stdout.write(JSON.stringify(r)))")
    r = subprocess.run(["node", "-e", js], env={"PATH": "/usr/bin:/bin", "PC_SLURP": str(slurp), "HOME": str(tmp_path)},
                       capture_output=True, text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    return json.loads(r.stdout)


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_escape_is_still_a_quiet_cancel(tmp_path):
    got = _capture(tmp_path, "selection cancelled", 1)
    assert got.get("cancelled") is True and not got.get("why"), got


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_a_real_failure_says_why(tmp_path):
    got = _capture(tmp_path, "failed to connect to display", 1)
    assert not got.get("cancelled") and "failed to connect to display" in got.get("why", ""), got


@pytest.mark.skipif(not shutil.which("node"), reason="node required")
def test_the_picker_is_not_left_waiting_on_stdin(tmp_path):
    """"I selected Select and can't select": when its stdin is not a terminal, slurp first reads a
    list of predefined boxes from it, and execFile hands every child an open pipe. So slurp sat
    reading a pipe nobody would ever close — measured on the PosterChanOS desktop with WAYLAND_DEBUG,
    it had not sent a single request to the compositor, so no overlay was ever drawn, while the same
    binary run with stdin at EOF showed its overlay at once. This fake does what slurp does: read
    stdin to EOF, then answer."""
    slurp = tmp_path / "slurp"
    slurp.write_text("#!/bin/sh\ncat >/dev/null\necho '10,20 300x200'\n")
    slurp.chmod(0o755)
    grim = tmp_path / "grim"
    grim.write_text('#!/bin/sh\nfor a; do last="$a"; done\necho "$@" > "$HOME/grim.args"\nprintf PNG > "$last"\n')
    grim.chmod(0o755)
    js = ("require(" + json.dumps(str(ROOT / "desktop/screenshot.js")) + ").capture({mode:'region'})"
          ".then(r=>process.stdout.write(JSON.stringify(r)))")
    try:
        r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=20,
                           env={"PATH": "/usr/bin:/bin", "PC_SLURP": str(slurp), "PC_GRIM": str(grim), "HOME": str(tmp_path)})
    except subprocess.TimeoutExpired:
        pytest.fail("the region picker never answered: slurp is still waiting on an open stdin")
    got = json.loads(r.stdout)
    assert got.get("ok") is True, got
    assert "-g 10,20 300x200" in (tmp_path / "grim.args").read_text()
