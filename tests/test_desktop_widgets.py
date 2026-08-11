"""Desktop widgets, as the DOCUMENT sees them — the shipped `_normDoc` under node.

Run: venv-unified/bin/python -m unittest tests.test_desktop_widgets

Widgets live in the same encrypted `pcai:desktop` document as the icon arrangement, and `_normDoc` is
the one place its invariants are enforced. Everything it decides fails silently on screen: a widget
that stops appearing, one that draws off the edge of a smaller laptop, a document a newer client
wrote that a older one has to survive reading.

The rules asserted here are decisions, not accidents:

  * POSITIONS ARE FRACTIONS, not pixels. Icons store pixels and clamp, which keeps them on screen but
    not where you put them — a panel against the right edge of a 2560px monitor belongs against the
    right edge of the laptop that opens the same account, not 1500px into the middle of it. This is
    what "widgets should resize going from a tablet to a desktop and back" actually requires.
  * SIZE IS A NAME, so the real width comes from the screen. A fixed pixel width is either cramped on
    a desktop or covers a tablet.
  * An UNKNOWN TYPE is dropped rather than drawn: an empty frame nothing can fill and nothing
    explains is worse than the widget being absent. This is also the forward-compatibility rule — a
    document written by a newer client must not be able to break the desktop of an older one.
  * `cfg` is a small flat bag, bounded in count and length. It is where a widget remembers its city;
    it is not a place to store documents, and it is read on every draw.
  * The whole list is capped, because this document is the one thing here a half-finished write or a
    future version could put anything in.

The rendering half (drawing, dragging, the shared timer) is scripts/check_os_desktop.py's job.
"""
import json
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OS_JS = ROOT / "static" / "js" / "client" / "os.js"

BOOT = """
global.window = {};
global.document = { addEventListener(){}, querySelector(){ return null; },
                    querySelectorAll(){ return []; } };
global.getComputedStyle = () => ({ zoom: '1' });
require(%s);
const PCOS = window.PCOS;
""" % json.dumps(str(OS_JS))


def _node(script: str):
    out = subprocess.run(["node", "-e", BOOT + script], capture_output=True, timeout=60)
    if out.returncode != 0:
        raise AssertionError(out.stderr.decode()[-2000:])
    return json.loads(out.stdout.decode() or "null")


def norm(doc):
    return _node(f"console.log(JSON.stringify(PCOS.__normDoc({json.dumps(doc)})))")


@unittest.skipUnless(shutil.which("node"), "node not installed")
class WidgetDocument(unittest.TestCase):
    def test_a_widget_survives_a_round_trip(self):
        d = norm({"widgets": [{"id": "w1", "type": "crypto", "x": 0.5, "y": 0.25, "size": "l",
                               "cfg": {"place": "Home"}}]})
        self.assertEqual(d["widgets"], [{"id": "w1", "type": "crypto", "x": 0.5, "y": 0.25,
                                         "size": "l", "cfg": {"place": "Home"}}])

    def test_an_unknown_type_is_dropped(self):
        """A document written by a newer client must not draw a frame this one cannot fill."""
        d = norm({"widgets": [{"id": "a", "type": "stockmarket3000"},
                              {"id": "b", "type": "weather"}]})
        self.assertEqual([w["type"] for w in d["widgets"]], ["weather"])

    def test_positions_are_fractions_and_are_clamped(self):
        """Off-scale values are the ones that put a panel where it cannot be reached — and 1.0 has to
        survive intact, because that is 'against the right edge', the most useful position there is."""
        d = norm({"widgets": [
            {"id": "a", "type": "crypto", "x": 1, "y": 0},
            {"id": "b", "type": "crypto", "x": 4.5, "y": -3},
            {"id": "c", "type": "crypto", "x": "nonsense", "y": None},
        ]})
        self.assertEqual([[w["x"], w["y"]] for w in d["widgets"]], [[1, 0], [1, 0], [0, 0]])

    def test_an_unknown_size_becomes_the_middle_one(self):
        d = norm({"widgets": [{"id": "a", "type": "crypto", "size": "enormous"},
                              {"id": "b", "type": "crypto"},
                              {"id": "c", "type": "crypto", "size": "s"}]})
        self.assertEqual([w["size"] for w in d["widgets"]], ["m", "m", "s"])

    def test_two_widgets_cannot_share_an_id(self):
        """Every mutation finds its row by id; a duplicate means removing one removes both, and a cfg
        write lands on whichever came first."""
        d = norm({"widgets": [{"id": "same", "type": "crypto"}, {"id": "same", "type": "weather"}]})
        self.assertEqual(len(d["widgets"]), 1)
        self.assertEqual(d["widgets"][0]["type"], "crypto")

    def test_a_widget_with_no_id_still_gets_one(self):
        d = norm({"widgets": [{"type": "crypto"}, {"type": "weather"}]})
        ids = [w["id"] for w in d["widgets"]]
        self.assertTrue(all(ids), f"a widget with no id is unaddressable: {ids}")
        self.assertEqual(len(set(ids)), 2)

    def test_the_list_is_capped(self):
        d = norm({"widgets": [{"id": f"w{i}", "type": "crypto"} for i in range(60)]})
        self.assertLessEqual(len(d["widgets"]), 12)
        self.assertGreater(len(d["widgets"]), 0)

    def test_cfg_is_bounded_in_both_directions(self):
        """It is read on every draw of the desktop, and it is caller-written."""
        big = {f"k{i}": i for i in range(40)}
        big["text"] = "x" * 5000
        big["nested"] = {"no": "objects"}
        big["arr"] = [1, 2, 3]
        d = norm({"widgets": [{"id": "a", "type": "note", "cfg": big}]})
        cfg = d["widgets"][0]["cfg"]
        self.assertLessEqual(len(cfg), 12)
        for v in cfg.values():
            self.assertIn(type(v), (str, int, float, bool), f"cfg kept a {type(v)}")
        if "text" in cfg:
            self.assertLessEqual(len(cfg["text"]), 400)

    def test_a_document_with_no_widgets_key_is_fine(self):
        """Every desktop arranged before this feature existed has exactly that shape."""
        d = norm({"order": ["home"], "folders": [], "hidden": [], "pos": {}})
        self.assertEqual(d["widgets"], [])

    def test_junk_in_place_of_the_list_does_not_throw(self):
        for bad in ("nope", 42, {"not": "an array"}, None):
            self.assertEqual(norm({"widgets": bad})["widgets"], [], f"widgets={bad!r}")

    def test_widgets_reach_computeLayout(self):
        """_normDoc alone is not enough — the drawing code reads them off the computed layout, and a
        rule that stops there would leave the desktop bare with the document intact."""
        got = _node("""
          const lay = PCOS.__layout([{view:'home',label:'Home',icon:'#i-home'}],
                                    { widgets: [{ id:'w1', type:'weather', x:0.2, y:0.8, size:'s' }] });
          console.log(JSON.stringify(lay.widgets || null));
        """)
        self.assertEqual(got, [{"id": "w1", "type": "weather", "x": 0.2, "y": 0.8,
                                "size": "s", "cfg": {}}])


@unittest.skipUnless(shutil.which("node"), "node not installed")
class WidgetSizing(unittest.TestCase):
    """`wgtBox` is what makes a widget fit the screen it is on rather than the one it was placed on."""

    def box(self, size, w, h):
        return _node(f"console.log(JSON.stringify(PCOS.__wgtBox({json.dumps(size)}, {w}, {h})))")

    def test_a_big_desktop_gets_the_full_size(self):
        self.assertEqual(self.box("l", 2560, 1400), {"w": 380, "h": 250})

    def test_a_small_desktop_shrinks_it(self):
        """A tablet must not be covered by one panel — nor left with a widget too small to read."""
        b = self.box("l", 700, 420)
        self.assertLess(b["w"], 380)
        self.assertLessEqual(b["w"], 700 * 0.46 + 1)
        self.assertGreaterEqual(b["w"], 150)
        self.assertGreaterEqual(b["h"], 96)

    def test_it_never_exceeds_the_desk_even_when_tiny(self):
        b = self.box("l", 240, 200)
        self.assertLessEqual(b["w"], 240)
        self.assertLessEqual(b["h"], 200)


if __name__ == "__main__":
    unittest.main()
