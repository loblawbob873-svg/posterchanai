"""A TERMINAL SOMEBODY LEAVES OPEN HELD A DEAD SHELL'S LISTENING SOCKET.

Chromium's file descriptors are not CLOEXEC, so anything this process spawns inherits all of them.
For `grim`, `slurp`, `wpctl` or `nmcli` that is harmless -- they exit in milliseconds. The local
terminal is the opposite: a LOGIN SHELL that lives for days.

Measured on the running desktop, long after the shell that opened it had exited:

    LISTEN 127.0.0.1:9222  users:(("bash",pid=1089812,fd=59),("script",pid=1089811,fd=59))

95 descriptors, 13 of them sockets, one of them a LISTENING socket -- so the replacement shell could
not bind its own port, and nothing in any log connected the two. The fix is a prologue inside the
child, because node offers no "close the rest" and `script` runs its command through `sh -c`, which
is the one place that is after the fork and before the shell.
"""
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
SRC = (ROOT / "desktop/localterm.js").read_text()


def _prologue():
    """The exact string the launcher builds, recovered from the source rather than retyped."""
    body = SRC.split("const closeInherited =", 1)[1].split("const cmd =", 1)[0]
    # Only the CONCATENATED string literals, and the escaped empty-pattern `\'\'` inside the case
    # is part of the shell text, not a delimiter -- so join on the JS `+` rather than on quotes.
    parts = re.findall(r"'((?:[^'\\]|\\.)*)'", body)
    return "".join(p.replace("\\'", "'") for p in parts)


def test_the_shell_is_started_behind_the_prologue():
    assert "closeInherited" in SRC
    cmd = SRC.split("const cmd = `", 1)[1].split("`;", 1)[0]
    assert cmd.startswith("${closeInherited};"), cmd
    assert "exec ${shell} -l" in cmd


def test_it_actually_closes_an_inherited_descriptor():
    """RUN it. A regex over the source cannot tell a working `exec N>&-` from a typo in one."""
    pro = _prologue()
    assert "exec" in pro and ">&-" in pro
    open_two = "exec 9< /etc/hostname; exec 8< /etc/hosts; "
    listing = "ls /proc/$$/fd | tr '\\n' ' '"
    before = subprocess.check_output(["sh", "-c", open_two + listing], text=True).split()
    after = subprocess.check_output(["sh", "-c", open_two + pro + "; " + listing], text=True).split()
    assert "8" in before and "9" in before, before
    assert after == ["0", "1", "2"], after


def test_the_terminal_still_gets_its_pty_and_its_size():
    """The prologue must not take stdin/stdout/stderr with it: those ARE the pty."""
    pro = _prologue()
    out = subprocess.check_output(
        ["sh", "-c", pro + "; echo alive >&1; echo err >&2"], text=True, stderr=subprocess.STDOUT)
    # STDERR IS THE HALF THAT BROKE. A `2>/dev/null` on the loop made the shell save fd 2 to a high
    # descriptor the glob had already listed, so the loop closed the shell's own copy and every
    # later diagnostic went nowhere -- on a TERMINAL, where stderr is most of the point.
    assert "alive" in out and "err" in out, out
    assert "stty cols" in SRC.split("const cmd = `", 1)[1].split("`;", 1)[0]
