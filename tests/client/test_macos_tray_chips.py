"""The machine's own tray has to fit the macOS menu bar it is put in.

`#os-shell` (`.os-sys`) is filled by osshell.js — wifi, battery, volume, power. macOS mode moves the
whole tray out of the Dock and into a 30px menu bar (`placeDesktopTray`), and there were NO macOS
rules for that container at all: its chips kept the sizing meant for a 48px taskbar, and kept
`color:var(--text)`, which on a light theme is dark text on the dark glass of the menu bar.

Reported as the wifi/power widget displaying weird and cut off.
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


def test_the_chips_are_legible_on_the_dark_menu_bar():
    """`.os-chip{color:var(--text)}` wins over the menu bar's inherited white. On a light theme
    that is dark text on dark glass — the widget is there and unreadable."""
    chip = _rule(".os-root.os-style-mac .os-sys .os-chip")
    assert "color:#fff" in chip, chip
    unknown = _rule(".os-root.os-style-mac .os-sys .os-chip.os-unknown")
    assert "255,255,255" in unknown, "an unreadable reading must stay marked against THIS bar"


def test_the_tray_cannot_grow_across_the_menu_bar():
    """The menu bar also carries the app menu. An unbounded machine tray pushes it off screen on a
    laptop, which is where this was reported."""
    sysrule = _rule(".os-root.os-style-mac .os-sys")
    assert "overflow:hidden" in sysrule and "max-width" in sysrule, sysrule
