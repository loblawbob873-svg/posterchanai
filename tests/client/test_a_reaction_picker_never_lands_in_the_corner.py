"""THE EMOJI PICKER OPENED IN THE TOP-LEFT CORNER, AND THAT IS WHAT "REACT IS BROKEN" LOOKED LIKE.

Reported four ways in one sitting -- "clicking emoji on a chat does nothing", "react broken", and
then exactly: "on webui, it loads the emoji picker in the top-left corner, on windows app, you don't
see it at all".

Every term in the placement is relative to the anchor, so an anchor with no box collapses the whole
computation to `{left: 8, top: 6}`. That is not an error anybody can see as an error: nothing
throws, nothing logs, and the picker IS on screen -- in the corner, hundreds of pixels from the
message it belongs to.

The anchor was hidden by the caller itself. The click handler calls `closeMessageActions()` before
placing, which strips `cc-actions-open`; under `(hover:none)` -- which a touch-capable Windows
machine reports -- the CSS then makes the toolbar's buttons `display:none`. The button that was just
clicked stops having a box, and is then measured.

Two rules, because one of them alone comes back. The rect may be handed in, taken before anything
was closed; and a degenerate rect is REFUSED rather than honoured, because the placer cannot tell a
corner somebody wants from a corner that means "I could not measure".
"""
import json
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
SIM = ROOT / "tests/client/reaction_picker_sim.mjs"

pytestmark = pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")

VISIBLE = {"width": 30, "height": 28, "left": 900, "top": 400, "right": 930, "bottom": 428}
HIDDEN = {"width": 0, "height": 0, "left": 0, "top": 0, "right": 0, "bottom": 0}


def _place(**plan):
    out = subprocess.run(["node", str(SIM), json.dumps(plan)], cwd=ROOT, text=True,
                         capture_output=True, timeout=60)
    assert out.returncode == 0, out.stderr[:2000]
    return json.loads(out.stdout)["at"]


def test_a_hidden_anchor_is_refused_rather_than_placed_in_the_corner():
    """The whole bug: {left:8, top:6} is a valid position and a useless one."""
    assert _place(anchor=HIDDEN) is None


def test_a_rect_measured_before_the_toolbar_closed_places_it_correctly():
    at = _place(anchor=HIDDEN, measured=VISIBLE)
    assert at is not None
    assert at["left"] > 500 and at["top"] > 300, at


def test_a_visible_anchor_is_unchanged():
    """The fix must not move the picker for everybody it already worked for."""
    assert _place(anchor=VISIBLE) == _place(anchor=VISIBLE, measured=VISIBLE)


def test_it_sits_beside_the_button_not_across_the_screen():
    at = _place(anchor=VISIBLE)
    assert abs(at["left"] + 140 - VISIBLE["right"]) <= 1, at   # right-aligned to the button
    assert at["top"] >= VISIBLE["bottom"], at                   # below it


def test_a_button_near_the_bottom_flips_above_instead_of_off_screen():
    low = {"width": 30, "height": 28, "left": 900, "top": 870, "right": 930, "bottom": 898}
    at = _place(anchor=low, viewport={"width": 1600, "height": 900})
    assert at["top"] + 80 <= 900, at


def test_it_stays_inside_a_narrow_viewport():
    edge = {"width": 30, "height": 28, "left": 350, "top": 100, "right": 380, "bottom": 128}
    at = _place(anchor=edge, viewport={"width": 390, "height": 780})
    assert at["left"] >= 8 and at["left"] + 140 <= 390 - 8 + 1, at


def test_a_zero_width_but_tall_anchor_is_still_refused():
    """Half a box is not a box. A collapsed-width control would pin the picker to the left margin."""
    assert _place(anchor={"width": 0, "height": 28, "left": 900, "top": 400,
                          "right": 900, "bottom": 428}) is None
