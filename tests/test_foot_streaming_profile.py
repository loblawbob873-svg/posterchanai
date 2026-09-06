"""PosterChanOS Foot must coalesce partial streaming-TUI redraws.

Codex and Claude update their terminal UI in bursts.  Foot's own manual explains that rendering the
clear from one write before the replacement text from the next is perceived as screen flicker.  The
OS wrapper raises Foot's deliberately provided delayed-render window while keeping both bounds under
one 60 Hz frame.  This is package/runtime behavior, not just an installer-only tweak.
"""
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
WRAPPER = ROOT / "os/overlay/app-misc/posterchanos-shell/files/foot"
EBUILD = ROOT / "os/overlay/app-misc/posterchanos-shell/posterchanos-shell-1.0.0.ebuild"
INSTALLER = ROOT / "os/gentoo.sh"


def test_wrapper_uses_foots_flicker_coalescing_controls():
    src = WRAPPER.read_text(encoding="utf-8")
    assert src.startswith("#!/bin/sh\n")
    assert "exec /usr/bin/foot" in src
    assert "tweak.delayed-render-lower=2000000" in src
    assert "tweak.delayed-render-upper=12000000" in src
    assert src.index("tweak.delayed-render-upper") < src.index('"$@"'), (
        "user arguments must come last so an explicit -o remains authoritative")


def test_delays_are_ordered_and_less_than_one_60hz_frame():
    src = WRAPPER.read_text(encoding="utf-8")
    values = {}
    for key in ("lower", "upper"):
        marker = f"tweak.delayed-render-{key}="
        values[key] = int(src.split(marker, 1)[1].split()[0].rstrip("\\"))
    assert 0 < values["lower"] < values["upper"] < 16_000_000


def test_wrapper_is_owned_by_the_upgrade_package_and_live_installer():
    ebuild = EBUILD.read_text(encoding="utf-8")
    installer = INSTALLER.read_text(encoding="utf-8")
    # The PROPERTY, not the adjacency: `foot` is installed by both halves. Pinning the two names
    # that happened to sit next to each other made adding any helper a failure in a test about the
    # terminal wrapper — which is how `pc-super` first showed up here.
    for where, text in (("ebuild", ebuild), ("installer", installer)):
        helpers = re.search(r"for helper in ([^;]+); do", text)
        assert helpers, f"the {where} no longer installs a list of helpers"
        assert "foot" in helpers.group(1).split(), (
            f"the {where} stopped installing the foot wrapper")


def test_the_flicker_gate_can_actually_run_on_this_desktop():
    """A GATE THAT CAN ONLY SKIP IS NOT A GATE.

    It demanded `SWAYSOCK` and drove `swaymsg`, so after the compositor changed it answered
    `SKIP SWAYSOCK is not available` for ever -- which in a report is indistinguishable from a
    machine nobody ran it on. The compositor half now goes through scripts/wayfire_ipc.py.
    """
    from pathlib import Path
    src = (Path(__file__).resolve().parents[1] / "scripts/check_installed_foot_flicker.py").read_text()
    body = src.split('"""', 2)[2]          # past the module docstring, which names what it replaced
    assert "swaymsg" not in body, "the gate shells out to Sway again"
    assert "SWAYSOCK" not in body
    assert "import wayfire_ipc as wf" in body
    assert "wf.socket_path()" in body
    assert "window-rules/configure-view" in body
    # OUTPUT-LOCAL GEOMETRY. Wayfire's configure-view coordinates are relative to output_id, so
    # passing global ones works on the left monitor and displaces every other one by its own
    # offset -- the bug the shell already paid for once in wm-wayfire.js.
    assert "output_id" in body
    assert 'int(rect["x"]) - base["x"]' in body


def test_the_flicker_gate_reads_geometry_the_way_wayfire_reports_it():
    """A gate that cannot pass is testing nothing, and this one could not.

    `settle()` waited for `node["rect"]["width"] > 100`. Wayfire views carry `geometry`, not `rect`
    -- which is precisely why `wayfire_ipc.rect_of` exists and says so in its own docstring. The
    width was therefore always 0, the wait always timed out, and the gate answered "Foot never
    mapped a window" on every run.

    Measured on the real desktop: Foot mapped at 702x530 with the marker in its title while the gate
    said it never appeared. With the normaliser in place the gate runs to completion and passes
    ("stayed nonblank across sustained output/focus/resize; 8 captures; DRM=amdgpu").
    """
    body = (ROOT / "scripts" / "check_installed_foot_flicker.py").read_text(encoding="utf-8")
    code = re.sub(r'"""[\s\S]*?"""', "", body)          # the explanation names what it replaced
    code = re.sub(r"(?m)^\s*#.*$", "", code)
    assert 'get("rect")' not in code and '["rect"]' not in code, \
        "the gate reads a key Wayfire does not publish; use wf.rect_of"
    assert "wf.rect_of(node)" in code


def test_a_blanked_screen_is_a_skip_not_a_flicker():
    """grim answers a DPMS-off output with "failed to copy output", which is not a flickering
    terminal. Hit on the real laptop, whose screen had simply blanked mid-run."""
    body = (ROOT / "scripts" / "check_installed_foot_flicker.py").read_text(encoding="utf-8")
    assert "class ScreenAsleep" in body
    assert "_output_is_off()" in body
    assert "except ScreenAsleep" in body, "the asleep case still reports as a FAIL"
    tail = body[body.index('if __name__ == "__main__":'):]
    assert tail.index("except ScreenAsleep") < tail.index("except (AssertionError"), \
        "AssertionError is caught first, so the asleep case can never reach its own handler"
