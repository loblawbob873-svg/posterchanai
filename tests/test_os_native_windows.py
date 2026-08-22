"""A Linux app inside a PosterChan window — the arithmetic, run.

Firefox on PosterChanOS must not look like firefox on top of PosterChanOS. It gets one of our
windows, and the real Wayland surface is held over that window's body. Every failure here puts the
app somewhere wrong rather than throwing, which is why this is measured rather than eyeballed.
"""
import json
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "static", "js", "client", "osnative.js")
NODE = shutil.which("node") or shutil.which("nodejs")


@unittest.skipIf(not NODE, "needs node")
class NativeWindowGeometry(unittest.TestCase):
    def js(self, body):
        src = ("const N = require(%s);\nconst out = {};\n%s\n"
               "process.stdout.write(JSON.stringify(out));" % (json.dumps(MOD), body))
        r = subprocess.run([NODE, "-e", src], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        return json.loads(r.stdout)

    # ---- scale -------------------------------------------------------------------------------
    def test_the_scale_is_measured_from_the_shells_own_window(self):
        """The client scales itself with body{zoom} and the display has its own factor. Their
        product is not devicePixelRatio and cannot be guessed — but the shell's window is one
        rectangle visible in both coordinate systems at once."""
        out = self.js("out.s = N.scaleFrom({x:0, y:0, width:3840, height:2160}, 1920, 1080);")
        self.assertEqual(out["s"]["x"], 2.0)
        self.assertEqual(out["s"]["y"], 2.0)

    def test_an_unmeasurable_scale_is_null_not_one(self):
        """Placing a window with the wrong scale is worse than not placing it: the app is moved
        somewhere confidently wrong and redraws itself there."""
        out = self.js("out.a = N.scaleFrom(null, 1920, 1080);"
                      "out.b = N.scaleFrom({x:0,y:0,width:0,height:0}, 1920, 1080);"
                      "out.c = N.scaleFrom({x:0,y:0,width:1920,height:1080}, 0, 0);")
        self.assertIsNone(out["a"])
        self.assertIsNone(out["b"])
        self.assertIsNone(out["c"])

    def test_a_second_display_does_not_send_the_app_to_the_other_screen(self):
        """On a laptop with an external display the output the desktop is on can start at x=1920.
        A window placed at "100" would land on the wrong screen entirely."""
        out = self.js("""
          const s = N.scaleFrom({x:1920, y:0, width:1920, height:1080}, 1920, 1080);
          out.r = N.mapRect({left:100, top:50, width:800, height:600}, s);
        """)
        self.assertEqual(out["r"], {"x": 2020, "y": 50, "w": 800, "h": 600})

    def test_the_body_rect_is_where_the_app_goes_not_the_whole_window(self):
        """Our title bar is HTML above the surface. Mapping the frame instead of its body puts the
        app over its own title bar, which is then unclickable."""
        out = self.js("""
          const s = N.scaleFrom({x:0, y:0, width:1920, height:1080}, 1920, 1080);
          out.r = N.mapRect({left:200, top:132, width:900, height:600}, s);
        """)
        self.assertEqual(out["r"]["y"], 132)

    def test_a_zero_area_body_is_refused(self):
        """A parked, minimised or display:none container measures zero. Sent to the compositor it
        makes the app redraw at 1x1 and forget its layout — the caller stashes instead."""
        out = self.js("""
          const s = N.scaleFrom({x:0,y:0,width:1920,height:1080}, 1920, 1080);
          out.a = N.mapRect({left:0, top:0, width:0, height:0}, s);
          out.b = N.mapRect({left:0, top:0, width:4, height:4}, s);
          out.c = N.mapRect({left:0, top:0, width:400, height:300}, s);
        """)
        self.assertIsNone(out["a"])
        self.assertIsNone(out["b"])
        self.assertIsNotNone(out["c"])

    # ---- stashing ----------------------------------------------------------------------------
    def test_selecting_an_html_window_does_not_make_firefox_disappear(self):
        """Focus is not minimise. Background native apps remain mapped like a real desktop."""
        out = self.js("""
          out.p = N.stashPlan(
            [{native: 7, z: 1, rect:{left:100, top:100, width:800, height:600}}],
            [{z: 2, rect:{left:400, top:300, width:500, height:400}}]);
        """)
        self.assertEqual(out["p"]["stash"], [])
        self.assertEqual(out["p"]["show"], [7])

    def test_a_window_behind_it_does_not(self):
        """Stacking is respected in both directions, or every native window would be stashed by
        anything that happens to share pixels with it."""
        out = self.js("""
          out.p = N.stashPlan(
            [{native: 7, z: 5, rect:{left:100, top:100, width:800, height:600}}],
            [{z: 2, rect:{left:400, top:300, width:500, height:400}}]);
        """)
        self.assertEqual(out["p"]["show"], [7])

    def test_a_window_that_does_not_touch_it_does_not(self):
        out = self.js("""
          out.p = N.stashPlan(
            [{native: 7, z: 1, rect:{left:0, top:0, width:300, height:200}}],
            [{z: 9, rect:{left:900, top:600, width:400, height:300}}]);
        """)
        self.assertEqual(out["p"]["show"], [7])

    def test_a_minimised_app_is_stashed(self):
        """Hiding only our frame would leave the app itself sitting on the desktop with no title
        bar — a browser nobody can move, close or identify."""
        out = self.js("""
          out.p = N.stashPlan([{native: 7, z: 1, minimised: true,
                                rect:{left:0, top:0, width:300, height:200}}], []);
        """)
        self.assertEqual(out["p"]["stash"], [7])

    def test_a_minimised_html_window_stashes_nothing(self):
        """It is not on screen, so it needs no pixels."""
        out = self.js("""
          out.p = N.stashPlan(
            [{native: 7, z: 1, rect:{left:100, top:100, width:800, height:600}}],
            [{z: 9, minimised: true, rect:{left:100, top:100, width:800, height:600}}]);
        """)
        self.assertEqual(out["p"]["show"], [7])

    def test_touching_edges_are_not_an_overlap(self):
        """Two windows sharing an edge share no pixel. Treating that as a collision stashes an app
        every time another window is docked beside it."""
        out = self.js("""
          out.t = N.overlaps({left:0, top:0, width:100, height:100},
                             {left:100, top:0, width:100, height:100});
        """)
        self.assertFalse(out["t"])

    # ---- traffic -----------------------------------------------------------------------------
    def test_an_unchanged_rectangle_is_not_re_sent(self):
        """Every placement is a round trip and a reconfigure the app must handle. Re-sending the
        same rectangle sixty times a second relayouts a browser continuously while somebody drags
        a window that is not even theirs."""
        out = self.js("""
          const a = {x:1, y:2, w:3, h:4};
          out.same = N.changed(a, {x:1, y:2, w:3, h:4});
          out.moved = N.changed(a, {x:2, y:2, w:3, h:4});
          out.first = N.changed(null, a);
          out.none = N.changed(a, null);
        """)
        self.assertFalse(out["same"])
        self.assertTrue(out["moved"])
        self.assertTrue(out["first"])
        self.assertFalse(out["none"])


if __name__ == "__main__":
    unittest.main()
