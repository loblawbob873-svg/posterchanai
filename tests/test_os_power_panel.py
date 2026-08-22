"""The power flyout and the volume flyout are the same panel.

    "power button menu is garabage! it's a menu that opens up top-left. make it nice, centered, big!"
    "maybe power button just needs to be improved to function like the volume section"
    "consistent UI experiuence and clean"

Both were already `openPop`, so the machinery was shared. What was not shared were the two arguments
that make the quick panel look like a panel: without `align:'end'` the flyout is laid out from the
chip's LEFT edge, and the tray chips sit at the right-hand end of the taskbar, so it walks across the
screen; without `os-pop-quick` it falls back to the generic 230-320px box beside a neighbour that is
a fixed 340. Two chips, one taskbar, two different panels.

This asserts the two agree, which is the actual requirement — not that either looks a particular way.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "static/js/client/osshell.js").read_text()
CSS = (ROOT / "static/css/client.css").read_text()


def call(fn):
    """The `openPop(...)` call inside a named function, brace-matched from its definition."""
    i = JS.index("function " + fn)
    depth, k, start = 0, JS.index("{", i), None
    for k in range(JS.index("{", i), len(JS)):
        if JS[k] == "{":
            depth += 1
            if start is None:
                start = k
        elif JS[k] == "}":
            depth -= 1
            if depth == 0:
                break
    body = JS[start:k]
    # Paren-matched, not regex-matched: the call carries a template literal full of parentheses and
    # braces, and a regex that tries to describe that is a regex that quietly matches nothing.
    i = body.index("openPop(")
    depth, j = 0, i + len("openPop(") - 1
    for j in range(i + len("openPop(") - 1, len(body)):
        if body[j] == "(":
            depth += 1
        elif body[j] == ")":
            depth -= 1
            if depth == 0:
                break
    return body, body[i:j + 1]


class ThePowerAndVolumePanelsAgree(unittest.TestCase):

    def test_both_are_anchored_the_same_way(self):
        """The tray is at the right-hand end of the bar. A flyout laid out from a chip's left edge
        hangs off the display, which is the "opens up top-left" report."""
        for fn in ("powerPop", "quickPop"):
            with self.subTest(panel=fn):
                _, c = call(fn)
                self.assertIn("align: 'end'", c,
                              "%s does not anchor to the chip's right edge" % fn)

    def test_both_are_the_same_width(self):
        """Not "wide enough" — the SAME. Two panels of different widths on one taskbar is the
        inconsistency being reported, whatever the numbers are."""
        for fn in ("powerPop", "quickPop"):
            with self.subTest(panel=fn):
                _, c = call(fn)
                self.assertIn("os-pop-quick", c,
                              "%s does not use the shared panel width" % fn)
        self.assertIn(".os-pop-quick{", CSS.replace(" ", ""))

    def test_every_action_has_an_icon_and_a_label(self):
        """A column of bare words is what "garbage" was describing; every other row in this tray has
        a glyph beside it."""
        body, _ = call("powerPop")
        for act in ("suspend", "reboot", "poweroff"):
            with self.subTest(action=act):
                self.assertIn("'%s'" % act, body)
        self.assertIn("ICO(", body, "the power rows draw no icons")

    def test_the_rows_are_a_pointer_target_not_a_list_item(self):
        """Every entry here loses whatever is on screen, and one of them shuts the machine down."""
        m = re.search(r"\.os-pop-power-acts \.os-pop-row\{([^}]*)\}", CSS)
        self.assertIsNotNone(m, "the power rows have no sizing of their own")
        pad = re.search(r"padding:(\d+)px", m.group(1))
        self.assertIsNotNone(pad, m.group(1))
        self.assertGreaterEqual(int(pad.group(1)), 10,
                                "the power rows are as tight as an ordinary menu line")

    def test_actions_are_a_large_two_column_grid(self):
        rule = re.search(r"\.os-pop-power-acts\{([^}]*)\}", CSS, re.S)
        self.assertIsNotNone(rule)
        self.assertIn("display:grid", rule.group(1).replace(" ", ""))
        self.assertIn("grid-template-columns:repeat(2", rule.group(1).replace(" ", ""))
        tile = re.search(r"\.os-pop-power-acts \.os-pop-row\{([^}]*)\}", CSS, re.S)
        self.assertIsNotNone(tile)
        self.assertIn("min-height:112px", tile.group(1).replace(" ", ""))
        self.assertIn("aspect-ratio:", tile.group(1))

    def test_the_dangerous_one_still_looks_dangerous(self):
        body, _ = call("powerPop")
        self.assertIn("os-pop-danger", body)
        self.assertIn("os-pop-danger", CSS)

    def test_taller_subpanel_is_remeasured_and_kept_above_taskbar(self):
        self.assertIn("requestAnimationFrame(() => positionPop(sub, _popAnchor, _popOpts))", JS)
        position = JS[JS.index("function positionPop"):JS.index("function openPop")]
        self.assertIn("d.offsetHeight", position)
        self.assertIn("T - h - 10", position)
        self.assertIn("d.style.maxHeight", position)
