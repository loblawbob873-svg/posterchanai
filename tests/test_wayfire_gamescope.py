"""GAMES LAUNCH DIRECTLY. THE NESTED COMPOSITOR IS A TOOL, NOT THE PATH.

Every Games-category launch used to be wrapped in `gamescope -f -b --force-windows-fullscreen`
(plus `-e` for Steam), gated on `WAYFIRE_SOCKET` — so it did nothing for as long as Sway was the
session and switched itself on the day Wayfire became it. Two faults landed together:

  * No `-W/-H` was passed, and gamescope's Wayland backend defaults its nested display to 1280x720.
    Measured with exactly those flags on the real machine: `NESTED_DISPLAY SIZE 1280 x 720`,
    upscaled to a 3840x2560 panel — 2.4x, and 16:9 into a 3:2 output.
  * Steam's own desktop entry carries `Categories=Network;FileTransfer;Game;`, so the CLIENT was a
    "game" too: the store, the library and every settings window went through it, with `-e` putting
    Steam into its Deck-session contract.

Reported as "why is steam and all the steam games running in steam full screen mode? games look
like shit now". The Sway session launched games directly and that worked.
"""
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "desktop/main.js").read_text()
SHELL = (ROOT / "static/js/client/osshell.js").read_text()
WAYFIRE = (ROOT / "os/overlay/app-misc/posterchanos-shell/files/wayfire.ini").read_text()


def _block():
    b = MAIN[MAIN.index("if(opts&&opts.gamescope&&process.env.WAYFIRE_SOCKET)"):]
    return b[:b.index("/* TELEGRAM")]


def test_game_category_crosses_the_theme_neutral_launcher_contract():
    assert "const game = app.group === 'Games'" in SHELL
    assert "{ waitMs: 20000, game }" in SHELL
    assert "candidates: true, game" in SHELL


def test_being_a_game_does_not_by_itself_start_a_nested_compositor():
    """`opts.game` is what the launcher already knew; wrapping is a SEPARATE decision."""
    assert "opts&&opts.game&&process.env.WAYFIRE_SOCKET" not in MAIN, (
        "every Games-category launch goes through gamescope again — including the Steam client, "
        "whose own .desktop file says Categories=...;Game;")
    assert "opts&&opts.gamescope&&process.env.WAYFIRE_SOCKET" in MAIN


def test_the_nested_display_is_never_left_at_its_720p_default():
    block = _block()
    assert "'/usr/bin/gamescope'" in block
    assert "--force-windows-fullscreen" not in block, (
        "this forces every window to the nested display's size, which is the 720p default")
    assert "'-W',String(w),'-H',String(h)" in block, (
        "gamescope defaults its nested display to 1280x720; without the output's real mode every "
        "game renders at 720p and is upscaled")
    assert "steam?['-e']:[]" in block
    assert "'--',...list" in block


def test_a_missing_gamescope_still_launches_the_game():
    """The migration notes require Gamescope AND a direct fallback to work. It used to refuse."""
    block = _block()
    assert "gamescope is required" not in block
    assert "launch directly" in block


def test_no_compositor_rule_pretends_to_fullscreen_a_game():
    """`set fullscreen` is not an action Wayfire 0.10's window-rules has — it logs "Unsupported set
    operation to identifier" and does nothing. A config line that looks like the feature is what
    stops anyone asking where the fullscreen actually comes from."""
    assert "then set fullscreen" not in "\n".join(
        l for l in WAYFIRE.splitlines() if l.startswith("rule_"))
    # The real path, and the idle half that made the old rule's second clause unnecessary.
    assert "wm-actions/set-fullscreen" in (ROOT / "desktop/wm-wayfire.js").read_text()
    assert "disable_on_fullscreen = true" in WAYFIRE
