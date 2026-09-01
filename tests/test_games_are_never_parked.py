"""A FULLSCREEN GAME IS NOT A WINDOW TO MANAGE — and every change to the desktop must keep believing it.

Asked for directly while the window rewrite was in flight: "make sure this works with steam games
good too don't forget".

Steam titles are fullscreened by the compositor (`for_window [class="^steam_app_.*"] fullscreen
enable, inhibit_idle fullscreen`), and the desktop's placement pass already knows to leave them
alone — the comment beside the guard records what happens otherwise: "Turning every new surface
floating in place() silently cancelled a game's fullscreen/pointer lock and let the mouse escape to
another monitor."

Three things have to stay true, and each is one line away from not being:

  1. The fullscreen branch runs BEFORE the parking branch. If parking ever wins, a PosterChan window
     overlapping a game takes the game off the screen mid-play and replaces it with a screenshot.
  2. Parking a game would also mean grim-CAPTURING it — a full-resolution screen grab every time a
     window moves over it, which is a stutter you cannot debug from a log.
  3. The compositor rule that makes a game fullscreen in the first place has to survive edits to
     sway.config — the window rewrite added rules to that same file.

This is a source-order test on purpose. The ordering is the property, and it is invisible to any
test that only asks whether both branches exist.
"""
from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
SWAY = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/sway.config").read_text(encoding="utf-8")
MAIN_JS = (ROOT / "desktop/main.js").read_text(encoding="utf-8")


def _placement_loop() -> str:
    start = OS_JS.index("const plan = NAT().stashPlan(")
    return OS_JS[start:OS_JS.index("_natShell=null; _natShellAt=0; _natAgain=true;", start)]


def test_a_fullscreen_client_is_decided_before_anything_can_park_it():
    """THE ORDERING, which is the whole guarantee. A game reaches its own branch and leaves the loop
    before the stash branch can consider it."""
    loop = _placement_loop()
    assert "it.w.nativeFullscreen" in loop, "the fullscreen guard is gone from the placement pass"
    assert "stash.has(it.native)" in loop, "re-read this test: the parking branch has moved"
    assert loop.index("it.w.nativeFullscreen") < loop.index("stash.has(it.native)"), (
        "parking is now decided before fullscreen, so a PosterChan window overlapping a game would "
        "take the game off the screen and leave a screenshot in its place")


def test_the_fullscreen_branch_leaves_the_loop():
    """It must `continue`, not fall through into placement — placing a fullscreen surface is what
    cancelled the pointer lock."""
    loop = _placement_loop()
    branch = loop[loop.index("if(it.w.nativeFullscreen){"):]
    branch = branch[:branch.index("it.w.el.classList.remove('native-fullscreen-frame')")]
    assert "continue;" in branch


def test_a_game_is_never_screen_captured():
    """Previews exist for parked windows. A game is never parked, so it is never grabbed — but the
    capture path also refuses on its own, because a full-resolution grim of a running game is a
    stutter with nothing in any log to explain it."""
    handler = MAIN_JS[MAIN_JS.index("ipcMain.handle('pc:wm:preview'"):]
    handler = handler[:handler.index("ipcMain.handle('pc:wm:close'")]
    assert "target.stashed" in handler and "visible===false" in handler.replace(" ", ""), (
        "the preview no longer refuses a window that is not parked and visible")


def test_the_compositor_still_fullscreens_steam_titles():
    """The rule the whole contract rests on, in a file the window rewrite also edits."""
    assert re.search(r'for_window \[class="\^steam_app_\.\*"\] fullscreen enable', SWAY), (
        "the Steam fullscreen rule is gone from sway.config")
    assert "inhibit_idle fullscreen" in SWAY, "a game would let the screen blank mid-play"


def test_the_new_window_rule_cannot_match_a_game():
    """The rewrite floats PosterChan windows by TITLE while sharing the desktop's app_id. That match
    must be anchored to our own application, or a game whose title happened to start the same way
    would be dragged out of fullscreen."""
    for line in SWAY.splitlines():
        if 'title="^PosterChan Window"' in line:
            assert "app_id=" in line or "class=" in line, (
                "the window float rule matches on title alone, so any client could match it: " + line)
            assert "posterchan" in line.lower(), (
                "the window float rule is not anchored to our own application: " + line)
