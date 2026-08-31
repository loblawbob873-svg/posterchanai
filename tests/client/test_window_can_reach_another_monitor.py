"""Moving a window to the other monitor had code, a tick handler, and no way for a person to ask.

"Terminal still don't let me move it across to the other monitor" — and before it, the same report
about Messages and about Torrents. It was never about the window.

`handoffDirection` in os.js says it in its own comment: "the title-bar Move-to-monitor action
remains the deterministic path where Wayland clamps it". `moveWindowToMonitor` was written for that
action. But there was no title-bar button, no menu entry, and no sway binding emitting
`pc:move-output:*` — the tick handler that calls it could not be reached from anything a person can
press. So the ONLY way to move a window between displays was to drag it past the edge hard enough
to produce virtual-desktop overflow, which is precisely what Wayland clamps: pointer capture pins
clientX to this surface, so the gesture needs `realScreen` established, the pointer within 12px of
the edge, AND more than 8px of overflow on two consecutive samples.

Every existing handoff test covers what happens AFTER a handoff fires — the payload, the terminal's
PTY state, the destination window. Nothing asserted that a person could start one. That is the
whole bug, and this file is that assertion.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OS_JS = (ROOT / "static" / "js" / "client" / "os.js").read_text(encoding="utf-8")


def _strip(js: str) -> str:
    js = re.sub(r"/\*.*?\*/", " ", js, flags=re.S)
    return re.sub(r"(?<![:\w])//[^\n]*", " ", js)


CODE = _strip(OS_JS)


class AWindowCanBeMovedToAnotherDisplayOnPurpose(unittest.TestCase):
    def test_there_is_a_probe_that_tries_every_neighbour(self):
        """The renderer cannot enumerate outputs, so 'the other display' is found by asking."""
        self.assertIn("async function moveToOtherMonitor(w)", CODE)
        body = CODE[CODE.index("async function moveToOtherMonitor(w)"):]
        body = body[:body.index("\n  function startDrag")]
        for direction in ("'right'", "'left'", "'down'", "'up'"):
            self.assertIn(direction, body, "the probe skips " + direction)
        self.assertIn("moveWindowToMonitor(w, direction)", body)

    def test_a_failed_move_says_so(self):
        """Silence here is indistinguishable from the bug it replaces."""
        body = CODE[CODE.index("async function moveToOtherMonitor(w)"):]
        body = body[:body.index("\n  function startDrag")]
        self.assertIn("toast(", body)

    def test_it_is_reachable_from_the_window_itself(self):
        """Nobody hunts through the taskbar for something to do to the window they are touching."""
        self.assertIn("$('.osw-bar', el).addEventListener('contextmenu'", CODE)
        menu = CODE[CODE.index("$('.osw-bar', el).addEventListener('contextmenu'"):]
        menu = menu[:menu.index("$('.osw-grip', el)")]
        self.assertIn("Move to other display", menu)
        self.assertIn("moveToOtherMonitor(w)", menu)
        # A right-click on the buttons must keep doing what those buttons do.
        self.assertIn("e.target.closest('.osw-b')", menu)

    def test_it_is_reachable_from_the_taskbar_too(self):
        self.assertEqual(CODE.count("Move to other display"), 3,
                         "expected the window menu, the taskbar menu and the native taskbar menu")

    def test_a_native_window_reaches_the_same_action(self):
        """Firefox and Telegram are hosted in our frames and move by the same route."""
        i = CODE.index("nativeTaskbarMove(w)")
        near = CODE[i:i + 400]
        self.assertIn("Move to other display", near)
        self.assertIn("nativeWins().find", near)

    def test_the_keyboard_direction_falls_back_to_the_probe(self):
        """MEASURED on the real two-monitor desk: `send_tick pc:move-output:right` with the Terminal
        already on the RIGHT-hand monitor changed ten by fourteen pixels of clock and nothing else.
        Asking for the output right of the rightmost screen is a legitimate question whose answer is
        "no", and it was answered by silence — which is the whole report. What the person meant is
        "put this on the other screen", so a refused direction falls back to the probe."""
        i = CODE.index("pc:move-output:(left|right|up|down)")
        branch = CODE[i:i + 700]
        self.assertIn("moveWindowToMonitor(w,p.slice(15))", branch)
        self.assertIn("moveToOtherMonitor(w)", branch)
        self.assertIn("if(!moved)", branch)

    def test_only_the_renderer_holding_the_focused_window_reacts(self):
        """Every output's shell receives the tick. Without the guard the other monitor announces a
        failure for a key that was never aimed at it."""
        i = CODE.index("pc:move-output:(left|right|up|down)")
        branch = CODE[i:i + 700]
        self.assertIn("wins.find(x=>x.el.classList.contains('focused'))", branch)
        self.assertIn("if(w)", branch)

    def test_the_tick_path_is_still_there_for_a_future_keybinding(self):
        """`pc:move-output:<dir>` is the compositor's way in: sway binds $mod+Shift+<arrow> to
        `pc-window-snap move-<dir>`, which sends this tick when the focused surface is our shell.
        That path works and is asserted here; it is not SUFFICIENT, because it is a direction and a
        person on the rightmost monitor has no direction that means "the other one"."""
        self.assertIn("pc:move-output:(left|right|up|down)", CODE)
        self.assertIn("moveWindowToMonitor(w,p.slice(15))", CODE)


class TheDragPathIsNotTheOnlyPath(unittest.TestCase):
    """The regression this file exists to prevent: deleting the menu entries and trusting the drag
    again. The drag's own conditions are asserted here so that anyone removing them has to read why
    they were never sufficient on their own."""

    def test_the_drag_still_requires_real_virtual_screen_overflow(self):
        body = CODE[CODE.index("const handoffDirection = (e) =>"):]
        body = body[:body.index("const handoffDrop")]
        self.assertIn("realScreen", body)
        self.assertIn("edgeOverflow(e,dir)>8", body)

    def test_and_two_consecutive_samples(self):
        self.assertIn("handoff=crossSamples>=2?candidate:''", CODE)


if __name__ == "__main__":
    unittest.main()
