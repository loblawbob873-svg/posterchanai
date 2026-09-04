import os
import subprocess
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


def _run(tmp_path, selected=None, wayfire_status=0, wayfire_body=None):
    bindir = tmp_path / "bin"
    home = tmp_path / "home"
    bindir.mkdir()
    home.mkdir()
    calls = tmp_path / "calls"
    _fake(bindir, "sway", f'echo "sway:$XDG_CURRENT_DESKTOP" >>"{calls}"\nexit 0\n')
    body = wayfire_body or f"exit {wayfire_status}\n"
    _fake(bindir, "wayfire", f'[ "$1" = --version ] && exit 0\necho "wayfire:$XDG_CURRENT_DESKTOP:$*" >>"{calls}"\n{body}')
    _fake(bindir, "pc-wayfire-health", 'exit "${PC_HEALTH_STATUS:-0}"\n')
    env = os.environ | {"PATH": f"{bindir}:/usr/bin:/bin", "HOME": str(home),
                        "PC_WAYFIRE_HEALTH": str(bindir / "pc-wayfire-health")}
    if selected:
        env["PC_COMPOSITOR"] = selected
    done = subprocess.run(["/bin/sh", str(LAUNCHER)], env=env, text=True,
                          capture_output=True, timeout=10)
    return done, calls.read_text(encoding="utf-8").splitlines()


def test_old_desktop_backend_is_rejected_before_wayfire_takes_the_display(tmp_path):
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
    assert calls == ["sway:sway"]
    log = tmp_path / "home/.local/state/posterchanos/compositor-fallback.log"
    assert "installed desktop is not Wayfire-ready" in log.read_text(encoding="utf-8")


def test_sway_is_the_safe_default(tmp_path):
    done, calls = _run(tmp_path)
    assert done.returncode == 0
    assert calls == ["sway:sway"]


def test_wayfire_failure_falls_back_to_sway(tmp_path):
    done, calls = _run(tmp_path, "wayfire", wayfire_status=23)
    assert done.returncode == 0
    assert calls == ["wayfire:wayfire:-c /etc/wayfire.ini", "sway:sway"]
    log = tmp_path / "home/.local/state/posterchanos/compositor-fallback.log"
    assert "status 23" in log.read_text(encoding="utf-8")


def test_unknown_selection_cannot_disable_the_desktop(tmp_path):
    done, calls = _run(tmp_path, "broken")
    assert done.returncode == 0
    assert calls == ["sway:sway"]


def test_shell_death_after_ready_terminates_wayfire_and_falls_back(tmp_path):
    body = "sleep 0.2 & child=$!\necho $child >\"$PC_WAYFIRE_SHELL_PID_FILE\"\ntouch \"$PC_WAYFIRE_READY_FILE\"\ntrap 'exit 0' TERM\nwhile :; do sleep 0.1; done\n"
    done, calls = _run(tmp_path, "wayfire", wayfire_body=body)
    assert done.returncode == 0
    assert calls == ["wayfire:wayfire:-c /etc/wayfire.ini", "sway:sway"]


def test_wayfire_and_fallback_are_shipped_together():
    ebuild = EBUILD.read_text(encoding="utf-8")
    gentoo = GENTOO.read_text(encoding="utf-8")
    config = (FILES / "wayfire.ini").read_text(encoding="utf-8")
    assert "gui-wm/wayfire" in ebuild and "gui-wm/sway" in ebuild
    assert 'doins "${FILESDIR}/wayfire.ini"' in ebuild
    assert "pc-compositor-session" in ebuild
    assert "gui-wm/wayfire" in gentoo and "gui-wm/sway" in gentoo
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
    assert "pc-wayfire-health" in ebuild and "pc-wayfire-health" in gentoo


def test_native_drag_snap_has_edges_corners_restore_and_no_seam_switch():
    config = (FILES / "wayfire.ini").read_text(encoding="utf-8")
    for setting in ("enable_snap = true", "snap_threshold = 10",
                    "quarter_snap_threshold = 50", "enable_snap_off = true",
                    "snap_off_threshold = 10", "workspace_switch_after = -1"):
        assert setting in config
    assert "rule_3" not in config, "fullscreen games must never be rewritten as maximized windows"


def test_live_iso_requires_and_installs_both_compositor_configs():
    gentoo = GENTOO.read_text(encoding="utf-8")
    assert 'pseudoput "etc/sway/config"' in gentoo
    assert 'pseudoput "etc/wayfire.ini"' in gentoo
    assert "PosterChanOS Wayfire fallback config was not found" in gentoo


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
    assert "WAYFIRE_SOCKET" in restart and "pc-shell-start-wayfire" in restart


def test_super_opens_start_only_when_not_used_for_snap_drag_or_resize():
    config = (FILES / "wayfire.ini").read_text(encoding="utf-8")
    super_helper = (FILES / "pc-super").read_text(encoding="utf-8")
    assert "release_binding_start = KEY_LEFTMETA" in config
    assert "command_start = /usr/local/bin/pc-super tap" in config
    for gesture in ("<super> KEY_LEFT", "<super> KEY_RIGHT", "<super> KEY_UP", "<super> KEY_DOWN",
                    "<super> BTN_LEFT", "<super> BTN_RIGHT"):
        assert gesture in config
    assert config.count("/usr/local/bin/pc-super used") >= 6
    assert "WAYFIRE_SOCKET" in super_helper
    assert "pc-wayfire-action pc:start" in super_helper
