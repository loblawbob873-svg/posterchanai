"""A real compositor, with two outputs, on a machine with no screen.

Every PosterChanOS window-manager bug reported so far has been invisible to this repo's checks:
windows resetting when resized, moved between monitors, activated or maximised; a native app drawn
separately from its PosterChan frame; a terminal that glitches crossing a seam. The checks covering
that area pass, because they drive a STUB compositor with ONE screen, and the failures live in the
half that talks to a real one — two outputs, different sizes, real focus.

wlroots needs no hardware for that: WLR_BACKENDS=headless gives sway virtual outputs and its
ordinary IPC works against them. This is the floor that keeps that capability working, so a check
built on it can say SKIP with a reason on a machine without sway rather than fail for the wrong one.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "scripts"))
import swayheadless  # noqa: E402


class AHeadlessCompositorIsAvailable(unittest.TestCase):
    def test_it_reports_why_when_this_machine_cannot_run_one(self):
        """`available()` must answer with a REASON. A check that skips saying nothing is a check
        nobody notices has stopped running."""
        ok, why = swayheadless.available()
        if ok:
            self.assertEqual(why, "")
        else:
            self.assertTrue(why.strip(), "it refused without saying why")

    def test_two_outputs_come_up_and_are_the_sizes_asked_for(self):
        ok, why = swayheadless.available()
        if not ok:
            self.skipTest(why)
        with swayheadless.headless_sway(outputs=[(1920, 1080), (1600, 900)]) as sway:
            outs = {o["name"]: o["rect"] for o in sway.outputs()}
            self.assertEqual(len(outs), 2, outs)
            self.assertEqual((outs["HEADLESS-1"]["width"], outs["HEADLESS-1"]["height"]),
                             (1920, 1080), outs)
            self.assertEqual((outs["HEADLESS-2"]["width"], outs["HEADLESS-2"]["height"]),
                             (1600, 900), outs)
            # Side by side, so a window can be dragged across a real seam.
            self.assertEqual(outs["HEADLESS-2"]["x"], 1920, outs)

    def test_it_cleans_up_after_itself(self):
        """A compositor left running holds a socket and a workspace; the next run would attach to
        the wrong one and report somebody else's screens."""
        ok, why = swayheadless.available()
        if not ok:
            self.skipTest(why)
        with swayheadless.headless_sway(outputs=[(1024, 768)]) as sway:
            sock = sway.sock
            self.assertTrue(os.path.exists(sock), sock)
        self.assertFalse(os.path.exists(sock), "the IPC socket outlived the compositor")
