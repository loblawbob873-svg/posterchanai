"""ONE BUTTON PER WINDOW; ITS CONTROLS APPEAR WHEN YOU REACH FOR THEM.

Reported as "wtf is the deal with the window control on the taskbar eating up space!", right after
"we can't have half the windows with controls and the other half no window controls".

Both are true at once, and they pull in opposite directions:

  * sway draws a title bar with NO buttons, and neither Firefox nor Telegram will negotiate
    client-side decorations (measured: `border csd` on both, they stayed `normal`). So for a
    non-hosted window the taskbar is the only place controls can live.
  * three controls beside every task button is three buttons of taskbar per open window. Measured on
    the machine with seven PosterChan windows plus Firefox and Telegram open, that is most of the
    bar — and it was only two controls then. Adding minimise (which was missing, and whose absence
    is what made people read the set as "no controls") made the space problem worse.

So the controls stay, and stop costing anything until they are wanted: hidden by default, revealed
on hover or keyboard focus of the task group. `display:none`, never `opacity` — an invisible control
still occupies its width, which is the entire complaint.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


def test_the_button_and_its_controls_are_one_group():
    """Hover has to reveal the controls from the BUTTON too, or you must already be on the tiny
    controls to make them appear."""
    assert '<span class="os-taskgroup">' in OS_JS
    bar = OS_JS[OS_JS.index("os-taskgroup"):]
    bar = bar[:bar.index("</div>")]
    assert "os-task" in bar and "os-native-controls" in bar, "the controls left the group"


def test_the_controls_are_hidden_until_wanted():
    assert ".os-taskgroup .os-native-controls{display:none}" in CSS


def test_they_appear_on_hover_and_on_keyboard_focus():
    """Hover alone would make them unreachable without a mouse."""
    rule = CSS[CSS.index(".os-taskgroup:hover"):CSS.index(".os-native-controls{display:inline-flex}",
                                                          CSS.index(".os-taskgroup:hover")) + 40]
    assert ":hover" in rule and ":focus-within" in rule


def test_hiding_is_display_none_not_opacity():
    """An invisible control that still occupies its width fixes nothing — that IS the report."""
    block = CSS[CSS.index(".os-taskgroup{"):CSS.index(".os-native-controls{display:inline-flex")]
    assert "opacity:0" not in block, "the controls are merely transparent and still take the space"
    assert "visibility:hidden" not in block


def test_all_three_controls_are_still_there():
    """The other half of the complaint: a set missing minimise reads as no controls at all."""
    for control in ("os-native-min", "os-native-max", "os-native-close"):
        assert control in OS_JS, f"{control} is gone"


def test_they_are_still_bound():
    """Controls that appear and do nothing would be the worst of both."""
    for control, call in (("os-native-min", "pcWM.hide"), ("os-native-max", "pcWM.snap"),
                          ("os-native-close", "pcWM.close")):
        line = re.search(r"\$\$\('\." + control + r"',bar\)[^\n]*", OS_JS)
        assert line, f"{control} has no binding"
        assert call in line.group(0), f"{control} is not wired to {call}"
