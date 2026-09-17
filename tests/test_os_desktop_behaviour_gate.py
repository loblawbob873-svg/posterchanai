"""The desktop-behaviour gate's verdicts, and the two coordinate facts it is built on.

`scripts/check_os_desktop_behaviour.py` needs a real compositor, which this suite's host is not — so
what is tested here is everything that can be wrong WITHOUT one: what counts as a snap, what counts
as switching windows, and the two measured facts that made a working desktop look broken while this
was being written. Both are in the script as comments and both are asserted, because a future
"cleanup" that drops either one reintroduces a false failure that takes a device to diagnose.

Measured against the real thing (headless two-output PosterChanOS): alt-tab ok, clipboard ok,
snap -> half (640 of 1280), cross-monitor HEADLESS-1 -> HEADLESS-2, 0 of 4 failed.
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_os_desktop_behaviour.py"


@pytest.fixture(scope="module")
def gate():
    spec = importlib.util.spec_from_file_location("check_os_desktop_behaviour", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


SCREEN = {"x": 0, "y": 0, "width": 1280, "height": 720}


@pytest.mark.parametrize("geometry,expected", [
    # The real measurement from the device: half the width, and ~100px shorter than the output
    # because the taskbar and the frame's margins take it. Demanding the full height reads this
    # correct snap as a miss.
    ({"x": 0, "y": 10, "width": 640, "height": 640}, "half"),
    ({"x": 640, "y": 10, "width": 640, "height": 660}, "half"),
    ({"x": 0, "y": 0, "width": 1280, "height": 700}, "full"),
    ({"x": 259, "y": 59, "width": 763, "height": 590}, "floating"),
    # Half the width but a stub of height is not a snap, it is a small window at the edge.
    ({"x": 0, "y": 10, "width": 640, "height": 200}, "floating"),
])
def test_what_counts_as_a_snap(gate, geometry, expected):
    assert gate.snapped(geometry, SCREEN) == expected


def test_an_unmeasurable_window_is_never_a_snap(gate):
    assert gate.snapped(None, SCREEN) == "unknown"
    assert gate.snapped({"x": 0, "y": 0, "width": 640, "height": 640}, None) == "unknown"


def test_switching_means_reaching_another_window(gate):
    app = {"id": 11, "title": "PosterChan Window — terminal"}
    other = {"id": 13, "title": "PosterChan Window — notes"}
    desktop = {"id": 5, "title": "PosterChan Desktop"}
    assert gate.moved_focus(app, other) is True
    # Landing back on the desktop surface is what the cycle did when it was broken, and it is not
    # switching windows — the whole point of Alt+Tab here is to reach an application.
    assert gate.moved_focus(app, desktop) is False
    assert gate.moved_focus(app, app) is False
    assert gate.moved_focus(None, other) is False
    assert gate.moved_focus(app, None) is False


def test_the_cursor_is_global_while_a_view_is_output_local():
    """The fact that cost the most: a window reported x=259 on an output starting at x=1280. Press
    at 259 and the pointer is on the OTHER screen, grabs nothing, and the drag reads as "snapping is
    broken". It passes anyway on whichever output happens to start at zero, which is what makes it
    worth pinning rather than remembering."""
    source = SCRIPT.read_text(encoding="utf-8")
    grab = source[source.index("def _grab(view):"):source.index("def check_snap(")]
    assert "screen[\"x\"] + box[\"x\"]" in grab
    assert "screen[\"y\"] + box[\"y\"]" in grab
    assert "OUTPUT-LOCAL" in grab


def test_the_button_shape_that_actually_presses():
    """stipc answers an error for any other spelling, and nothing reads it: the pointer then glides
    over the title bar without pressing, and a working snap measures as free-floating."""
    source = SCRIPT.read_text(encoding="utf-8")
    press = source[source.index("def button(down):"):source.index("def cursor(")]
    assert '"combo": "BTN_LEFT"' in press
    assert '"mode": "press" if down else "release"' in press
    # And the refusal is not swallowed, or the drag silently measures nothing again.
    assert "raise Skip" in press


def test_a_tick_is_routed_by_the_runtime_dir_not_the_wayfire_socket():
    """`pc-wayfire-action` opens $XDG_RUNTIME_DIR/posterchan-action.sock. Pointing WAYFIRE_SOCKET at
    a test session while leaving XDG_RUNTIME_DIR alone sends the keypress to somebody else's
    desktop — which is exactly what happened here, to a live session."""
    source = SCRIPT.read_text(encoding="utf-8")
    assert "XDG_RUNTIME_DIR, NOT" in source


def test_with_no_desktop_it_exits_two_and_says_why():
    result = subprocess.run([sys.executable, str(SCRIPT)],
                            env=dict(os.environ, PC_OS_CDP="127.0.0.1:1",
                                     PC_OS_WF="/nonexistent/wayfire.sock"),
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "SKIP" in result.stdout
