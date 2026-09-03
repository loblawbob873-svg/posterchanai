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


def test_the_controls_are_always_visible():
    """THE CORRECTION. These were briefly hover-only to save bar space, and that was wrong: sway
    draws no title-bar buttons and neither Firefox nor Telegram will negotiate CSD, so the taskbar
    is the ONLY place a window's controls exist. Hidden behind hover, they are a secret — reported
    within minutes as "there is no window controls"."""
    assert ".os-taskgroup .os-native-controls{display:inline-flex}" in CSS
    assert ".os-taskgroup .os-native-controls{display:none}" not in CSS, (
        "the window controls are hidden again; on this desktop that means there are none")


def test_they_are_not_hidden_by_any_other_means_either():
    block = CSS[CSS.index(".os-taskgroup{"):CSS.index(".os-native-controls{display:inline-flex;align")]
    for hide in ("opacity:0", "visibility:hidden", "display:none"):
        assert hide not in block, f"the controls are hidden with {hide}"


def test_they_are_compact_so_the_space_complaint_is_still_answered():
    """The space complaint was real. It is answered by making them cheap, not by hiding them:
    18px and no gap, so three now cost less bar than two did at 22px with gaps."""
    rule = CSS.split(".os-native-controls button{", 1)[1].split("}", 1)[0]
    assert "width:18px" in rule and "height:18px" in rule
    # The FIRST `.os-native-controls{` is the group override; the sizing rule is the one that
    # carries align-items. Splitting on the bare selector matched the wrong block.
    gap = CSS.split(".os-native-controls{display:inline-flex;align-items:center;", 1)[1].split("}", 1)[0]
    assert "gap:0" in gap


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
