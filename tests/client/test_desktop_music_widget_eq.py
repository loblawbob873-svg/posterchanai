"""The desktop Now-playing widget grows the same animated equaliser bars the player/profile show,
and they animate ONLY while a track plays.

Reported as the desktop music widget being plain next to the regular player. It now carries a
.wgt-eq bar strip that dances via a `.playing` class the widget's refresh() sets from the player
state — reusing the existing @keyframes profEq so it is one bar animation, not a second copy.
"""
import subprocess
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


def test_the_widget_markup_has_the_equaliser_bars():
    block = OS_JS[OS_JS.index("label: 'Now playing'"):OS_JS.index("label: 'Now playing'") + 4000]
    assert 'class="wgt-eq"' in block, "the Now-playing widget has no equaliser strip"
    assert "Array.from({length:14}" in block, "the eq should render its bars"


def test_the_bars_animate_only_while_playing_and_reuse_the_shared_keyframes():
    assert ".wgt-music.playing .wgt-eq i{animation:profEq" in CSS, \
        "bars must animate via the shared profEq keyframes, gated on .playing"
    assert "@keyframes profEq" in CSS, "the reused keyframes must exist"
    # At rest (no .playing) there is no animation on the bars.
    idle = CSS[CSS.index(".wgt-eq{"):CSS.index(".wgt-music.playing .wgt-eq{")]
    assert "animation:" not in idle, "the idle bars must not animate"


def test_refresh_toggles_playing_from_the_player_state_runtime():
    r = subprocess.run(["node", str(ROOT / "tests/client/music_widget_eq_runtime.mjs")],
                       capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
