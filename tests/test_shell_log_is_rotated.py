"""The desktop shell's log is rotated, not appended to for ever.

Measured 2026-10-06 on the user's desktop: ~/.config/posterchan-desktop/shell.log at 75 MB, back to
2026-09-10. pc-shell-start-wayfire now moves it aside past 20 MiB at each shell start. Runs the exact
block from BOTH copies of the launcher (os/bin is what is edited, the overlay copy is what is installed).
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COPIES = [ROOT / "os/bin/pc-shell-start-wayfire",
          ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-shell-start-wayfire"]


def _block(path):
    m = re.search(r"# >>> rotate-shell-log\n(.*?)# <<< rotate-shell-log", path.read_text(), re.S)
    assert m, f"no rotation block in {path}"
    return m.group(1)


def _run(path, tmp_path, size):
    log = tmp_path / "shell.log"
    log.write_bytes(b"x" * size)
    (tmp_path / "shell.log.1").write_bytes(b"old")
    subprocess.run(["sh", "-c", f'log="{log}"\n' + _block(path)], check=True, timeout=30)
    return log, tmp_path / "shell.log.1"


@pytest.mark.parametrize("path", COPIES, ids=["os-bin", "overlay"])
def test_a_big_log_is_moved_aside(path, tmp_path):
    log, old = _run(path, tmp_path, 21 * 1024 * 1024)
    assert not log.exists() and old.stat().st_size == 21 * 1024 * 1024


@pytest.mark.parametrize("path", COPIES, ids=["os-bin", "overlay"])
def test_a_small_log_is_left_alone(path, tmp_path):
    log, old = _run(path, tmp_path, 1024)
    assert log.stat().st_size == 1024 and old.read_bytes() == b"old"


def test_the_rotation_runs_before_the_shell_writes_to_the_log():
    text = COPIES[0].read_text()
    assert text.index("# >>> rotate-shell-log") < text.index('"$launcher" --shell')
