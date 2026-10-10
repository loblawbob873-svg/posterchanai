"""pc-shell-restart hands the new desktop NONE of the old desktop's open descriptors.

Measured 2026-10-08 on .102 after an update restart (which the desktop runs itself): the launcher script held
486 GPU buffers, 556 deleted files and 206 sockets inherited from the previous desktop -- Chromium leaves its
descriptors inheritable -- and passed them on through `exec` to the new desktop. One of them was the old
remote-debugging listener, so the new desktop could not bind its own and the port accepted nothing for the rest
of the session. Runs the SHIPPED script holding a listening socket on fd 71 and a file on fd 72, with a fake
launcher that records its own descriptor table.
"""
import os
import socket
import subprocess
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-shell-restart"


def test_the_launcher_inherits_only_stdio(tmp_path):
    out = tmp_path / "fds.txt"
    fake = tmp_path / "launcher"
    fake.write_text(f"#!/bin/sh\nls /proc/$$/fd > {out}\n")
    fake.chmod(0o755)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen(1)
    held = open(tmp_path / "old-profile.ldb", "w")
    # Free descriptors chosen by the kernel (>= 70, as a desktop's would be). A fixed dup2(…, 71) raced
    # other threads of a long serial run (EBUSY) and would silently CLOSE whatever already lived at 71.
    import fcntl
    a = fcntl.fcntl(listener.fileno(), fcntl.F_DUPFD, 70)
    b = fcntl.fcntl(held.fileno(), fcntl.F_DUPFD, 70)
    os.set_inheritable(a, True)
    os.set_inheritable(b, True)
    try:
        env = {"PATH": os.environ["PATH"], "PC_SHELL_START": str(fake), "XDG_RUNTIME_DIR": str(tmp_path)}
        subprocess.run(["sh", str(SCRIPT)], env=env, pass_fds=(a, b), stdout=subprocess.DEVNULL,
                       stderr=subprocess.DEVNULL, timeout=20)
        for _ in range(100):
            if out.exists() and out.read_text().strip():
                break
            time.sleep(.05)
        fds = {int(x) for x in out.read_text().split()}
    finally:
        os.close(a); os.close(b); listener.close(); held.close()
    assert fds, "the launcher never ran"
    assert a not in fds and b not in fds, ("the launcher inherited the old desktop's descriptors", sorted(fds))
    # 3 = ls listing its own directory; 255 = the fake launcher (a bash script) holding ITS script open.
    assert fds <= {0, 1, 2, 3, 255}, sorted(fds)
