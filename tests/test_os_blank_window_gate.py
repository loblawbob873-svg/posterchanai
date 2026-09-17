"""The blank-window gate's rule, and the two ways it must refuse to lie.

`scripts/check_os_blank_windows.py` needs a running PosterChanOS desktop, which this suite's host
usually is not — so the interesting parts are tested here without one: the VERDICT (what counts as
an empty window) and the exit code when there is nothing to measure. A check that cannot run must
exit 2 and be read as a SKIP; answering 0 there would report "no blank windows" about a desktop it
never looked at, which is exactly the false green this gate exists to remove.

Measured against the real thing on a headless two-output instance: 37 windows, 0 blank; with one
window's body emptied, exit 1 naming `PosterChan Window — settings` and nothing else.
"""
import importlib.util
import os
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "check_os_blank_windows.py"


def _module():
    spec = importlib.util.spec_from_file_location("check_os_blank_windows", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def gate():
    return _module()


def test_a_painted_window_passes(gate):
    assert gate.verdict({"h": 432, "chars": 766, "visible": 15}) == ""


@pytest.mark.parametrize("measurement,expected", [
    ({"h": 432, "chars": 0, "visible": 0}, "characters"),
    ({"h": 0, "chars": 900, "visible": 40}, "tall"),
    ({"h": 400, "chars": 900, "visible": 1}, "visible"),
    ({"h": 400, "chars": 2, "visible": 40}, "characters"),
])
def test_an_empty_window_is_named_with_its_numbers(gate, measurement, expected):
    why = gate.verdict(measurement)
    assert why and expected in why


def test_a_window_that_could_not_be_measured_is_never_a_pass(gate):
    """`None` is what a renderer that stopped answering returns, and it is a finding of its own."""
    assert gate.verdict(None)
    assert gate.verdict("")
    assert gate.verdict({})


def test_a_title_bar_alone_is_not_a_window(gate):
    """The failure shape: the frame, its title and its taskbar button all exist, and the surface
    inside shows nothing. Chrome measured, on a real one: h=0, chars=0, visible=0."""
    assert gate.verdict({"title": "PosterChan Window — settings", "h": 0, "chars": 0, "visible": 0})


def test_with_no_desktop_to_ask_it_exits_two_and_says_why():
    result = subprocess.run([sys.executable, str(SCRIPT)],
                            env=dict(os.environ, PC_OS_CDP="127.0.0.1:1"),
                            capture_output=True, text=True, timeout=120)
    assert result.returncode == 2, result.stdout + result.stderr
    assert "SKIP" in result.stdout
    assert "PC_OS_CDP" in result.stdout


def test_the_probe_counts_what_is_actually_visible():
    """A window can be full of nodes and show nothing: display, visibility, opacity and size each
    hide content on their own, and this repo has already shipped a screen that was painted at
    opacity 0 (`anim-off`). The probe must count all four, in the window's own renderer."""
    source = SCRIPT.read_text(encoding="utf-8")
    probe = source[source.index("PROBE = "):source.index("OPEN_EVERYTHING = ")]
    for rule in ("visibility!=='hidden'", "display!=='none'", "Number(s.opacity)>0.05",
                 "r.width>4 && r.height>4"):
        assert rule in probe, rule
    assert "spinner" in probe, "a window still on its spinner is worth naming"
