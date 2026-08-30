"""The machine's own tray has to fit the macOS menu bar it is put in.

`#os-shell` (`.os-sys`) is filled by osshell.js — wifi, battery, volume, power. macOS mode moves the
whole tray out of the Dock and into a 30px menu bar (`placeDesktopTray`), and there were NO macOS
rules for that container at all: its chips kept the sizing meant for a 48px taskbar, and kept
`color:var(--text)`, which on a light theme is dark text on the dark glass of the menu bar.

Reported as the wifi/power widget displaying weird and cut off.

The menu bar has since stopped being a fixed dark glass and now derives from the palette, so the
fix moved with it: the chips name the bar's own ink token rather than a literal white.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "static" / "css" / "client.css").read_text(encoding="utf-8")


def _rule(selector):
    m = re.search(r"(?m)^" + re.escape(selector) + r"\{", CSS)
    assert m, "no rule for %s" % selector
    return CSS[m.end():CSS.index("}", m.end())]


def test_the_machine_tray_chips_fit_the_menu_bar():
    chip = _rule(".os-root.os-style-mac .os-sys .os-chip")
    # The same height as the relay light and the bell beside them, so the row reads as one row.
    assert "height:26px" in chip, chip
    assert "line-height:1" in chip, chip


def test_the_chips_are_legible_against_the_menu_bar_they_sit_in():
    """The chip must be coloured for THE MENU BAR, never left on whatever it inherits.

    Originally that meant a literal `color:#fff`, because the bar was a fixed dark glass on every
    theme; `.os-chip{color:var(--text)}` then put dark text on dark glass under a light theme and
    the widget was there and unreadable.

    The bar is no longer fixed — it is `var(--mac-glass)` = `--panel2`, the theme's own panel — so
    the ink is `--mac-ink` = `--text`, which is the pairing the palette itself guarantees. The rule
    that survives both designs is the one asserted here: the chip names the menu bar's OWN ink
    token, so text and surface can never again be chosen independently. Whether the pair is actually
    readable is measured, not asserted from a string: check_os_theme_contrast.py reads `macchip` on
    all nine themes under this style."""
    chip = _rule(".os-root.os-style-mac .os-sys .os-chip")
    assert "color:var(--mac-ink)" in chip, chip
    unknown = _rule(".os-root.os-style-mac .os-sys .os-chip.os-unknown")
    assert "var(--muted)" in unknown, "an unreadable reading must stay marked against THIS bar"


def test_the_tray_cannot_grow_across_the_menu_bar():
    """The menu bar also carries the app menu. An unbounded machine tray pushes it off screen on a
    laptop, which is where this was reported."""
    sysrule = _rule(".os-root.os-style-mac .os-sys")
    assert "overflow:hidden" in sysrule and "max-width" in sysrule, sysrule
