"""THE TASKBAR IS A LIST OF WINDOWS; ITS CONTROLS LIVE IN THE RIGHT-CLICK MENU.

This file has now been through the whole argument, and the conclusion is worth keeping because each
step was a real report:

  * the task button had maximise and close only — two thirds of a set, which reads as none:
    "we can't have half the windows with controls and the other half no window controls";
  * minimise was added, and three glyphs beside every app ate the bar:
    "wtf is the deal with the window control on the taskbar eating up space";
  * they were hidden behind hover, which on this desktop means gone, because sway draws no
    title-bar buttons and neither Firefox nor Telegram will negotiate CSD:
    "there is no window controls";
  * they were made visible again, and the answer was still no: "i do not want to see _ [] X on
    every taskbar app ... rightclick on a taskbar open app will suffice".

So the inline controls are gone. Nothing is lost: the right-click menu already carried Move, Move to
other display, Snap left, Snap right and Close, and it now carries Minimize/Restore — the one it
never had, which is what the inline buttons were added for in the first place. The keyboard has all
three too (Super+Q / Alt+F4 close, Super+Up maximise, Super+Down minimise).
"""
from __future__ import annotations

from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
SWAY = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config").read_text(encoding="utf-8")


def _ctx_menu() -> str:
    start = OS_JS.index("$$('.os-task', bar).forEach(b => b.oncontextmenu")
    return OS_JS[start:start + 2600]


def test_there_are_no_inline_controls_on_a_task_button():
    """THE REQUEST, stated as the rule."""
    for gone in ("os-native-min", "os-native-max", "os-native-close", "os-taskgroup"):
        assert gone not in OS_JS, f"{gone} is back on the taskbar"


def test_their_styling_went_with_them():
    """Dead CSS for a removed control is how it quietly comes back."""
    for gone in ("os-native-controls", "os-taskgroup"):
        assert gone not in CSS


def test_the_right_click_menu_has_all_three():
    body = _ctx_menu()
    assert "'Close'" in body
    assert "'Maximize'" in body
    assert "Minimize" in body and "Restore" in body, (
        "the menu still has no minimise — which is exactly why the inline buttons were added")


def test_minimise_uses_the_same_path_as_the_task_button():
    """`hide` is the scratchpad toggle the button already uses; a second notion of 'minimised'
    would mean one control could not undo the other."""
    body = _ctx_menu()
    assert "pcWM.hide(w.id)" in body and "pcWM.show(w.id)" in body


def test_the_menu_still_offers_moving_and_snapping():
    body = _ctx_menu()
    for label in ("'Move'", "'Snap left'", "'Snap right'", "'Move to other display'"):
        assert label in body, f"{label} was lost from the menu"


def test_the_task_button_itself_survives():
    """Removing the controls must not remove the window list."""
    assert '<button class="os-task${w.focused' in OS_JS


def test_the_keyboard_can_still_do_all_three():
    """The menu is a convenience; the bindings are the guarantee, and they ship in sway.config."""
    assert "bindsym $mod+q exec /usr/local/bin/pc-window-close" in SWAY
    assert "bindsym Mod1+F4 exec /usr/local/bin/pc-window-close" in SWAY
    assert "bindsym $mod+Up    exec /usr/local/bin/pc-window-snap max" in SWAY
    assert "bindsym $mod+Down  exec /usr/local/bin/pc-window-snap minimise" in SWAY
