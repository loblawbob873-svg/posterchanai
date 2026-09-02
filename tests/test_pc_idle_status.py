"""`pc-idle status` MUST NAME THE REASON THE SCREEN IS STILL ON.

Reported as "you need to make sure that the screen turnoff thing with sway working, my desktop
monitor has been on a long time", followed by the user's own guess: "i think the timeout for
display worked but maybe notifications is waking up monitor".

That guess is the important part, because it is what everybody reaches for and it is checkable
from nothing. There are four ways this fails and THREE of them leave every symptom you would think
to look at reading perfectly healthy:

  * the timeout is 0 (never) — someone set it, or `pc-idle set` wrote a 0;
  * a keep-awake hold is on — `pc-idle hold on` and nothing turned it back off;
  * swayidle is not running at all — `exec_always` did not fire, or it was pkill'd and never
    restarted, and there is then nothing anywhere that will ever blank the display;
  * SOMETHING HOLDS AN IDLE INHIBITOR — and this one hides from all three checks above. Sway
    implements idle-inhibit by never reporting the seat idle, so swayidle's timer does not fire
    LATE, it never fires, while the daemon sits there running and the configured timeout is right.
    The shell is Chromium, and Chromium takes an inhibitor for any page holding a screen wake lock
    or playing video — the client takes a screen wake lock during a call — and sway.config takes
    one deliberately for a fullscreen Steam game.

So `status` reads all four and says which. This RUNS the shipped script against a stubbed
compositor: a test that read the file as text could not tell whether the tree walk finds an
inhibitor, which is the whole reason the command exists.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import textwrap
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "os/bin/pc-idle"
INSTALLED = ROOT / "os/overlay/app-misc/posterchanos-shell/files/pc-idle"

pytestmark = pytest.mark.skipif(not shutil.which("python3"), reason="python3 unavailable")


def _view(app, name, user="none", application="none"):
    return {"app_id": app, "name": name,
            "idle_inhibitors": {"user": user, "application": application},
            "nodes": [], "floating_nodes": []}


def tree(*views):
    return {"type": "root", "nodes": [{"type": "output", "name": "DP-1", "nodes": list(views),
                                       "floating_nodes": []}], "floating_nodes": []}


def run(tmp_path, *, seconds="120", hold=False, swayidle=True, sway_tree=None, no_sway=False):
    """Run the shipped `pc-idle status` against a stubbed session."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)

    (bin_dir / "pgrep").write_text(textwrap.dedent(f"""\
        #!/bin/sh
        {'echo 4242; exit 0' if swayidle else 'exit 1'}
        """))
    if not no_sway:
        payload = tmp_path / "tree.json"
        payload.write_text(json.dumps(sway_tree if sway_tree is not None else tree()))
        (bin_dir / "swaymsg").write_text(f"#!/bin/sh\ncat {payload}\n")
    for stub in bin_dir.iterdir():
        stub.chmod(0o755)

    conf = tmp_path / "idle"
    conf.write_text(seconds)
    runtime = tmp_path / "run"
    runtime.mkdir(exist_ok=True)
    if hold:
        (runtime / "posterchan-keep-awake").write_text("")

    env = dict(os.environ)
    env.update(PATH=f"{bin_dir}:{os.environ['PATH']}", PC_IDLE_CONF=str(conf),
               XDG_RUNTIME_DIR=str(runtime))
    done = subprocess.run(["sh", str(SCRIPT), "status"], capture_output=True, text=True,
                          env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout


def test_the_inhibitor_is_found_and_named(tmp_path):
    """THE CAUSE THAT HIDES. Everything else here is healthy — the timeout is set, the daemon is
    running, no hold — and the screen still never blanks."""
    out = run(tmp_path, sway_tree=tree(
        _view("firefox", "some video — Mozilla Firefox", application="enabled"),
        _view("org.telegram.desktop", "Telegram")))
    assert "HOLDING THE SCREEN AWAKE" in out, out
    assert "firefox" in out, "the inhibitor is reported but not attributed to an app"
    assert "swayidle:   running" in out, "the daemon is running — that must not read as the fault"


def test_a_healthy_session_says_nothing_is_holding_it(tmp_path):
    """The check has to be able to come back clean, or it is just a scary message."""
    out = run(tmp_path, sway_tree=tree(_view("firefox", "Firefox")))
    assert "inhibitors: none" in out, out


def test_a_missing_daemon_is_called_out(tmp_path):
    """With a real timeout and no swayidle there is nothing anywhere that will blank the display."""
    out = run(tmp_path, swayidle=False)
    assert "NOT RUNNING" in out, out


def test_no_daemon_is_correct_when_the_timeout_is_never(tmp_path):
    """0 means never and is a real answer somebody chose — it must not be reported as a fault."""
    out = run(tmp_path, seconds="0", swayidle=False)
    assert "never" in out
    assert "NOT RUNNING" not in out, "a deliberate 'never' is being reported as a broken daemon"


def test_a_keep_awake_hold_is_reported(tmp_path):
    out = run(tmp_path, hold=True)
    assert "keep-awake: ON" in out, out
    assert "pc-idle hold off" in out, "it says a hold is on without saying how to clear it"


def test_it_says_when_it_could_not_ask_sway(tmp_path):
    """'Could not ask' is never 'nothing is holding it' — the same rule the drive check follows."""
    out = run(tmp_path, no_sway=True)
    assert "cannot ask sway" in out, out
    assert "inhibitors: none" not in out, (
        "a session it could not reach is being reported as one with no inhibitors")


def test_an_inhibitor_held_by_the_user_counts_too(tmp_path):
    """sway reports `user` and `application` separately; either one stops the seat going idle."""
    out = run(tmp_path, sway_tree=tree(_view("steam_app_1091500", "Cyberpunk 2077", user="enabled")))
    assert "HOLDING THE SCREEN AWAKE" in out
    assert "steam_app_1091500" in out


def test_a_broken_tree_does_not_take_the_whole_report_down(tmp_path):
    """status is a diagnostic; it has to survive an answer it did not expect and still print the
    three facts it read before it got there."""
    bad = tmp_path / "bad"
    bad.mkdir()
    out = run(bad, sway_tree={"nodes": "not a list"})
    assert "timeout:" in out and "swayidle:" in out, out


def test_both_copies_of_the_helper_are_the_same_file():
    """Every OS helper exists twice — os/bin and the shell package's FILESDIR — and the installed
    one is the only one any machine ever runs. A fix in one is not a fix."""
    assert SCRIPT.read_bytes() == INSTALLED.read_bytes(), (
        "os/bin/pc-idle and the packaged copy have drifted; the machine runs the packaged one")
