"""The start panel opens above the start button — in both desktop styles.

THE RULE HAS TO BE FOUND BY ITS SELECTOR, NOT BY A SUBSTRING. This test used to slice from the
first literal `.os-startmenu{` in the stylesheet, which is a substring of
`.os-root.os-style-mac .os-startmenu{` too. The moment macOS mode added its own centred panel that
search stopped landing on the base rule and the test failed against perfectly correct CSS —
a red suite that says "do not deploy" about nothing.

Both intents are pinned here now, so a future edit cannot silently swap them: the default taskbar
puts its start button on the left and the panel is anchored there; macOS mode centres its dock and
centres the panel over it.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]


def _rule(css, selector):
    """The body of the rule whose selector is exactly `selector`."""
    m = re.search(r"(?m)^" + re.escape(selector) + r"\{", css)
    assert m, "no rule for %s — the selector moved and this test stopped checking" % selector
    return css[m.end():css.index("}", m.end())]


def test_start_panel_opens_above_the_left_aligned_start_button():
    css = (ROOT / "static" / "css" / "client.css").read_text(encoding="utf-8")
    rule = _rule(css, ".os-startmenu")
    assert "inset-inline-start:10px" in rule, rule[:200]
    assert "translateX(-50%)" not in rule, rule[:200]


def test_macos_mode_centres_the_panel_over_its_centred_dock():
    """macOS mode moves the dock to the middle of the screen, so the panel has to follow it. A
    left-anchored panel under a centred dock is the same bug as the reverse."""
    css = (ROOT / "static" / "css" / "client.css").read_text(encoding="utf-8")
    panel = _rule(css, ".os-root.os-style-mac .os-startmenu")
    assert "left:50%" in panel, panel[:200]
    bar = _rule(css, ".os-root.os-style-mac .os-bar")
    # CENTRED, however it is spelled. `left:50%` with a translate and `left:0;right:0` with an auto
    # inline margin are both centring; asserting one idiom makes this fail on a rewrite that
    # changed nothing a user can see, which is exactly what this file did once already.
    centred = ("left:50%" in bar) or ("margin-inline:auto" in bar and "left:0" in bar)
    assert centred, "the dock is not centred, so centring the panel over it is wrong: " + bar[:200]
