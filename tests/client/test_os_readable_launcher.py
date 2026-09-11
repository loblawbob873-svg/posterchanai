"""PosterChanOS launcher geometry remains readable after the desktop zoom is applied."""

from pathlib import Path
import re


ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "static/css/client.css").read_text()
JS = (ROOT / "static/js/client/os.js").read_text()


def _rule(selector: str) -> str:
    """The rule whose selector IS this, not the first rule whose selector ENDS with it.

    `CSS.index(".os-app{")` matches inside `.os-root.os-style-mac .os-startmenu .os-app{...}` too,
    so adding any descendant rule above the base one silently shadowed it and this file started
    asserting font sizes against a two-property macOS override. Anchor on a rule boundary — start of
    file, or after the previous rule's `}` / a `,` in a selector list — which is what "the .os-app
    rule" always meant.
    """
    m = re.search(r"(?:^|[}\n,])\s*" + re.escape(selector) + r"\{", CSS)
    assert m, "no rule for %s" % selector
    start = CSS.index(selector + "{", m.start())
    return CSS[start:CSS.index("}", start) + 1]


def test_desktop_icon_css_and_drag_geometry_agree():
    icon = _rule(".os-icon")
    values = re.search(r"width:(\d+)px;height:(\d+)px", icon)
    assert values
    constants = re.search(
        r"const ICON_W = (\d+), ICON_H = (\d+), ICON_GAP = (\d+)", JS
    )
    assert constants
    assert values.groups() == constants.groups()[:2]
    assert int(values.group(1)) >= 120
    assert int(values.group(2)) >= 100


def test_launcher_labels_survive_high_resolution_shell_scaling():
    """A FLOOR, NOT A FIXED NUMBER — and the difference matters twice.

    This pinned 14/17/14 exactly. What it is actually about is legibility: the shell runs on a
    3072x2048 output under `body{zoom}`, and a launcher label that shrinks is one nobody can read
    from a seat. The exact step belongs to the TYPE SCALE, which `scripts/check_css_scale.py` owns
    and which has since moved two of these onto the nearest ladder step (14 -> 15) — so the fixed
    numbers failed for a change that made the labels slightly LARGER, which is a test that gets
    edited rather than read.

    Stated as a floor it still fails on the regression it exists for (a snap down to 13 or 11), and
    it additionally refuses a value off the ladder, which is how 14 got here in the first place."""
    ladder = {11, 12, 13, 15, 17, 20, 24, 30}

    def size(selector):
        m = re.search(r"font-size:(\d+)px", _rule(selector))
        assert m, "no font-size in the %s rule" % selector
        return int(m.group(1))

    for selector, floor in ((".os-icon span", 14), (".os-app", 17), (".os-stat", 14)):
        got = size(selector)
        assert got >= floor, (
            "%s is %dpx — below the %dpx legibility floor for a launcher read at arm's length on a "
            "scaled 3072x2048 output" % (selector, got, floor))
        assert got in ladder, (
            "%s is %dpx, which is off the type scale check_css_scale.py enforces" % (selector, got))


def test_start_menu_has_room_for_larger_rows():
    menu = _rule(".os-startmenu")
    assert "width:min(780px" in menu
    assert "height:min(920px,calc(100vh - 78px))" in menu
    assert "max-height:calc(100vh - 78px)" in menu
