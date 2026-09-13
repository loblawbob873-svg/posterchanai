"""Every row in Settings → Sidebar must say what it would turn off.

Reported on Android: "User Settings -> Side Bar there is an actual blank entry". A blank switch is
unusable — you cannot tell what it controls — and it is invisible to any test that reads markup
rather than what renders.

It could not be reproduced on the current client: measured at 360px, 53 rows, every one with text
and every label with a non-zero rendered width, and all 48 nav items in the shipped template carry a
text-bearing first span. So this pins the RULE instead of chasing the build: whatever the nav
contains, `navRows` must never hand the editor a row with nothing in it.

`_navLabel` reads the FIRST <span>'s text nodes. A group header carries two spans (label, chevron)
and several rows carry a badge inside theirs, so an empty answer is reachable on a nav that differs
from this one — which is exactly the situation an APK build is in.
"""
import json
import re
import subprocess
import unittest
from pathlib import Path

APP = Path(__file__).resolve().parents[2] / "static" / "js" / "client" / "app.js"


def _lift(name):
    src = APP.read_text()
    start = src.index("function " + name + "(")
    i, depth, seen = src.index("{", start), 0, False
    while i < len(src):
        if src[i] == "{":
            depth += 1; seen = True
        elif src[i] == "}":
            depth -= 1
            if seen and depth == 0:
                return src[start:i + 1]
        i += 1
    raise AssertionError(name + " not found")


def _rows(items):
    """Run the SHIPPED _navLabel + navRows against a stub nav."""
    js = """
const NAV_LOCKED = new Set(['settings','bookmarks','blossom']);
const navHiddenSet = () => new Set();
const ITEMS = %s;
function el(spec){
  // Each label carries its TAG, and querySelector honours the selector — otherwise the stub cannot
  // tell `span` from `b` and a test for exactly that distinction passes whatever the code does.
  const mk = (tag, html) => ({ tag,
    childNodes: [...html.matchAll(/>([^<]*)</g)].length ? [] : [{nodeType:3, textContent: html}],
    cloneNode(){ const t = html.replace(/<[^>]*>/g,''); return {
      querySelectorAll(){ return []; }, get textContent(){ return t; } }; },
    querySelectorAll(){ return []; },
  });
  const labels = (spec.spans || []).map(h => mk('span', h))
              .concat((spec.bolds || []).map(h => mk('b', h)));
  return {
    dataset: spec.dataset || {}, id: spec.id || '',
    classList: { contains: c => (spec.classes||[]).includes(c) },
    querySelector: sel => {
      const want = String(sel).split(',').map(x => x.trim());
      return labels.find(l => want.includes(l.tag)) || null;
    },
  };
}
const nodes = ITEMS.map(el);
const document = { querySelectorAll: () => nodes, getElementById: () => null };
%s
%s
%s
console.log(JSON.stringify(navRows().map(r => ({key: r.key, label: r.label}))));
""" % (json.dumps(items), _lift("_navKey"), _lift("_navLabel"), _lift("navRows"))
    p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
    if p.returncode != 0:
        raise AssertionError("node failed: " + (p.stderr or "")[:500])
    return json.loads(p.stdout.strip().splitlines()[-1])


class NoBlankRow(unittest.TestCase):
    def test_an_empty_span_still_produces_a_label(self):
        """The regression, as a rule: a nav item whose span holds nothing must not render blank."""
        rows = _rows([{"dataset": {"view": "texts"}, "spans": [""]}])
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["label"].strip(), "a row rendered with no label at all")

    def test_a_span_with_no_span_at_all_still_produces_a_label(self):
        rows = _rows([{"dataset": {"view": "weird"}, "spans": []}])
        self.assertTrue(rows and rows[0]["label"].strip(),
                        "a nav item with no span produced a blank row")

    def test_a_whitespace_label_is_not_accepted(self):
        rows = _rows([{"dataset": {"view": "spacey"}, "spans": ["   "]}])
        self.assertTrue(rows[0]["label"].strip(),
                        "a whitespace-only label was passed through as the row's name")

    def test_an_invisible_label_is_not_accepted(self):
        """THE ONE WAY A BLANK ROW IS STILL REACHABLE.

        `String.trim()` removes whitespace but NOT a zero-width space, a joiner or a BOM, so a
        label of "\u200b" is truthy, survives the trim, beats the `|| key` fallback and draws a row
        with nothing in it. Every other blank shape is already impossible here — which is why this
        is the case the rule has to cover to be worth anything.
        """
        for ch in ("\u200b", "\ufeff", "\u200d", "\u2028"):
            rows = _rows([{"dataset": {"view": "ghost"}, "spans": [ch]}])
            visible = rows[0]["label"].strip().strip(ch)
            self.assertTrue(visible,
                            "a label of %r drew a row with nothing in it" % ch)
            self.assertIn("ghost", rows[0]["label"])

    def test_a_bold_label_is_read_too(self):
        """The phone's bottom bar writes `<b>Home</b>`; the sidebar writes `<span>Home</span>`.

        `_navLabel` read only `span`, so any row using the bar's markup answered '' — the shape most
        likely to put an unlabelled switch in front of somebody, since both markups carry the same
        `nav-item` class.
        """
        rows = _rows([{"dataset": {"view": "home"}, "spans": [], "bolds": ["Home"]}])
        self.assertEqual(rows[0]["label"], "Home",
                         "a <b>-labelled nav row is still read as unlabelled")

    def test_a_real_label_is_untouched(self):
        rows = _rows([{"dataset": {"view": "notes"}, "spans": ["Notes"]}])
        self.assertEqual(rows[0]["label"], "Notes")

    def test_the_fallback_names_the_view_so_it_can_be_identified(self):
        """"Unnamed item" alone would be as useless as a blank one."""
        rows = _rows([{"dataset": {"view": "mystery"}, "spans": [""]}])
        self.assertIn("mystery", rows[0]["label"],
                      "the fallback does not say WHICH item it is")


if __name__ == "__main__":
    unittest.main()
