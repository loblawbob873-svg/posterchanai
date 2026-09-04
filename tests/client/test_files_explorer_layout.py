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


def test_it_matches_the_selection_bar_that_already_did_this():
    """Same scroller, same problem, and that one was already solved — so the two must not drift."""
    fx, sel = _rule(".fx-bar"), _rule(".files-selbar")
    for prop in ("position:sticky", "top:0", "z-index:3", "background:var(--bg)"):
        assert prop in fx, (prop, fx)
        assert prop in sel, (prop, sel)


def test_the_grid_is_the_thing_that_scrolls():
    """If the grid stopped being the scroller the sticky bar would silently stop sticking."""
    assert "overflow:auto" in _rule(".bp-explorer>.files-grid")
