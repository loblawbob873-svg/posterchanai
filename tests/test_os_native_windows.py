"""A Linux app inside a PosterChan window — the arithmetic, run.

Firefox on PosterChanOS must not look like firefox on top of PosterChanOS. It gets one of our
windows, and the real Wayland surface is held over that window's body. Every failure here puts the
app somewhere wrong rather than throwing, which is why this is measured rather than eyeballed.
"""
import json
import re
import os
import shutil
import subprocess
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MOD = os.path.join(ROOT, "static", "js", "client", "osnative.js")
NODE = shutil.which("node") or shutil.which("nodejs")


def test_maximised_workspace_can_be_restored_and_resized_from_its_grip():
    """A maximised Concord window must not turn its visible resize grip into a dead control."""
    with open(os.path.join(ROOT, "static", "js", "client", "os.js"), encoding="utf-8") as handle:
        src = handle.read()
    resize = src[src.index("function startResize"):src.index("// ---- desktop, taskbar")]
    assert "if(w.max) toggleMax(w)" in resize
    assert "if(w.max) return" not in resize


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

    def test_terminal_drag_overlay_parks_only_the_intersecting_native_surface(self):
        out = self.js("""
          const items=[
            {native:7,z:20,minimised:false,rect:{left:100,top:100,width:700,height:500}},
            {native:8,z:21,minimised:false,rect:{left:1000,top:100,width:700,height:500}}];
          const terminal=[{z:Number.MAX_SAFE_INTEGER,minimised:false,
            rect:{left:200,top:150,width:600,height:450}}];
          out.p=N.stashPlan(items,terminal);
        """)
        self.assertEqual(out["p"]["stash"], [7])
        self.assertEqual(out["p"]["show"], [8])
        self.assertEqual(out["p"]["show"], [8])

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

    def test_frames_are_clamped_wholly_inside_the_usable_output(self):
        out = self.js("""
          out.right = N.clampLocalRect({x:1800,y:900,w:900,h:700},
            {width:1920,height:1008},{width:420,height:260,gap:12});
          out.left = N.clampLocalRect({x:-700,y:-90,w:800,h:600},
            {width:1920,height:1008},{width:420,height:260,gap:12});
          out.huge = N.clampLocalRect({x:400,y:300,w:4000,h:3000},
            {width:1024,height:528},{width:420,height:260,gap:12});
        """)
        self.assertEqual(out["right"], {"x":1008,"y":296,"w":900,"h":700})
        self.assertEqual(out["left"], {"x":12,"y":12,"w":800,"h":600})
        self.assertEqual(out["huge"], {"x":12,"y":12,"w":1000,"h":504})

    def test_removed_monitor_geometry_clamps_saved_unsnap_position(self):
        out = self.js("""
          out.r = N.clampLocalRect({x:2100,y:40,w:1100,h:800},
            {width:1920,height:1008},{width:420,height:260,gap:12});
        """)
        self.assertEqual(out["r"], {"x":808,"y":40,"w":1100,"h":800})

    def test_every_final_window_path_uses_the_same_usable_output_clamp(self):
        src = open(os.path.join(ROOT, "static", "js", "client", "os.js"), encoding="utf-8").read()
        drag = src[src.index("function startDrag"):src.index("function startResize")]
        resize = src[src.index("function startResize"):src.index("// ---- desktop, taskbar")]
        unsnap = src[src.index("function unsnap"):src.index("function toggleMax")]
        changed = src[src.index("function onResize"):src.index("function onKey")]
        handoff = src[src.index("if(pcWM.onHandoffFrame"):src.index("if(pcWM.onPreviewFrame")]
        for name, block in (("drag", drag), ("resize", resize), ("unsnap", unsnap),
                            ("display change", changed), ("handoff", handoff)):
            self.assertIn("keepFrameReachable(w)", block, name + " can leave a window off-screen")

    # ---- stashing ----------------------------------------------------------------------------
    def test_a_window_in_front_of_it_puts_it_away(self):
        """CLICKING A WINDOW PUTS IT IN FRONT — which for a native app means its surface leaves the
        screen, because there is no other lever.

        A native app is a FLOATING sway window and this desktop is the one TILED window; sway paints
        floating above tiled, always. So a PosterChan window can never be drawn in front of Telegram,
        and the only way the window you clicked can be usable is for the app covering it to go.

        This assertion was briefly inverted, to stop apps "disappearing". They were not disappearing
        because of this rule — they were disappearing because `.osw.native-stashed` was
        `visibility:hidden`, which took the FRAME off the screen too, so an app that had gone behind
        left no window at all. Inverting this instead left Telegram on top of everything, for ever.
        Both halves belong to one fix; see stashPlan() and the stylesheet rule it names."""
        out = self.js("""
          out.p = N.stashPlan(
            [{native: 7, z: 1, rect:{left:100, top:100, width:800, height:600}}],
            [{z: 2, rect:{left:400, top:300, width:500, height:400}}]);
        """)
        self.assertEqual(out["p"]["stash"], [7])

    def test_html_drag_crossing_a_native_surface_updates_the_stash_plan(self):
        """A Terminal that starts clear of Firefox must park it when their paths cross."""
        out = self.js("""
          const native = [{native:7,z:1,rect:{left:500,top:200,width:700,height:500}}];
          out.before = N.stashPlan(native,
            [{z:Number.MAX_SAFE_INTEGER,rect:{left:20,top:20,width:300,height:180}}]);
          out.crossed = N.stashPlan(native,
            [{z:Number.MAX_SAFE_INTEGER,rect:{left:650,top:260,width:300,height:180}}]);
        """)
        self.assertEqual(out["before"]["stash"], [])
        self.assertEqual(out["crossed"]["stash"], [7])

        src = open(os.path.join(ROOT, "static", "js", "client", "os.js"), encoding="utf-8").read()
        drag = src[src.index("function startDrag"):src.index("function startResize")]
        resize = src[src.index("function startResize"):src.index("// ---- desktop, taskbar")]
        self.assertIn("if(nativeWins().length) _natGesture(w, true)", drag)
        self.assertIn("if(nativeWins().length) _natGesture(w, true)", resize)

    def test_the_frame_of_a_stashed_app_stays_on_the_desktop(self):
        """The other half, and without it the rule above IS the "Telegram disappeared" bug.

        A background window in any desktop is still there: occluded, clickable, and one click from
        the front. Read from the real stylesheet, because this is exactly the line somebody deletes
        while fixing a black rectangle."""
        css = open(os.path.join(ROOT, "static", "css", "client.css"), encoding="utf-8").read()
        # EVERY RULE WHOSE SELECTOR NAMES IT, and nothing else. A fixed slice from the first match
        # ran straight into `.osw.native-fullscreen-frame{visibility:hidden}` — a different rule
        # that hides for a good reason — and reported this one as broken.
        # COMMENTS STRIPPED FIRST. A rule's "selector" as matched below is everything since the
        # previous `}`, comments included — so this test read the word `visibility:hidden` out of
        # the paragraph explaining why it must not be there, and failed against a correct file.
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        rules = re.findall(r"([^{}]*)\{([^{}]*)\}", css)
        block = "".join(f"{sel}{{{body}}}" for sel, body in rules
                        if "native-stashed" in sel)
        self.assertTrue(block, "no .osw.native-stashed rule at all — re-point this test")
        self.assertNotIn("visibility:hidden", block,
                         "a native app that went behind another window vanished from the desktop "
                         "entirely — frame, title bar and all")
        self.assertIn("pointer-events:auto", block,
                      "the frame is what a person clicks to bring the app back")

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

    def test_native_apps_are_adopted_into_real_posterchan_frames(self):
        """A compositor-only task is exactly the regression: different chrome, floating forever,
        and no HTML drag path to show the snap ghost."""
        src = open(os.path.join(ROOT, "static", "js", "client", "os.js"), encoding="utf-8").read()
        block = src[src.index("function adoptNative(nw)"):src.index("async function adoptAll()")]
        self.assertIn("openApp(view", block)
        self.assertIn("w.native=id", block)
        self.assertIn("osw-native", block)
        self.assertNotIn("return null; } // compatibility", block)

    def test_adopted_apps_do_not_get_duplicate_taskbar_controls(self):
        src = open(os.path.join(ROOT, "static", "js", "client", "os.js"), encoding="utf-8").read()
        self.assertIn("nativeTasks=rows.filter", src)

    def test_hidden_terminal_surface_is_restored_atomically(self):
        src = open(os.path.join(ROOT, "static", "js", "client", "os.js"), encoding="utf-8").read()
        self.assertIn("pcWM.restore(it.native,rect.x,rect.y,rect.w,rect.h)", src)


if __name__ == "__main__":
    unittest.main()


@unittest.skipIf(not NODE, "needs node")
class TheCompositorIsTheAuthorityOnGeometryToo(unittest.TestCase):
    """"Firefox is launching and it does not even fit in the PosterChan window."

    MEASURED on the real desktop before this existed: a 1280x1624 HTML frame containing a 1394x867
    Firefox. Its POSITION matched the frame to the pixel — that path had run — and its size had
    never once been corrected. Forcing the surface to 600x400 in the wrong corner with `swaymsg`
    left it there, uncorrected, indefinitely.

    Two defects, and both had to be fixed: the desktop compared geometry against `_natSent`, its own
    record of what it last ASKED for (so anything the compositor did without us was invisible — a
    client restoring its session geometry, a sway keybinding), and nothing ever asked, because
    `nsync` only ran on events the desktop generates itself.

    The parked/hidden flag had already been given exactly this treatment, with a comment saying
    "sway is the authority". Geometry, one field over, had not.
    """
    def js(self, body):
        src = ("const N = require(%s);\nconst out = {};\n%s\n"
               "process.stdout.write(JSON.stringify(out));" % (json.dumps(MOD), body))
        r = subprocess.run([NODE, "-e", src], capture_output=True, text=True, timeout=30)
        self.assertEqual(r.returncode, 0, r.stderr[-800:])
        return json.loads(r.stdout)

    WANT = "{x:448,y:199,w:1280,h:1624}"

    def test_the_real_firefox_measurement_is_detected_as_drift(self):
        got = self.js("out.p = N.driftPlan(%s, {x:447,y:199,w:1394,h:867}, null, 2);" % self.WANT)
        self.assertTrue(got["p"]["replace"],
                        "the exact geometry measured on the real desktop is not reported as drift")

    def test_a_surface_sitting_where_we_put_it_is_left_alone(self):
        got = self.js("out.p = N.driftPlan(%s, {x:448,y:199,w:1280,h:1624}, null, 2);" % self.WANT)
        self.assertFalse(got["p"]["replace"])
        self.assertIsNone(got["p"]["memo"], "an agreeing window must not carry give-up state")

    def test_a_client_that_cannot_be_exact_is_tolerated_not_fought(self):
        """A terminal resizes in whole character cells, so it can never be exactly the rectangle it
        is asked for. An exact comparison would re-place it every few seconds for ever."""
        got = self.js("out.p = N.driftPlan(%s, {x:449,y:200,w:1279,h:1623}, null, 2);" % self.WANT)
        self.assertFalse(got["p"]["replace"])

    def test_the_desktop_stops_arguing_after_two_corrections_at_the_same_target(self):
        got = self.js(
            "let memo = null, tries = [];"
            "for (let i = 0; i < 5; i++) {"
            "  const p = N.driftPlan(%s, {x:448,y:199,w:900,h:1624}, memo, 2);"
            "  tries.push(p.replace); memo = p.memo; }"
            "out.tries = tries;" % self.WANT)
        self.assertEqual(got["tries"], [True, True, False, False, False],
                         "a client refusing the size is either never corrected or fought for ever")

    def test_a_new_target_gets_a_fresh_pair_of_attempts(self):
        """Giving up is about ONE rectangle. Moving the frame is a new question."""
        got = self.js(
            "let memo = null;"
            "for (let i = 0; i < 3; i++) memo = N.driftPlan(%s, {x:448,y:199,w:900,h:1624}, memo, 2).memo;"
            "out.p = N.driftPlan({x:20,y:20,w:800,h:600}, {x:448,y:199,w:900,h:1624}, memo, 2);"
            % self.WANT)
        self.assertTrue(got["p"]["replace"])

    def test_a_parked_window_and_an_unreadable_rect_are_never_drift(self):
        """"I could not read it" is not "it moved" — the recurring rule in this codebase."""
        got = self.js(
            "out.hidden = N.driftPlan('hidden', {x:0,y:0,w:10,h:10}, null, 2);"
            "out.norect = N.driftPlan(%s, null, null, 2);"
            "out.nowant = N.driftPlan(null, {x:1,y:2,w:3,h:4}, null, 2);" % self.WANT)
        self.assertFalse(got["hidden"]["replace"])
        self.assertFalse(got["norect"]["replace"])
        self.assertFalse(got["nowant"]["replace"])

    def test_the_reconcile_is_wired_into_the_sync_and_something_drives_it(self):
        with open(os.path.join(ROOT, "static", "js", "client", "os.js"), encoding="utf-8") as handle:
            src = handle.read()
        start = src.index("async function nsync(){")
        sync = src[start:start + 9000]
        self.assertIn("NAT().driftPlan(", sync, "the authority check is never consulted")
        self.assertIn("_natSent.delete(id)", sync, "drift is detected and then not acted on")
        # A pure check nothing ever runs is the shape this bug already had once.
        beat = src[src.index("function _natHeartbeatOn(){"):src.index("async function nsync(){")]
        self.assertIn("setInterval", beat)
        self.assertIn("nativeWins().length", beat, "it must cost nothing with no native app hosted")
        self.assertIn("w.gesturing", beat, "a heartbeat must not fight a drag in progress")
        self.assertIn("_natHeartbeatOn();", sync, "the heartbeat is defined but never started")
