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




def run(tmp_path, *, seconds="120", hold=False, swayidle=True):
    """Run the shipped `pc-idle status` against a stubbed session.

    No compositor stub: with Sway gone there is no tree to walk, and the Wayfire half is exercised
    against a real socket by the tests further down. This covers the three causes that are still
    answerable from the outside -- the timeout, a keep-awake hold, and whether the watcher is up.
    """
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    (bin_dir / "pgrep").write_text("#!/bin/sh\n%s\n" % ("echo 4242; exit 0" if swayidle else "exit 1"))
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
    env.pop("WAYFIRE_SOCKET", None)
    done = subprocess.run(["sh", str(SCRIPT), "status"], capture_output=True, text=True,
                          env=env, timeout=60)
    assert done.returncode == 0, done.stderr
    return done.stdout

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





def test_both_copies_of_the_helper_are_the_same_file():
    """Every OS helper exists twice — os/bin and the shell package's FILESDIR — and the installed
    one is the only one any machine ever runs. A fix in one is not a fix."""
    assert SCRIPT.read_bytes() == INSTALLED.read_bytes(), (
        "os/bin/pc-idle and the packaged copy have drifted; the machine runs the packaged one")


def test_swayidle_survives_as_the_protocol_watcher_and_never_touches_the_screen():
    """SWAYIDLE IS NOT SWAY, and it is the one piece of that stack the session still needs.

    It holds the seat's idle accounting and observes idle inhibitors; the POWER action belongs to
    Wayfire's own idle plugin, which `wf_apply` configures. So both edges here are deliberately
    no-ops: a second thing turning the screen off would race the compositor for it. `swaymsg` is
    gone from this script entirely along with the compositor that answered it.
    """
    source = SCRIPT.read_text()
    run = source[source.index("  run|*)"):]
    assert "exec swayidle -w timeout" in run
    assert "true resume true" in run, "swayidle must not perform the power action itself"
    assert "swaymsg" not in source, "pc-idle still shells out to a compositor that is not installed"


# --------------------------------------------------------------- Wayfire owns the power action


def _wayfire_stub(tmp_path):
    """A socket that speaks Wayfire's uint32-le JSON IPC and records what it was asked to set."""
    import socket
    import struct
    import threading

    path = tmp_path / "wayfire-wayland-9-.socket"
    seen: list[dict] = []
    server = socket.socket(socket.AF_UNIX)
    server.bind(str(path))
    server.listen(8)

    def serve():
        while True:
            try:
                conn, _ = server.accept()
            except OSError:
                return
            with conn:
                head = conn.recv(4)
                if len(head) != 4:
                    continue
                size = struct.unpack("<I", head)[0]
                body = b""
                while len(body) < size:
                    body += conn.recv(size - len(body))
                message = json.loads(body)
                seen.append(message)
                if message.get("method") == "wayfire/get-config-option":
                    reply = json.dumps({"result": "ok", "value": "120"}).encode()
                else:
                    reply = json.dumps({"result": "ok"}).encode()
                conn.sendall(struct.pack("<I", len(reply)) + reply)

    thread = threading.Thread(target=serve, daemon=True)
    thread.start()
    return path, seen, server


def _idle(tmp_path, *args, seconds="120", hold=False, socket_path=None):
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for name in ("pgrep", "pkill", "swayidle", "setsid"):
        (bin_dir / name).write_text("#!/bin/sh\nexit 0\n")
        (bin_dir / name).chmod(0o755)
    conf = tmp_path / "idle"
    conf.write_text(seconds)
    runtime = tmp_path / "run"
    runtime.mkdir(exist_ok=True)
    if hold:
        (runtime / "posterchan-keep-awake").write_text("")
    env = dict(os.environ)
    env.update(PATH=f"{bin_dir}:{os.environ['PATH']}", PC_IDLE_CONF=str(conf),
               XDG_RUNTIME_DIR=str(runtime))
    if socket_path:
        env["WAYFIRE_SOCKET"] = str(socket_path)
    else:
        env.pop("WAYFIRE_SOCKET", None)
    done = subprocess.run(["sh", str(SCRIPT), *args], capture_output=True, text=True,
                          env=env, timeout=60)
    return done


def _set_options(seen):
    return [m["data"] for m in seen if m.get("method") == "wayfire/set-config-options"]


def _dpms(seen):
    """The DPMS value of each write. A write also pins `screensaver_timeout`, which is asserted on
    its own below -- matching whole dicts here would break every time another key is added."""
    return [d.get("idle/dpms_timeout") for d in _set_options(seen)]


def test_the_configured_timeout_reaches_the_compositor_that_performs_it(tmp_path):
    """UNDER WAYFIRE THIS SCRIPT IS NOT THE MECHANISM, AND THAT MADE EVERY CONTROL DECORATIVE.

    swayidle here runs `true` on both edges -- the power action belongs to Wayfire's `idle` plugin,
    whose `dpms_timeout` comes from /etc/wayfire.ini and which no user setting wrote. So Settings ->
    Power -> "Turn display off when idle" wrote a file nothing read, and the screen went on blanking
    at the packaged two minutes. Nothing logged; `pc-idle status` agreed with the setting.
    """
    path, seen, server = _wayfire_stub(tmp_path)
    try:
        done = _idle(tmp_path, "set", "300", socket_path=path)
        assert done.returncode == 0, done.stderr
        assert 300 in _dpms(seen), (
            "the number the person chose never reached the compositor that acts on it: %s" % seen)
    finally:
        server.close()


def test_never_disables_the_plugin_rather_than_asking_it_for_zero_seconds(tmp_path):
    """-1 IS "DISABLED"; 0 IS A TIMEOUT OF ZERO SECONDS.

    They are one character apart and opposite: the plugin's own default is -1, and passing the 0 a
    person means by "Never" would ask it to blank the screen immediately.
    """
    path, seen, server = _wayfire_stub(tmp_path)
    try:
        assert _idle(tmp_path, "set", "0", socket_path=path).returncode == 0
        assert -1 in _dpms(seen), _set_options(seen)
    finally:
        server.close()


def test_keep_awake_reaches_the_compositor_and_is_given_back_when_released(tmp_path):
    """A film going black with the switch showing On. Killing swayidle held nothing here."""
    path, seen, server = _wayfire_stub(tmp_path)
    try:
        assert _idle(tmp_path, "hold", "on", seconds="600", socket_path=path).returncode == 0
        assert -1 in _dpms(seen), "keep-awake never reached Wayfire"
        seen.clear()
        assert _idle(tmp_path, "hold", "off", seconds="600", socket_path=path).returncode == 0
        assert 600 in _dpms(seen), (
            "releasing the hold left the compositor never blanking the screen again")
    finally:
        server.close()


def test_the_login_path_reapplies_the_saved_number(tmp_path):
    """The compositor starts from /etc/wayfire.ini, so a saved setting is forgotten every boot
    unless `run` re-applies it -- including when the value is "never", which returns early."""
    path, seen, server = _wayfire_stub(tmp_path)
    try:
        _idle(tmp_path, "run", seconds="0", socket_path=path)
        assert -1 in _dpms(seen), (
            "a saved 'never' is lost at every login and the screen blanks again")
    finally:
        server.close()


def test_a_sway_session_never_talks_to_wayfire(tmp_path):
    """Rollback safety: the proven path must not gain a dependency on a socket that is not there."""
    path, seen, server = _wayfire_stub(tmp_path)
    try:
        assert _idle(tmp_path, "set", "300", socket_path=None).returncode == 0
        assert not seen, "the Sway path reached for the Wayfire IPC: %s" % seen
    finally:
        server.close()


def test_never_also_silences_the_compositors_own_screensaver(tmp_path):
    """"NEVER" HAS TO MEAN NEVER, AND THERE ARE TWO TIMERS.

    Wayfire's idle plugin carries `screensaver_timeout` (3600s by default) alongside
    `dpms_timeout`. It fades the screen to BLACK without powering the outputs down, so a machine
    told never to blank went black an hour later with its monitors awake, the compositor healthy and
    the shell surfaces mapped -- nothing in any log, and indistinguishable from the desktop having
    died. Reproduced on the real desktop: a 28,769-byte screenshot of a 3840x2560 output, restored
    by a single mouse movement. Every write has to pin both.
    """
    path, seen, server = _wayfire_stub(tmp_path)
    try:
        assert _idle(tmp_path, "set", "0", socket_path=path).returncode == 0
        for data in _set_options(seen):
            assert data.get("idle/screensaver_timeout") == -1, (
                "the compositor's own screensaver is still armed: %s" % data)
    finally:
        server.close()


def test_the_shipped_config_does_not_arm_a_second_blanker():
    from tests.wayfire_config import sections
    assert int(sections()["idle"]["screensaver_timeout"]) < 0, (
        "wayfire.ini leaves a 3600s screensaver armed under pc-idle's own policy")
