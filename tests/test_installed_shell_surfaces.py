"""The installed-desktop output gate, now that the compositor is Wayfire.

It used to demand `SWAYSOCK` and shell out to `swaymsg`, so on the Wayfire session it could only
ever answer SKIP -- which in a report is indistinguishable from a machine nobody ran it on. The
RULE is unchanged and is the one that matters: every active output must be covered by exactly one
full-size, package-backed shell surface, or a monitor is black and nothing says so.
"""
import importlib.util
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location(
    "installed_shell_surfaces", ROOT / "scripts" / "check_installed_shell_surfaces.py")
GATE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(GATE)


def output(name, x):
    return {"name": name, "geometry": {"x": x, "y": 0, "width": 1920, "height": 1080}}


def surface(view_id, x, mapped=True):
    return {"id": view_id, "title": "PosterChan · Nostr", "app-id": "place.poster.desktop",
            "mapped": mapped, "geometry": {"x": x, "y": 0, "width": 1920, "height": 1080}}


def test_each_active_output_requires_one_exact_full_size_shell_surface():
    got = GATE.validate([output("DP-1", 0), output("DP-2", 1920)],
                        [surface(10, 0), surface(11, 1920)])
    assert got == {"outputs": 2, "surfaces": 2, "geometry": ["1920x1080", "1920x1080"]}


@pytest.mark.parametrize("views", [
    [surface(10, 0)],
    [surface(10, 0), surface(11, 0), surface(12, 1920)],
    [surface(10, 0), surface(11, 1920, mapped=False)],
])
def test_missing_duplicate_or_unmapped_companion_is_a_black_output_failure(views):
    with pytest.raises(RuntimeError, match="full visible shell surfaces"):
        GATE.validate([output("DP-1", 0), output("DP-2", 1920)], views)


def test_inactive_outputs_do_not_require_a_renderer():
    """A disconnected output has no geometry to cover. Wayfire simply stops listing its size."""
    disconnected = output("DP-2", 1920)
    disconnected["geometry"] = {"x": 0, "y": 0, "width": 0, "height": 0}
    assert GATE.validate([output("DP-1", 0), disconnected], [surface(10, 0)])["outputs"] == 1


def test_normal_floating_app_windows_are_not_misreported_as_extra_desktops():
    app = {"id": 12, "title": "PosterChan Window — terminal", "app-id": "place.poster.desktop",
           "mapped": True, "geometry": {"x": 200, "y": 100, "width": 1100, "height": 760}}
    got = GATE.validate([output("DP-1", 0)], [surface(10, 0), app])
    assert got["surfaces"] == 1


def test_a_layer_shell_popup_is_not_a_shell_surface():
    """Wayfire marks its own layer-shell clients `role: desktop-environment`. A notification popup
    that happens to fill an output must not be counted as that output's desktop."""
    popup = dict(surface(99, 0)); popup["role"] = "desktop-environment"
    got = GATE.validate([output("DP-1", 0)], [surface(10, 0), popup])
    assert got["surfaces"] == 1


def test_headless_host_is_an_explicit_missing_prerequisite(monkeypatch):
    """The socket question lives in scripts/wayfire_ipc.py now — one client for three gates, after
    this package already shipped two copies of one helper that drifted apart."""
    monkeypatch.setattr(GATE.subprocess, "run", lambda *args, **kwargs:
                        type("Result", (), {"stdout": "XDG_RUNTIME_DIR=/run/user/1\n"})())
    monkeypatch.delenv("WAYFIRE_SOCKET", raising=False)
    with pytest.raises(GATE.PrerequisiteMissing, match="no installed Wayfire IPC session"):
        GATE.wf.socket_path()


def test_a_socket_path_that_no_longer_exists_is_missing_not_present(monkeypatch, tmp_path):
    """A stale WAYFIRE_SOCKET from a previous session is the shape that made this gate try to
    connect and report a FAILURE about a desktop that is simply not running."""
    monkeypatch.setenv("WAYFIRE_SOCKET", str(tmp_path / "gone.sock"))
    with pytest.raises(GATE.PrerequisiteMissing):
        GATE.wf.socket_path()
