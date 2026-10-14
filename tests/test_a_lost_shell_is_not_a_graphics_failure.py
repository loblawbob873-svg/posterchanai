"""One lost shell must not drop a working machine to software rendering, and the log that explains it must survive.

The TV, 2026-10-13, opening Steam: the desktop shell's display connection was cut, the shell exited, and
pc-compositor-session (correctly) stopped Wayfire because no shell replaced it. Then it read the status of ITS OWN
stop ("killed by signal 127") as the graphics failing, and re-exec'd with software rendering, then pixman -- three
restarts in two minutes on an NVIDIA card that was fine. Each restart also truncated wayfire.log, so the one record
of why the shell's connection was cut was gone before anyone could read it.

Both rules run here as the SHIPPED shell code, sliced out of os/bin/pc-compositor-session.
"""
import subprocess
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SESSION = (ROOT / "os/bin/pc-compositor-session").read_text(encoding="utf-8")
PACKAGED = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-compositor-session"


def _retry_rule():
    a = SESSION.index("pc_retry_sw=''")
    b = SESSION.index("esac", a) + len("esac")
    return SESSION[a:b]


def _decide(status, reason):
    script = ("describe_status(){ echo \"exited with $1\"; }\nstatus=%s\nreason=%s\n%s\nprintf '%%s' \"$pc_retry_sw\"\n"
              % (status, _q(reason), _retry_rule()))
    return subprocess.run(["sh", "-c", script], capture_output=True, text=True, check=True).stdout


def _q(s):
    return "'" + s.replace("'", "'\\''") + "'"


def test_stopping_wayfire_after_the_shell_went_away_is_not_a_graphics_failure():
    out = _decide(127, "no replacement shell within 3000ms of the last one exiting (restart marker absent)")
    assert out == "", ("a lost shell is being retried in software rendering: " + out)


def test_a_real_compositor_crash_still_falls_back_to_software():
    assert _decide(139, "startup") != "", "a compositor that crashed by itself no longer gets the software retry"
    assert _decide(0, "the shell never signalled ready") != "", "a desktop that painted nothing lost its retry"


def _keep_fn():
    a = SESSION.index("pc_keep_wayfire_logs() {")
    b = SESSION.index("\n}\n", a) + 3
    return SESSION[a:b]


def test_a_restart_keeps_the_previous_compositor_logs():
    with tempfile.TemporaryDirectory() as d:
        p = Path(d)
        for gen in ("first", "second", "third"):
            subprocess.run(["sh", "-c", _keep_fn() + "\npc_keep_wayfire_logs \"$1\"", "sh", d], check=True)
            (p / "wayfire.log").write_text(gen + " session\n")
        subprocess.run(["sh", "-c", _keep_fn() + "\npc_keep_wayfire_logs \"$1\"", "sh", d], check=True)
        assert (p / "wayfire.log.1").read_text() == "third session\n", "the session that just ended lost its log"
        assert (p / "wayfire.log.2").read_text() == "second session\n", "the one before it was not kept"
    assert "pc_keep_wayfire_logs \"$state_dir\"" in SESSION.split('wayfire -c "$session_cfg" >"$state_dir/wayfire.log"')[0][-600:], \
        "the logs are not rotated right before Wayfire starts"


def test_the_packaged_copy_is_the_same_script():
    assert PACKAGED.read_text(encoding="utf-8") == SESSION, "the overlay ships a different pc-compositor-session"
