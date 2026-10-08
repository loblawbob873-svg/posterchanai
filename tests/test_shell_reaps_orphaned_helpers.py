"""A dead desktop's GPU process does not outlive it.

Found on the desktop box (2026-10-07) while chasing "posterchan crashed": two earlier sessions' GPU processes
and network services were still running 3 and 5 hours after their shells had died -- reparented to init,
holding GPU memory beside the live desktop's. pc-shell-restart kills only the shell, and a crash kills only
the shell, so the helpers stay. The launcher now reaps them before each start: a PosterChan helper
(`--type=`) whose parent is init. This RUNS the function shipped in the launcher against a fake /proc.
"""
import re
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
COPIES = [ROOT / "os/bin/pc-shell-start-wayfire",
          ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-shell-start-wayfire"]
EXE = "/opt/posterchan/posterchan-desktop"


def _fn(path):
    text = path.read_text()
    m = re.search(r"# >>> reap-orphaned-helpers\n.*?# <<< reap-orphaned-helpers\n", text, re.S)
    assert m, f"{path.name} has no orphaned-helper reaper"
    return m.group(0)


def _proc(tmp, pid, cmd, ppid, comm="posterchan-desk"):
    d = tmp / str(pid)
    d.mkdir()
    (d / "cmdline").write_bytes(b"\0".join(c.encode() for c in cmd) + b"\0")
    (d / "stat").write_text(f"{pid} ({comm}) S {ppid} {pid} {pid} 0 -1 4194560 0\n")


@pytest.mark.parametrize("path", COPIES, ids=["os-bin", "overlay"])
def test_only_an_orphaned_posterchan_helper_is_reaped(path, tmp_path):
    proc = tmp_path / "proc"; proc.mkdir()
    _proc(proc, 101, [EXE, "--type=gpu-process", "--ozone-platform=wayland"], 1)               # orphan: reap
    _proc(proc, 102, [EXE, "--type=utility", "--utility-sub-type=network.mojom.NetworkService"], 1)  # orphan: reap
    _proc(proc, 103, [EXE, "--type=gpu-process"], 500)                                         # live shell's: keep
    _proc(proc, 104, [EXE, "--shell", "--ozone-platform=wayland"], 1)                          # a shell itself: keep
    _proc(proc, 105, [EXE, "--type=gpu-process", "--pc-diagnostic-token=x"], 1)                # diagnostic: keep
    _proc(proc, 106, ["/usr/bin/firefox", "--type=gpu-process"], 1)                            # not ours: keep
    _proc(proc, 107, [EXE, "--type=renderer"], 1, comm="posterchan desk (x)")                  # odd comm, orphan: reap
    log = tmp_path / "killed"
    stub = tmp_path / "kill"
    stub.write_text(f"#!/bin/sh\necho \"$2\" >>{log}\n"); stub.chmod(0o755)
    script = _fn(path) + f"PC_PROC={proc} PC_KILL={stub} pc_reap_orphaned_helpers\n"
    done = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=30)
    assert done.returncode == 0, done.stderr
    killed = sorted(log.read_text().split()) if log.exists() else []
    assert killed == ["101", "102", "107"], killed


@pytest.mark.parametrize("path", COPIES, ids=["os-bin", "overlay"])
def test_the_launcher_reaps_before_every_start(path):
    text = path.read_text()
    loop = text[text.index("while [ \"$attempt\" -lt 2 ]; do"):]
    assert loop.index("pc_reap_orphaned_helpers") < loop.index('"$launcher" --shell'), \
        "the launcher starts a shell without clearing the last one's orphaned helpers"
