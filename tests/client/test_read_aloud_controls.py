"""Read-aloud can be stopped, and can be switched off.

Both halves of a filed report: "I should be able to stop a post from being read aloud while it's
playing (maybe by scrolling away?). I should also be able to turn this long press to read aloud
off." Neither was possible — once a post started narrating the only control was long-pressing a
DIFFERENT post, which starts another one, and the gesture itself had no switch at all.

Source-read: the audio path needs a real `Audio` element and a TTS endpoint, and the gesture needs
pointer events on a live timeline; what is asserted here is the wiring that was missing, including
the two ways a client setting silently fails in this codebase — no hydrate clause (saves, never
loads, reads as "it didn't stick"), and a stop that misses one of its callers.
"""
import os
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
APP = os.path.join(ROOT, "static", "js", "client", "app.js")
CSS = os.path.join(ROOT, "static", "css", "client.css")


class ReadAloudControls(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with open(APP, encoding="utf-8") as fh:
            cls.app = fh.read()
        with open(CSS, encoding="utf-8") as fh:
            cls.css = fh.read()

    def test_there_is_one_stop_and_everything_goes_through_it(self):
        self.assertIn("function stopNarration(", self.app)
        seg = self.app[self.app.index("function stopNarration("):][:900]
        self.assertIn(".pause()", seg)
        self.assertIn("_narrateWatch", seg, "the scroll observer outlives the audio it belongs to")
        self.assertIn("narrate-chip", seg, "the chip outlives the narration it offers to stop")

    def test_starting_a_second_one_stops_the_first_through_that_function(self):
        i = self.app.index("async function narratePost(")
        seg = self.app[i:i + 2600]
        self.assertIn("stopNarration();", seg,
                      "a second narration left the first one's chip and observer behind")

    def test_it_stops_when_it_ends_when_you_scroll_away_and_when_you_leave(self):
        i = self.app.index("async function narratePost(")
        seg = self.app[i:i + 2600]
        self.assertIn("onended", seg, "the chip stays after the audio finishes")
        watch = self.app[self.app.index("function _narrateChip("):][:1400]
        self.assertIn("IntersectionObserver", watch, "scrolling away does not stop it")
        self.assertIn("isIntersecting", watch)
        sv = self.app[self.app.index("function switchView(v, quiet)"):][:600]
        self.assertIn("stopNarration", sv, "leaving the screen leaves it reading")

    def test_the_chip_is_fixed_to_the_viewport(self):
        """It has to outlive the post scrolling away — that is the case it exists for — so it cannot
        be positioned inside the card."""
        seg = self.css[self.css.index(".narrate-chip{"):][:400]
        self.assertIn("position:fixed", seg)
        self.assertIn("z-index", seg)

    def test_the_gesture_can_be_turned_off(self):
        i = self.app.index("t=setTimeout(()=>{ t=null; held=true;")
        seg = self.app[max(0, i - 1400):i]
        self.assertIn("ClientSettings.get('readAloudHold', true)", seg,
                      "the long press cannot be switched off")
        self.assertIn("set-read-aloud", self.app, "there is no control for it in Settings")

    def test_the_setting_is_saved_AND_loaded_back(self):
        """The recurring shape: writing it in the toggle handler syncs it OUT for free, and the way
        back IN is a hand-written clause per key. Without one it turns itself back on everywhere
        else, which reads as the switch not sticking."""
        self.assertIn("saveClientPrefsNostr({ readAloudHold:", self.app)
        i = self.app.index("async function restoreClientPrefsNostr(")
        seg = self.app[i:i + 6000]
        self.assertIn("pr.readAloudHold", seg,
                      "saved but never hydrated — the toggle un-sets itself on every other device")

    def test_turning_it_off_stops_whatever_is_playing(self):
        i = self.app.index("const ra=$('#set-read-aloud')")
        seg = self.app[i:i + 600]
        self.assertIn("stopNarration()", seg,
                      "switching the feature off left the current post still reading")


if __name__ == "__main__":
    unittest.main()
