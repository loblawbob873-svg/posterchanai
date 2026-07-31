"""Overlays must fit INSIDE the frame, on every image shape.

PIL's alpha_composite silently CLIPS an overlay that doesn't fit at the given offset — no error, no
warning. So an overlay sized against ONE axis, with the other left to follow the aspect ratio, is cut
off on any frame with the opposite orientation and nothing anywhere says so.

The stamps (gay / goon / hag) scaled to 66% of the WIDTH, then rotated with expand=True — which grows
the bounding box a further ~28% at 20 degrees — and composited centred. On a plain 1920x1080 photo
that put the top edge at y=-271: a quarter of the stamp gone. Portrait and square frames were fine,
which is exactly why it read as intermittent rather than broken.
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

SHAPES = [(1920, 1080, "landscape 16:9"), (1920, 600, "wide banner"),
          (1080, 1920, "portrait"), (1000, 1000, "square"), (600, 1920, "very tall")]


class StampFits(unittest.TestCase):
    def test_stamp_never_leaves_the_frame(self):
        from PIL import Image
        from app.services.effects_service.stamps import _stamp_centred
        for W, H, label in SHAPES:
            for sw, sh in ((1622, 1622), (2400, 400), (400, 2400)):
                img = Image.new("RGBA", (W, H))
                out = _stamp_centred(img, Image.new("RGBA", (sw, sh), (255, 0, 0, 255)))
                box = out.split()[-1].getbbox()
                self.assertIsNotNone(box, f"{label}: nothing drawn")
                self.assertGreaterEqual(box[0], 0, f"{label} {sw}x{sh}: clipped left")
                self.assertGreaterEqual(box[1], 0, f"{label} {sw}x{sh}: clipped top")
                self.assertLessEqual(box[2], W, f"{label} {sw}x{sh}: clipped right")
                self.assertLessEqual(box[3], H, f"{label} {sw}x{sh}: clipped bottom")

    def test_a_small_stamp_is_not_upscaled(self):
        """Only ever shrink — blowing a small stamp up to 66% would soften it for no reason."""
        from PIL import Image
        from app.services.effects_service.stamps import _stamp_centred
        img = Image.new("RGBA", (1920, 1080))
        out = _stamp_centred(img, Image.new("RGBA", (100, 80), (255, 0, 0, 255)))
        box = out.split()[-1].getbbox()
        self.assertEqual((box[2] - box[0], box[3] - box[1]), (100, 80))

    def test_no_site_scales_by_width_alone(self):
        """The regression: `target_w = int(W * 0.66)` with the height left to the aspect ratio."""
        with open(os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                               "app/services/effects_service/stamps.py"), encoding="utf-8") as f:
            src = f.read()
        self.assertNotIn("target_w = int(W * 0.66)", src,
                         "sizing by width alone is what cut the stamp off on landscape photos")
        self.assertEqual(src.count("_stamp_centred(img, stamp)"), 3,
                         "all three stamps (gay/goon/hag) must go through the fitting helper")


if __name__ == "__main__":
    unittest.main(verbosity=1)
