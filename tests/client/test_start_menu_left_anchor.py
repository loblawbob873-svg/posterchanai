"""The start panel opens above the start button.

THE RULE HAS TO BE FOUND BY ITS SELECTOR, NOT BY A SUBSTRING. This test used to slice from the
first literal `.os-startmenu{` in the stylesheet, which is a substring of
any descendant rule such as `.x .os-startmenu{` too. The moment a more specific panel rule existed
that search stopped landing on the base rule and the test failed against perfectly correct CSS —
a red suite that says "do not deploy" about nothing.
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
