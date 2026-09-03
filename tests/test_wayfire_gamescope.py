from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
MAIN = (ROOT / "desktop/main.js").read_text()
SHELL = (ROOT / "static/js/client/osshell.js").read_text()


def test_game_category_crosses_the_theme_neutral_launcher_contract():
    assert "const game = app.group === 'Games'" in SHELL
    assert "{ waitMs: 20000, game }" in SHELL
    assert "candidates: true, game" in SHELL


def test_wayfire_games_launch_in_fullscreen_gamescope_with_steam_integration():
    block = MAIN[MAIN.index("if(opts&&opts.game&&process.env.WAYFIRE_SOCKET)") :]
    block = block[:block.index("/* TELEGRAM")]
    assert "'/usr/bin/gamescope'" in block
    assert "'-f','-b','--force-windows-fullscreen'" in block
    assert "steam?['-e']:[]" in block
    assert "'--',...list" in block
    assert "gamescope is required" in block


def test_sway_rollback_does_not_wrap_games():
    condition = "opts&&opts.game&&process.env.WAYFIRE_SOCKET"
    assert condition in MAIN
    assert "SWAYSOCK" not in condition
