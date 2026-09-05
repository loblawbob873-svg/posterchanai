"""THE START MENU IS ITS WINDOW, AND THIS IS THE MEASUREMENT THE OTHER TESTS COULD NOT MAKE.

Reported four times -- "start menu is not attached to the taskbar, looks weird", then "why is the
start menu and taskbar widgets not attaachd to the dam taskbar still!" -- and marked fixed twice,
because what was measured each time was the WINDOW. The window was never wrong. Measured on the
laptop with the shipped build, Wayfire reporting a taskbar top of 1043:

    start popup window   geometry 8,327  601x708   -> bottom 1035, 8px above the bar   (correct)
    .os-startmenu inside it        10,22  581x630   -> bottom  652, i.e. 979 on screen

`.os-startmenu` is positioned for the DESKTOP: `inset-inline-start:10px`, `bottom:56px` to clear the
taskbar it opens above, `height:min(920px, calc(100vh - 78px))` for the same reason. Hosted in its
own popup window there is no taskbar and no wallpaper, so those offsets became 56px of empty
translucent window UNDER the menu and 10px of it either side -- a visible 64px gap between the menu
and the bar it is anchored to, with a translucent skirt hanging beneath it.

`.os-noti` and `.os-pop` were each given a "a panel hosted in a popup window fills it" rule when
they moved into windows. The start menu was not, and nothing noticed because every existing check
reads the numbers os.js sends to the compositor.

WHY THIS TEST RESOLVES THE CASCADE INSTEAD OF GREPPING. The desktop rule and the mac skin's
`.os-root.os-style-mac .os-startmenu` (which adds `left:50%` and a `translate(-50%,0)`) both set the
same properties, so "the popup rule exists" is not the question -- "does it win" is. The order and
specificity are computed here the way a browser computes them.
"""
from pathlib import Path
import re
import unittest


ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "static/css/client.css").read_text(encoding="utf-8")
OS_JS = (ROOT / "static/js/client/os.js").read_text(encoding="utf-8")

# The element under test: the menu, drawn inside a start popup window.
ELEMENT = {"os-startmenu"}
ANCESTORS = [{"os-popup-body", "os-popup-start", "native", "desktop"},   # <body>
             {"os-popup-host"}]                                          # its host


def _rules(css):
    """(selector, declarations, source order) for every top-level rule, media blocks skipped.

    A media query's contents are indented in this stylesheet and none of them touch .os-startmenu;
    parsing them as ordinary rules would attribute an `@media` prelude to a selector.
    """
    out, i, order = [], 0, 0
    depth_skip = None
    while True:
        brace = css.find("{", i)
        if brace < 0:
            return out
        prelude = css[i:brace]
        # Strip comments from the prelude so a `{` inside one cannot be read as a rule.
        prelude_clean = re.sub(r"/\*.*?\*/", "", prelude, flags=re.S).strip()
        end = css.find("}", brace)
        if prelude_clean.startswith("@"):
            # Skip the whole at-block by matching braces.
            depth, j = 0, brace
            while j < len(css):
                if css[j] == "{":
                    depth += 1
                elif css[j] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                j += 1
            i = j + 1
            continue
        out.append((prelude_clean, css[brace + 1:end], order))
        order += 1
        i = end + 1


def _matches(sel):
    """Does this compound-selector chain match the element in ANCESTORS + ELEMENT?

    Only the shapes this stylesheet uses for `.os-startmenu` need to be understood: descendant
    combinators of class-only compounds, optionally with a leading element/`html`/`body`.
    """
    parts = sel.strip().split()
    if not parts:
        return False
    target = parts[-1]
    classes = set(re.findall(r"\.([A-Za-z0-9_-]+)", target))
    if re.sub(r"\.[A-Za-z0-9_-]+", "", target).strip() not in ("", "*"):
        return False
    if not classes or not classes <= ELEMENT:
        return False
    # Every earlier compound must match some ancestor, in order (outermost first).
    pool = list(ANCESTORS)
    for compound in parts[:-1]:
        want = set(re.findall(r"\.([A-Za-z0-9_-]+)", compound))
        bare = re.sub(r"\.[A-Za-z0-9_-]+", "", compound).strip()
        if bare not in ("", "*", "html", "body"):
            return False
        hit = next((k for k, anc in enumerate(pool) if want <= anc), None)
        if hit is None:
            return False
        pool = pool[hit + 1:]
    return True


def _specificity(sel):
    target = sel.strip()
    ids = len(re.findall(r"#[A-Za-z0-9_-]+", target))
    cls = len(re.findall(r"\.[A-Za-z0-9_-]+", target)) + len(re.findall(r"\[[^\]]+\]", target))
    return (ids, cls, 0)


def resolve():
    """The declared value of each property that reaches the menu, browser order."""
    winners = {}
    for prelude, body, order in _rules(CSS):
        for sel in prelude.split(","):
            if not _matches(sel):
                continue
            spec = _specificity(sel)
            decls = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
            for decl in decls.split(";"):
                if ":" not in decl:
                    continue
                prop, _, val = decl.partition(":")
                prop, val = prop.strip(), val.strip()
                if not prop:
                    continue
                important = "!important" in val
                key = (important, spec, order)
                cur = winners.get(prop)
                if cur is None or key > cur[0]:
                    winners[prop] = (key, val)
    return {p: v for p, (_, v) in winners.items()}


class TestTheMenuFillsItsWindow(unittest.TestCase):
    def setUp(self):
        self.won = resolve()
        self.assertTrue(self.won, "no rule in the stylesheet reaches .os-startmenu at all")

    def test_the_stylesheet_knows_this_popup_kind(self):
        """os.js must actually stamp the class the rule is written for."""
        self.assertIn("os-popup-start", OS_JS,
                      "renderStartPopup does not mark the body, so the rule can never apply")
        self.assertIn("os-popup-start", CSS, "no stylesheet rule for the start popup")

    def test_it_is_not_lifted_off_the_bottom_of_the_window(self):
        bottom = self.won.get("bottom", "")
        self.assertIn(bottom, ("auto", "0", "0px"),
                      f"the menu is held {bottom} above the bottom of its own window; "
                      "on the desktop that clears the taskbar, in a window it is a gap")

    def test_it_is_not_inset_from_the_sides(self):
        for prop in ("inset-inline-start", "left"):
            val = self.won.get(prop)
            if val is None:
                continue
            self.assertIn(val, ("auto", "0", "0px"),
                          f"{prop}:{val} insets the menu inside a window sized for it")
        self.assertIn(self.won.get("transform", "none"), ("none",),
                      "a skin's re-centring transform survives into the popup window")

    def test_it_takes_the_whole_height_and_width(self):
        self.assertEqual(self.won.get("height", ""), "100vh",
                         "the menu is shorter than the window that was placed for it")
        self.assertEqual(self.won.get("width", ""), "100%",
                         "the menu is narrower than the window that was placed for it")
        maxh = self.won.get("max-height", "100vh")
        self.assertIn(maxh, ("100vh", "none"),
                      f"max-height:{maxh} claws back the height the rule above just gave it")

    def test_the_desktop_rule_is_what_this_replaces(self):
        """Prove the check can fail: with the popup rules removed, the desktop offsets win."""
        global CSS
        keep = CSS
        try:
            CSS = re.sub(r"\.os-popup-body\.os-popup-start[^{]*\{[^}]*\}", "", CSS)
            fallen = resolve()
        finally:
            CSS = keep
        self.assertEqual(fallen.get("bottom"), "56px",
                         "the rule being tested is not the one holding the menu down")
        self.assertNotEqual(fallen.get("height"), "100vh")


if __name__ == "__main__":
    unittest.main()
