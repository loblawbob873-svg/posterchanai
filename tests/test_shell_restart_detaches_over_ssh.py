"""pc-shell-restart run over ssh gives the new desktop a session of its own.

2026-10-02: run over ssh after an update, `exec` made the launcher -- and the desktop -- children of the
ssh connection on both test machines: the desktop would die with the connection, and the command never
returned. Runs the SHIPPED script with a fake launcher that records its pid and session id.
"""
import os
import subprocess
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-shell-restart"


def _run(tmp_path, extra_env):
    out = tmp_path / "launcher.txt"
    fake = tmp_path / "launcher"
    fake.write_text(f"#!/bin/sh\necho \"$$ $(ps -o sid= -p $$ | tr -d ' ')\" > {out}\nsleep 2\n")
    fake.chmod(0o755)
    env = {"PATH": os.environ["PATH"], "PC_SHELL_START": str(fake), "XDG_RUNTIME_DIR": str(tmp_path)}
    env.update(extra_env)
    t = time.time()
    p = subprocess.Popen(["sh", str(SCRIPT)], env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                         start_new_session=True)
    p.wait(timeout=10)
    took = time.time() - t
    for _ in range(50):
        if out.exists() and out.read_text().strip():
            break
        time.sleep(.05)
    pid, sid = out.read_text().split()
    # The script runs in a new session of its own here, so its session id is its pid.
    return {"script_pid": p.pid, "script_sid": p.pid,
            "pid": int(pid), "sid": int(sid), "took": took}


@pytest.mark.skipif(not Path("/usr/bin/setsid").exists() and not Path("/bin/setsid").exists(), reason="setsid required")
def test_over_ssh_the_desktop_gets_a_session_of_its_own_and_the_command_returns(tmp_path):
    r = _run(tmp_path, {"SSH_CONNECTION": "192.168.0.2 5000 192.168.0.154 22"})
    assert r["took"] < 1.5, ("the command waited on the desktop instead of returning", r)
    assert r["sid"] == r["pid"], ("the launcher still belongs to the caller's session", r)
    assert r["pid"] != r["script_pid"], r


def test_from_the_key_binding_it_still_hands_over_in_place(tmp_path):
    r = _run(tmp_path, {})
    # exec: the launcher IS the script's process, so the session is unchanged and it ran to its end.
    assert r["pid"] == r["script_pid"] and r["sid"] == r["script_sid"], r
    assert r["took"] >= 1.5, r
