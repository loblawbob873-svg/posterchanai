"""FILE TILES SCROLLED STRAIGHT THROUGH THE TOOLBAR.

Reported: *"File Manger bug: ALL -> as you scroll, files scrolling above buttons"*.

`.fx-bar` — the breadcrumbs, the search box and the view switch — shares a scroller with the file
grid and had neither a background nor a stacking position, so scrolling drew tiles over it. Sticky
rather than fixed, because the bar belongs to whichever pane it is in (the drive, the Explorer's
right half, a popped-out window) and not to the viewport; and opaque, because a transparent sticky
bar is the same bug with the tiles merely dimmer.

The sibling rule `.files-selbar` already had exactly this treatment, which is the clearest evidence
it was an omission rather than a decision.
"""
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")


def _rule(selector):
    body = CSS.split("\n" + selector + "{", 1)[1].split("}", 1)[0]
    return " ".join(body.split())


def test_the_toolbar_stays_put_and_is_opaque():
    rule = _rule(".fx-bar")
    assert "position:sticky" in rule, rule
    assert "top:0" in rule, rule
    assert "background:" in rule, "a transparent sticky bar still has tiles showing through it"
    assert "z-index:" in rule, rule


def test_the_toolbar_is_above_everything_that_scrolls_under_it():
    """This used to REQUIRE the toolbar and the selection bar to share z-index:3 "so the two must not
    drift". Equal was the bug: two sticky strips at the same top and level, so the selection bar slid
    over the toolbar, and a list row's ⋯ menu (z-index:4) painted over both -- on a phone, every scroll
    (APK 1.0.2371). The rule that holds: the toolbar is sticky, opaque, and above an OPEN row menu;
    inside the Explorer the selection bar does not stick at all."""
    fx = _rule(".fx-bar")
    for prop in ("position:sticky", "top:0", "background:var(--bg)"):
        assert prop in fx, (prop, fx)
    bar_z = int(re.search(r"z-index:(\d+)", fx).group(1))
    open_menu = re.search(r"\.fx-mobile-actions\[open\]\{z-index:(\d+)\}", CSS)
    assert open_menu and bar_z > int(open_menu.group(1)), "an open row menu must stay UNDER the toolbar"
    assert not re.search(r"\.fx-mobile-actions\{[^}]*z-index", CSS), "a CLOSED row menu must not be raised"
    assert ".fx-explorer .files-selbar{position:static}" in CSS


def test_the_grid_is_the_thing_that_scrolls():
    """If the grid stopped being the scroller the sticky bar would silently stop sticking."""
    assert "overflow:auto" in _rule(".bp-explorer>.files-grid")
