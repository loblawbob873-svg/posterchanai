""""We just need images to load full screen without the shitty borders that waste space."

Three things were taking that space in the lightbox and none of them earned it:

1. `max-height:84vh` on the media, to "leave ~16vh at the bottom for the toolbar". But `.lb-bar` is
   `position:fixed` — it was never in the layout and always floated OVER the media, so the
   reservation bought nothing and letterboxed every image with ~170px of black on a 1080p screen.
   On a desktop it is worse: that bar is `opacity:0` until the pointer moves, so the space was held
   for something not even on screen.
2. A 1px neon border and a 40px glow drawn around the photo, with a 10px radius clipping its
   corners — chrome on top of somebody's content.
3. 16px of padding on the backdrop, cropping every edge for nothing.

Video keeps one reservation and only the real one: its native controls sit along its own bottom
edge and the toolbar would land on them.

The half that must not regress with it: making the media full-bleed removes the backdrop a tap
relies on, so the deliberate ways out have to still be there.
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CSS = (ROOT / "static" / "css" / "client.css").read_text(encoding="utf-8")
APP = (ROOT / "static" / "js" / "client" / "app.js").read_text(encoding="utf-8")


def rule(selector: str) -> str:
    """The declaration block whose selector list STARTS with this text.

    Anchored, because `.lightbox video` is also a substring of `.lightbox img,.lightbox video` — and
    matching that combined rule instead returned the shared declarations and made the video-specific
    assertion look like a missing rule. A helper that can silently answer about the wrong block is
    the sort of thing that makes a test agree with a bug.
    """
    for m in re.finditer(re.escape(selector) + r"\s*\{([^}]*)\}", CSS):
        before = CSS[:m.start()].rstrip()
        if not before or before[-1] in "};/*":
            return m.group(1)
    raise AssertionError("no rule whose selector list starts with " + selector)


class OpeningAPictureShowsThePicture(unittest.TestCase):
    def test_an_image_is_not_capped_below_the_viewport(self):
        block = rule(".lightbox img")
        self.assertNotIn("84vh", block)
        self.assertIn("100dvh", block, "dvh is what makes this right on a phone whose bar comes and goes")
        self.assertIn("100vh", block, "a fallback is needed for anything that does not know dvh")

    def test_the_decoration_around_the_photo_is_gone(self):
        block = rule(".lightbox img,.lightbox video")
        self.assertIn("border:0", block)
        self.assertIn("border-radius:0", block)
        self.assertIn("box-shadow:none", block)
        self.assertNotIn("var(--neon)", block)

    def test_the_backdrop_no_longer_crops_the_edges(self):
        self.assertIn("padding:0", rule(".lightbox"))

    def test_video_reserves_only_the_toolbar_and_nothing_like_16vh(self):
        """Its own controls are along its bottom edge; the app toolbar would land on them. That is a
        real conflict and worth ~76px — it was never worth 16vh."""
        block = rule(".lightbox video")
        m = re.search(r"calc\(100dvh - (\d+)px\)", block)
        self.assertIsNotNone(m, "video no longer reserves the toolbar strip: " + block)
        self.assertLessEqual(int(m.group(1)), 100,
                             "the reserve grew back into letterboxing")

    def test_the_reservation_it_replaced_was_for_something_not_in_the_layout(self):
        """If `.lb-bar` ever stops being fixed it WOULD need room, and this stops that landing
        silently — the rule above would be wrong and nothing else would say so."""
        self.assertIn("position:fixed", rule(".lb-bar"))


class AndYouCanStillGetOut(unittest.TestCase):
    """A full-bleed image can leave no backdrop to tap, and a tap on the image deliberately stops
    propagation. So the other ways out are load-bearing now, not conveniences."""

    def _viewer(self) -> str:
        i = APP.index("bg.className='lightbox'")
        return APP[i:i + 6000]

    def test_escape_closes(self):
        self.assertIn("if(e.key==='Escape')", self._viewer())

    def test_there_is_always_a_close_button(self):
        self.assertIn("lb-btn", self._viewer())

    def test_the_bar_is_never_hidden_on_a_touch_screen(self):
        """The hide-until-you-move behaviour is gated on a real pointer for exactly this reason."""
        i = CSS.index(".lb-bar{opacity:0}")
        before = CSS[:i]
        guard = before.rindex("@media")
        self.assertIn("hover:hover", CSS[guard:i])
        self.assertIn("pointer:fine", CSS[guard:i])

    def test_a_tap_on_the_media_still_does_not_fall_through_to_close(self):
        self.assertIn("don't let a tap on the image reach the backdrop-close", self._viewer())


if __name__ == "__main__":
    unittest.main()
