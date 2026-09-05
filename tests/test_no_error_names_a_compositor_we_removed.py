"""NOTHING THE USER CAN READ MAY NAME SWAY.

Reported from the WINDOWS build: System Settings -> Displays answered

    Could not read displays: Error invoking remote method 'pc:display:status':
    Error: no compositor socket - SWAYSOCK is not set

-- an environment variable belonging to a compositor this app stopped shipping, on an operating
system that has never had one. "since we are not using sway anymore, we need to make sure anything
we had for sway is changed ... that message concerns me."

Two things were wrong and both are fixed here:

  * the message named a mechanism instead of stating a fact. "this machine has no window manager to
    ask" is true of Windows, macOS, a browser, and a Linux desktop that is not PosterChanOS;
  * a host with no window manager was reaching the compositor bridge at all. `pcDisplays` is
    injected on EVERY platform -- preload cannot know what the machine is -- so its presence was
    never the question. `pc:display:status` answers null now, and the client already had the right
    sentence behind that: "Display controls are unavailable on this device."

The sway BACKEND itself is deliberately still here (`wm.js` reads SWAYSOCK/I3SOCK), because an
installed machine can still be rolled back to a Sway session -- see wm-wayfire.js. What must not
survive is a user-facing string that presents it as the thing this app runs on.
"""
from pathlib import Path
import re

ROOT = Path(__file__).resolve().parents[1]


def _strings(text):
    """Single- and double-quoted literals, with comments removed first."""
    body = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    body = re.sub(r"(?m)^\s*//.*$", "", body)
    return re.findall(r"'([^'\\\n]{4,200})'|\"([^\"\\\n]{4,200})\"", body)


def test_no_thrown_message_names_swaysock():
    for name in ("desktop/wm.js", "desktop/main.js", "desktop/displays.js",
                 "static/js/client/os.js"):
        text = (ROOT / name).read_text(encoding="utf-8")
        for a, b in _strings(text):
            literal = a or b
            assert "SWAYSOCK" not in literal, f"{name} can show the user: {literal!r}"


def test_the_display_bridge_answers_rather_than_throwing_with_no_window_manager():
    main = (ROOT / "desktop/main.js").read_text(encoding="utf-8")
    at = main.index("ipcMain.handle('pc:display:status'")
    handler = main[at: main.index("ipcMain.handle('pc:display:preview'", at)]
    assert "_canArrangeDisplays()" in handler and "return null" in handler, (
        "pc:display:status still asks the compositor on a machine that has none")

    guard = main[main.index("const _canArrangeDisplays ="):][:220]
    assert "wm().available()" in guard, "availability must be the compositor bridge's own answer"


def test_the_client_reads_that_answer_as_unavailable_not_as_empty():
    """null must not fall through to `outs=[]`, which draws an empty Displays page as if it worked."""
    os_js = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
    at = os_js.index("_settingsRead(pcDisplays.status(), 'displays')")
    block = os_js[at: at + 600]
    assert "r.value === null" in block, "a null answer is not distinguished from a real reading"
    order = block.index("r.value === null"), block.index("else if(r.ok) outs=")
    assert order[0] < order[1], "the null check must come before the general ok branch"


def test_the_error_that_remains_states_a_fact():
    wm = (ROOT / "desktop/wm.js").read_text(encoding="utf-8")
    at = wm.index("if(!this.paths.length) return Promise.reject(")
    line = wm[at: wm.index("\n", at)]
    assert "no window manager" in line, line
    assert "SWAYSOCK" not in line and "I3SOCK" not in line, line
