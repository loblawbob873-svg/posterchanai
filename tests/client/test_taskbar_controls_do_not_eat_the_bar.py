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


def _ini(text):
    """wayfire.ini as a flat key->value map. Section names do not collide across the file."""
    out = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("[") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        out[key.strip()] = value.strip()
    return out
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
WAYFIRE = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/wayfire.ini").read_text(encoding="utf-8")


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
    """The menu is a convenience; the bindings are the guarantee, and they ship in wayfire.ini.

    These carried NOTHING for a while. The four Super+arrow entries existed only to run `pc-super
    used` (which suppresses the Start menu the Super release would open) and never performed the
    window action, so keyboard snapping silently did nothing on this session while the taskbar menu
    that offers the same thing kept working. Assert the ACTION, not just that a binding exists.
    """
    for chord, action in (("KEY_LEFT", "pc-window-snap left"),
                          ("KEY_RIGHT", "pc-window-snap right"),
                          ("KEY_UP", "pc-window-snap max"),
                          ("KEY_DOWN", "pc-window-snap minimise"),
                          ("KEY_Q", "pc-window-close")):
        binding = next((k for k, v in _ini(WAYFIRE).items()
                        if k.startswith("binding_") and v.endswith(chord)), None)
        assert binding, f"no Super+{chord} binding in wayfire.ini"
        command = _ini(WAYFIRE)["command_" + binding[len("binding_"):]]
        assert action in command, f"Super+{chord} does not run {action}: {command!r}"
        assert "pc-super used" in command, (
            f"Super+{chord} does not mark the modifier consumed, so it also opens Start on release")
    # Alt+F4 is the other close, and it goes through the shell tick rather than the helper.
    assert "pc-wayfire-action pc:close" in WAYFIRE
