"""The phone live overlay has TWO audio controls during a native screen share: the mic and the screen.

    "When I go live with my phone sharing screen, the 'mute' button mutes my mic AND the screen
     audio. I want to control it individually."

stream_audio_controls_runtime.mjs runs the SHIPPED streams.js (go-live, the overlay, both toggles,
minimize, a re-render, the camera → screen hand-over) against a fake ScreenShare plugin that models the
capture service's two independent inputs, and asserts on what the fake service would put on air. The
Android half (where the two inputs actually come from) is tests/test_android_screenshare_audio.py.
"""
import json
import os
import re
import shutil
import subprocess

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
HARNESS = os.path.join(ROOT, "tests", "client", "stream_audio_controls_runtime.mjs")
STREAMS = os.path.join(ROOT, "static", "js", "client", "streams.js")
CSS = os.path.join(ROOT, "static", "css", "client.css")


@pytest.fixture(scope="module")
def results():
    if not shutil.which("node"):
        pytest.skip("no node on this node")
    r = subprocess.run(["node", HARNESS, STREAMS], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
    out = {}
    for line in r.stdout.splitlines():
        if line.startswith("{"):
            d = json.loads(line)
            out[d["name"]] = d
    assert "harness" not in out, out.get("harness")
    return out


CHECKS = [
    "native share is live",
    "screen-audio button exists",
    "screen-audio button shown for a native share with screen audio",
    "mic button still says Mute",
    "mic toggle mutes the mic",
    "mic toggle leaves the screen audio on air",
    "mic toast names the mic",
    "mic button says Unmute",
    "screen button unchanged by the mic",
    "screen toggle mutes the screen audio",
    "screen toggle leaves the mic as it was",
    "screen toast names the screen audio",
    "screen button says muted",
    "unmuting the mic does not unmute the screen",
    "minimize keeps the buttons",
    "re-render keeps the mic state",
    "re-render keeps the screen state",
    "no screen audio -> no screen button",
    "camera stream never shows the screen button",
    "camera mute disables the mic track",
    "switch to screen carries the mic mute",
    "switch to screen carries a screen mute field",
    "after the switch the screen button appears",
    "after the switch the mic is still muted on air",
]


@pytest.mark.parametrize("name", CHECKS)
def test_audio_controls(results, name):
    assert name in results, f"check did not run: {name}"
    assert results[name]["ok"], f"{name}: {results[name]['detail']}"


def test_a_hidden_overlay_button_is_not_drawn():
    """`.btn:has(.b-ic)` and `button:not(.tl-fab):has(> svg.ic.b-ic:only-child)` (specificity 0,4,2) both set
    display:inline-flex, which beats the [hidden] attribute — so the Screen-audio button would be drawn on a
    camera stream (and Share screen was drawn during every native share). Measured in headless Chrome at
    390px: a NON-important `.phone-live .pl-actions .btn[hidden]` (0,4,0) still loses, so it must be
    !important."""
    with open(CSS, encoding="utf-8") as fh:
        css = fh.read()
    assert re.search(r"\.phone-live \.pl-actions \.btn\[hidden\]\{display:none\s*!important\}", css)
