"""CHANGING A MONITOR'S SCALE MUST NOT SCRAMBLE THE ARRANGEMENT.

Reported twice, most recently "changing the monitor zoom in system settings breaks the monitor
layout still! wtf".

sway positions outputs in LOGICAL pixels, and a display's logical size is its mode divided by its
scale. The settings row carries `w`/`h` taken from `rect.width`/`rect.height` — the logical size at
the CURRENT scale — and the scale control did exactly one thing:

    box.querySelector('[data-scale]').onchange = e => { r.scale = +e.target.value };

So after choosing 100% on a 3840-wide panel that was at 125%, `r.w` still said 3072 while the output
had become 3840. Apply then sent coordinates measured against a size that no longer existed, and the
monitors landed overlapping or with a gap. It also never redrew, so the map still showed the old
arrangement — there was nothing on screen to suggest anything was wrong until after it was applied.

The logical size is recomputed from the MODE (real pixels, which scale does not change), and the
displays to the right of / below the changed one shift by the difference — preserving the
arrangement the user built rather than repacking it into a different one.

These run the SHIPPED handler over row objects; no compositor and no browser.
"""
from __future__ import annotations

import json
import re
import subprocess
from pathlib import Path

import pytest

OS_JS = (Path(__file__).resolve().parents[2] / "static/js/client/os.js").read_text(encoding="utf-8")


def _handler() -> str:
    start = OS_JS.index("      box.querySelector('[data-scale]').onchange=e=>{")
    depth, i = 0, OS_JS.index("{", OS_JS.index("=>{", start))
    for j in range(i, len(OS_JS)):
        if OS_JS[j] == "{":
            depth += 1
        elif OS_JS[j] == "}":
            depth -= 1
            if depth == 0:
                return OS_JS[start:j + 2]
    raise AssertionError("scale handler")


def change_scale(rows: list, index: int, to: float) -> dict:
    body = _handler()
    body = body[body.index("=>{") + 3:body.rindex("}")]
    program = """
      const rows = %(rows)s;
      const r = rows[%(i)d];
      let drew = 0;
      const draw = () => { drew++; };
      const e = { target: { value: %(to)s } };
      (function(){ %(body)s })();
      process.stdout.write(JSON.stringify({rows, drew}));
    """ % {"rows": json.dumps(rows), "i": index, "to": json.dumps(str(to)), "body": body}
    done = subprocess.run(["node", "-e", program], capture_output=True, text=True, timeout=60)
    assert done.returncode == 0, done.stderr[-800:]
    return json.loads(done.stdout)


def two_monitors():
    """A 3840x2160 panel at 125% (logical 3072) with a second one immediately to its right."""
    return [
        {"name": "DP-1", "enabled": True, "x": 0, "y": 0, "w": 3072, "h": 1728,
         "scale": 1.25, "mode": "3840x2160@60Hz"},
        {"name": "DP-2", "enabled": True, "x": 3072, "y": 0, "w": 3072, "h": 1728,
         "scale": 1.25, "mode": "3840x2160@60Hz"},
    ]


def test_the_logical_size_follows_the_scale():
    """THE BUG: the width stayed at the old scale's value."""
    got = change_scale(two_monitors(), 0, 1)
    assert got["rows"][0]["w"] == 3840, f"logical width not recomputed: {got['rows'][0]['w']}"
    assert got["rows"][0]["h"] == 2160


def test_the_monitor_to_the_right_moves_out_of_the_way():
    """Otherwise the widened display lands underneath its neighbour."""
    got = change_scale(two_monitors(), 0, 1)
    assert got["rows"][1]["x"] == 3840, (
        f"the second monitor is still at {got['rows'][1]['x']} while the first is now 3840 wide — "
        f"they overlap by {3840 - got['rows'][1]['x']}px")


def test_shrinking_closes_the_gap_too():
    rows = two_monitors()
    rows[0].update(scale=1, w=3840, h=2160)
    rows[1]["x"] = 3840
    got = change_scale(rows, 0, 1.25)
    assert got["rows"][0]["w"] == 3072
    assert got["rows"][1]["x"] == 3072, "a gap is left where the display shrank"


def test_a_monitor_that_is_not_to_the_right_is_left_alone():
    """Only what sat beyond the changed edge moves — an arrangement the user built by hand must
    survive a scale change."""
    rows = two_monitors()
    rows[1].update(x=0, y=1728)          # stacked BELOW, not beside
    got = change_scale(rows, 0, 1)
    assert got["rows"][1]["x"] == 0, "a monitor below was shoved sideways"
    assert got["rows"][1]["y"] == 2160, "the monitor below did not follow the taller display"


def test_it_redraws():
    """It never did, so the map kept showing the old arrangement and the first sign of trouble was
    after Apply."""
    assert change_scale(two_monitors(), 0, 1)["drew"] >= 1


def test_choosing_the_scale_it_already_has_changes_nothing():
    got = change_scale(two_monitors(), 0, 1.25)
    assert got["rows"][0]["w"] == 3072 and got["rows"][1]["x"] == 3072


def test_a_row_with_no_mode_string_is_not_destroyed():
    """`mode` can be empty when the compositor reported no current mode; falling back to the old
    logical size times the old scale keeps the arithmetic sane instead of producing NaN."""
    rows = two_monitors()
    rows[0]["mode"] = ""
    got = change_scale(rows, 0, 1)
    assert got["rows"][0]["w"] == 3840


def test_a_disabled_monitor_is_not_moved():
    rows = two_monitors()
    rows[1]["enabled"] = False
    got = change_scale(rows, 0, 1)
    assert got["rows"][1]["x"] == 3072
