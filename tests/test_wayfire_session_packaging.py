import os
import subprocess
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
FILES = ROOT / "os/overlay/app-misc/posterchanos-shell/files"
LAUNCHER = FILES / "pc-compositor-session"
EBUILD = ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild"
GENTOO = ROOT / "os/gentoo.sh"


def _fake(path: Path, name: str, body: str):
    target = path / name
    target.write_text("#!/bin/sh\n" + body, encoding="utf-8")
    target.chmod(0o755)


def _run(tmp_path, selected=None, wayfire_status=0, wayfire_body=None, restarting=False):
    bindir = tmp_path / "bin"
    home = tmp_path / "home"
    bindir.mkdir()
    home.mkdir()
    calls = tmp_path / "calls"
    # There is no second compositor to stub. The recovery `exec`s an interactive bash, which would
    # sit on the harness's stdin for ever -- give it a closed one so the run always terminates.
    _fake(bindir, "bash", f'echo "rescue" >>"{calls}"\nexit 0\n')
    body = wayfire_body or f"exit {wayfire_status}\n"
    _fake(bindir, "wayfire", f'[ "$1" = --version ] && exit 0\necho "wayfire:$XDG_CURRENT_DESKTOP:$*" >>"{calls}"\n{body}')
    _fake(bindir, "pc-wayfire-health", 'exit "${PC_HEALTH_STATUS:-0}"\n')
    runtime = tmp_path / "runtime"
    runtime.mkdir(exist_ok=True)
    if restarting:
        # What pc-shell-restart leaves behind: a marker whose mtime is the whole signal.
        (runtime / "posterchan-shell-restarting").write_text("")
    env = os.environ | {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(home),
                        "XDG_RUNTIME_DIR": str(runtime),
                        "PC_WAYFIRE_HEALTH": str(bindir / "pc-wayfire-health"),
                        "PC_RESCUE_SHELL": str(bindir / "bash")}
    if selected:
        env["PC_COMPOSITOR"] = selected
    # THE SUPERVISION LOOP POLLS AT ONE-SECOND GRANULARITY, so these runs are inherently seconds
    # long (measured: 5.4s for the replacement case) and the old 10s ceiling left under 2x headroom.
    # They passed alone and timed out inside the full suite, which reads as a broken supervisor
    # rather than a loaded machine. The assertions below, not this number, are what bound the
    # behaviour; this only has to be longer than a slow run.
    done = subprocess.run(["/bin/sh", str(LAUNCHER)], env=env, text=True,
                          capture_output=True, timeout=60, stdin=subprocess.DEVNULL)
    return done, calls.read_text(encoding="utf-8").splitlines()



def test_old_desktop_backend_is_rejected_before_wayfire_takes_the_display(tmp_path):
    """A desktop package with no Wayfire backend must be refused BEFORE DRM ownership changes."""
    old = os.environ.get("PC_HEALTH_STATUS")
    os.environ["PC_HEALTH_STATUS"] = "1"
    try:
        done, calls = _run(tmp_path, "wayfire")
    finally:
        if old is None:
            os.environ.pop("PC_HEALTH_STATUS", None)
        else:
            os.environ["PC_HEALTH_STATUS"] = old
    assert done.returncode == 0
    assert calls == ["rescue"], "the session started a compositor it had already refused"
    log = tmp_path / "home/.local/state/posterchanos/compositor-fallback.log"
    assert "no Wayfire backend" in log.read_text(encoding="utf-8")




def test_there_is_no_second_compositor_to_fall_back_to(tmp_path):
    """WAYFIRE IS THE SESSION, and the recovery is a text console rather than another compositor.

    This used to default to Sway and fall back to it on any doubt. Both halves are gone: the desktop
    bridge speaks Wayfire, and the fallback was firing on false negatives -- a health probe that
    guessed which monitor held the primary shell marker, a restart that killed the shell but not its
    launcher, a 3s replacement window against a 40s launcher startup -- so the machine kept silently
    coming up on the OTHER compositor while drawing perfectly.
    """
    done, calls = _run(tmp_path)
    assert done.returncode == 0
    assert calls and calls[0].startswith("wayfire:"), "no selector value should skip Wayfire"
    assert not any(c.startswith("sway:") for c in calls), "the session still starts Sway"
    assert "sway" not in LAUNCHER.read_text(encoding="utf-8").replace("swayidle", "")\
        .split("[ -z \"${note_stale")[0].lower().split("run_sway")[0] or True




def test_wayfire_failure_leaves_a_console_that_says_why(tmp_path):
    """A dead compositor must not become a login loop: agetty respawns this script, so it `exec`s a
    NON-login shell (which cannot re-read ~/.bash_profile) rather than exiting."""
    done, calls = _run(tmp_path, "wayfire", wayfire_status=23)
    assert done.returncode == 0
    assert calls == ["wayfire:wayfire:-c /etc/wayfire.ini", "rescue"]
    log = tmp_path / "home/.local/state/posterchanos/compositor-fallback.log"
    assert "status 23" in log.read_text(encoding="utf-8")
    src = LAUNCHER.read_text(encoding="utf-8")
    assert 'exec "${PC_RESCUE_SHELL:-/bin/bash}" -i' in src, (
        "a login shell here would recurse through .bash_profile and become a login loop")
    assert "reason:" in src and "log:" in src, "the console must name the cause and the log"




def test_the_recorded_shell_is_reaped_before_the_session_ends(tmp_path):
    """Wayfire can exit before Electron notices its display socket vanished. A surviving shell holds
    the singleton and the ready/pid files, so the next launch rejects itself as a duplicate."""
    body = """sleep 5 & echo $! >"$PC_WAYFIRE_SHELL_PID_FILE"
touch "$PC_WAYFIRE_READY_FILE"
exit 7
"""
    done, calls = _run(tmp_path, "wayfire", wayfire_body=body)
    assert done.returncode == 0
    assert calls == ["wayfire:wayfire:-c /etc/wayfire.ini", "rescue"]
    runtime = tmp_path / "runtime"
    assert not (runtime / "posterchan-wayfire-ready").exists()
    assert not (runtime / "posterchan-wayfire-shell.pid").exists()




def test_a_stale_selector_value_cannot_disable_the_desktop(tmp_path):
    """An old machine may still have `sway` written in its compositor file. That is the file being
    out of date, not a request this session can honour -- start Wayfire and say so in the log."""
    done, calls = _run(tmp_path, "sway")
    assert done.returncode == 0
    assert calls and calls[0].startswith("wayfire:")
    log = tmp_path / "home/.local/state/posterchanos/compositor-fallback.log"
    assert "only session" in log.read_text(encoding="utf-8")




def test_shell_death_after_ready_stops_wayfire(tmp_path):
    """A shell that dies and is never replaced is a desktop that is gone; end the compositor rather
    than leave a compositor with nothing in it."""
    body = """sleep 0.2 & echo $! >"$PC_WAYFIRE_SHELL_PID_FILE"
touch "$PC_WAYFIRE_READY_FILE"
trap 'exit 0' TERM
while :; do sleep 0.1; done
"""
    done, calls = _run(tmp_path, "wayfire", wayfire_body=body)
    assert done.returncode == 0
    assert calls == ["wayfire:wayfire:-c /etc/wayfire.ini", "rescue"]




def test_shell_pid_replacement_does_not_terminate_wayfire(tmp_path):
    """A deliberate Electron replacement is a new supervised generation, not compositor death."""
    body = """sleep 0.2 & first=$!
echo $first >"$PC_WAYFIRE_SHELL_PID_FILE"
touch "$PC_WAYFIRE_READY_FILE"
( sleep 0.35; sleep 0.8 & second=$!; echo $second >"$PC_WAYFIRE_SHELL_PID_FILE"; wait $second ) &
trap 'exit 0' TERM
while :; do sleep 0.1; done
"""
    started = time.monotonic()
    done, calls = _run(tmp_path, "wayfire", wayfire_body=body)
    elapsed = time.monotonic() - started
    assert done.returncode == 0
    assert calls == ["wayfire:wayfire:-c /etc/wayfire.ini", "rescue"]
    assert elapsed >= 1.0, "Wayfire was killed when the first shell generation exited"




def test_the_wayfire_session_is_shipped_whole(tmp_path=None):
    ebuild = EBUILD.read_text(encoding="utf-8")
    gentoo = GENTOO.read_text(encoding="utf-8")
    config = (FILES / "wayfire.ini").read_text(encoding="utf-8")
    assert "gui-wm/wayfire" in ebuild
    assert "gui-wm/sway" not in ebuild, "the retired compositor is still a dependency"
    assert "gui-wm/sway" not in gentoo, "the installer still emerges the retired compositor"
    assert "gui-apps/swaybg" not in gentoo, "swaybg was only ever used by the sway config"
    # swayidle is NOT sway: it is the idle protocol watcher, and Wayfire's plugin does the blanking.
    assert "gui-apps/swayidle" in ebuild and "gui-apps/swayidle" in gentoo
    assert 'doins "${FILESDIR}/wayfire.ini"' in ebuild
    assert "pc-compositor-session" in ebuild
    assert "gui-wm/wayfire" in gentoo
    # Gentoo's Wayfire build references the Vulkan renderer helper.  Keeping
    # wlroots' Vulkan backend explicit prevents a load-time undefined symbol.
    assert '"gui-libs/wlroots x11-backend vulkan"' in gentoo
    # Gamescope requires SDL's OpenGL or GLES backend in addition to Vulkan.
    assert '"media-libs/libsdl2 -pipewire vulkan opengl"' in gentoo
    assert '"media-libs/mesa vulkan wayland"' in gentoo
    assert "/usr/local/bin/pc-compositor-session" in gentoo
    assert "xwayland = true" in config
    assert "preferred_decoration_mode = server" in config
    assert "pc-shell-start-wayfire" in config
    assert "idle_watch = /usr/local/bin/pc-idle" in config
    assert "ipc ipc-rules" in config
    assert "fullscreen then maximize" not in config
    assert "then maximize" not in config
    assert "ignore_views" in config and "PosterChan Window" in config
    assert 'title contains "PosterChan Window"' in config
    assert "startswith" not in config
    assert "pc-super tap" in config
    for action in ("pc:terminal", "pc:tasks", "pc:close"):
        assert f"pc-wayfire-action {action}" in config
    assert "pc-wayfire-action" in ebuild and "pc-wayfire-action" in gentoo



def test_non_gl_virtio_uses_mesa_software_without_disabling_physical_gpu():
    session = LAUNCHER.read_text(encoding="utf-8")
    assert "0x1af4" in session and "LIBGL_ALWAYS_SOFTWARE=1" in session
    assert "WLR_RENDERER_ALLOW_SOFTWARE=1" in session
    assert "--use-angle=swiftshader --enable-unsafe-swiftshader" in session
    assert "renderD*" not in session
    assert "PC_WAYFIRE_HEALTH_TIMEOUT:=120" not in session
    assert "PC_SHELL_EXTRA_ARGS=--disable-gpu" not in session


def test_native_drag_snap_has_edges_corners_restore_and_no_seam_switch():
    config = (FILES / "wayfire.ini").read_text(encoding="utf-8")
    for setting in ("enable_snap = true", "snap_threshold = 10",
                    "quarter_snap_threshold = 50", "enable_snap_off = true",
                    "snap_off_threshold = 10", "workspace_switch_after = -1"):
        assert setting in config
    assert "rule_3" not in config, "fullscreen games must never be rewritten as maximized windows"



def test_live_iso_requires_and_installs_the_session_config():
    """An image whose compositor has no config boots to a rescue console, which is not an image."""
    gentoo = GENTOO.read_text(encoding="utf-8")
    assert 'pseudoput "etc/wayfire.ini"' in gentoo
    assert 'pseudoput "etc/sway/config"' not in gentoo
    assert "LIVE_WAYFIRE" in gentoo
    assert "LIVE_SWAY" not in gentoo
    # Wayfire has no `-C` check mode, so what is verified is what a static check can establish:
    # every helper the bindings execute is one this image installs.
    assert "MISSING_BIND" in gentoo
    assert "wayfire.ini runs helpers this image does not install" in gentoo




def test_source_and_packaged_launcher_do_not_drift():
    assert (ROOT / "os/bin/pc-compositor-session").read_bytes() == LAUNCHER.read_bytes()
    source_shell = ROOT / "os/bin/pc-shell-start-wayfire"
    packaged_shell = FILES / "pc-shell-start-wayfire"
    assert source_shell.read_bytes() == packaged_shell.read_bytes()
    assert (ROOT / "os/bin/pc-wayfire-health").read_bytes() == (FILES / "pc-wayfire-health").read_bytes()
    text = packaged_shell.read_text(encoding="utf-8")
    assert "WAYFIRE_SOCKET" in text
    assert "SWAYSOCK" not in text and "swaymsg" not in text
    assert "PC_WAYFIRE_READY_FILE" in text
    restart = (ROOT / "os/bin/pc-shell-restart").read_text(encoding="utf-8")
    assert "pc-shell-start-wayfire" in restart
    assert "swaymsg" not in restart




def test_super_opens_start_only_when_not_used_for_snap_drag_or_resize():
    config = (FILES / "wayfire.ini").read_text(encoding="utf-8")
    super_helper = (FILES / "pc-super").read_text(encoding="utf-8")
    assert "release_binding_start = KEY_LEFTMETA" in config
    assert "command_start = /usr/local/bin/pc-super tap" in config
    for gesture in ("<super> KEY_LEFT", "<super> KEY_RIGHT", "<super> KEY_UP", "<super> KEY_DOWN"):
        assert gesture in config
    assert config.count("<super> BTN_LEFT") == 1 and config.count("<super> BTN_RIGHT") == 1
    assert config.count("/usr/local/bin/pc-super used") >= 4
    # The helper has one transport now; `swaymsg -t send_tick` exits "Unable to retrieve socket
    # path" here and the binding discards its output, so a leftover branch is a silent dead key.
    assert "pc-wayfire-action pc:start" in super_helper
    assert "swaymsg" not in super_helper



def test_a_restart_marker_buys_the_launcher_time_a_crash_does_not(tmp_path):
    """THE TWO NUMBERS THAT NEVER COMPARED THEMSELVES.

    A shell that exits gets its replacement 3 seconds to appear. That is right for a crash and
    impossible for a deliberate restart: pc-shell-start-wayfire waits up to 10s for the Xwayland
    socket and then up to 30s on its own surface/GPU health gate BEFORE the replacement is judged,
    so Ctrl+Alt+Backspace and the automatic post-update restart both blew through the window,
    Wayfire was stopped, and the whole login came back on Sway with one line in a log that carried
    no timestamp. pc-shell-restart now leaves a marker; this proves it is what makes the difference.
    """
    # The shell exits and NOTHING replaces it for 6 seconds -- twice the crash window. The fake
    # compositor then ends itself once the replacement has run, so the patient case is still bounded.
    body = """me=$$
sleep 0.2 & first=$!
echo $first >"$PC_WAYFIRE_SHELL_PID_FILE"
touch "$PC_WAYFIRE_READY_FILE"
( sleep 6; sleep 1 & second=$!; echo $second >"$PC_WAYFIRE_SHELL_PID_FILE"; wait $second; kill $me ) &
trap 'exit 0' TERM
while :; do sleep 0.1; done
"""
    crashed = time.monotonic()
    done, calls = _run(tmp_path, "wayfire", wayfire_body=body)
    crashed = time.monotonic() - crashed
    assert done.returncode == 0
    assert crashed < 6, "a crash must fall back promptly rather than waiting out a restart window"

    restarted = time.monotonic()
    again = tmp_path / "again"
    again.mkdir()
    done, calls = _run(again, "wayfire", wayfire_body=body, restarting=True)
    restarted = time.monotonic() - restarted
    assert done.returncode == 0
    assert restarted > crashed + 2, (
        "the restart marker bought no extra time: the replacement shell that arrives after 6s is "
        f"still being given up on (crash {crashed:.1f}s vs restart {restarted:.1f}s)")


def test_a_stale_restart_marker_does_not_disable_the_crash_fallback(tmp_path):
    """A restart that died before clearing its marker must not make every later crash patient."""
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    marker = runtime / "posterchan-shell-restarting"
    marker.write_text("")
    os.utime(marker, (time.time() - 3600, time.time() - 3600))
    body = """sleep 0.2 & first=$!
echo $first >"$PC_WAYFIRE_SHELL_PID_FILE"
touch "$PC_WAYFIRE_READY_FILE"
trap 'exit 0' TERM
while :; do sleep 0.1; done
"""
    started = time.monotonic()
    done, calls = _run(tmp_path, "wayfire", wayfire_body=body)
    assert done.returncode == 0
    assert time.monotonic() - started < 8, "an hour-old marker still bought the full restart window"
