"""Meme Builder Distort: an image layer's corners moved in the editor are moved in the EXPORT.

Asked for 2026-10-09: "Meme builder also needs those standard transform/warp features to images". The
editor's Distort drags a picture's four corners inside its box (perspective, skew); the renderer must warp
the picture the same way, show the background where a corner was pulled away (never a smear of the
photo's edge -- ffmpeg's perspective stretches edge pixels outward unless the edge is transparent), and
leave an untouched layer exactly as it was.
"""
import io
import shutil
import unittest

from app.services import meme_builder_service

BG = (0, 0, 255)       # blue canvas
RED = (255, 0, 0)      # the picture


def _have(cmd):
    return shutil.which(cmd) is not None


def _render(tmp, warp):
    from PIL import Image
    src = tmp + "/red.png"
    Image.new("RGB", (400, 400), RED).save(src)
    layer = {"type": "image", "src": "u1", "x": 0, "y": 0, "w": 400, "h": 400, "start": 0, "dur": 1, "fit": "cover"}
    if warp is not None:
        layer["warp"] = warp
    edit = {"w": 400, "h": 400, "fps": 10, "duration": 1, "bg": "#0000ff", "fmt": "png", "layers": [layer]}
    data, ctype = meme_builder_service.render(edit, {"u1": src})
    return Image.open(io.BytesIO(data)).convert("RGB")


def _near(px, want, tol=40):
    return all(abs(a - b) <= tol for a, b in zip(px, want))


@unittest.skipUnless(_have("ffmpeg"), "needs ffmpeg")
class TestDistort(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="pctest-warp-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_a_corner_pulled_in_shows_the_background_and_the_middle_stays(self):
        # Top-left corner pulled to 40% across: a perspective/skew of the left edge.
        im = _render(self.tmp, [0.4, 0, 1, 0, 0, 1, 1, 1])
        self.assertTrue(_near(im.getpixel((20, 20)), BG), ("the pulled-away corner is not background", im.getpixel((20, 20))))
        self.assertTrue(_near(im.getpixel((300, 300)), RED), im.getpixel((300, 300)))
        self.assertTrue(_near(im.getpixel((200, 200)), RED), im.getpixel((200, 200)))

    def test_a_corner_pulled_diagonally_inward_keeps_the_photo(self):
        """The shape the editor's test drags: top-left pulled down AND across."""
        im = _render(self.tmp, [0.35, 0.3, 1, 0, 0, 1, 1, 1])
        self.assertTrue(_near(im.getpixel((300, 300)), RED), ("the warped photo is missing", im.getpixel((300, 300))))
        self.assertTrue(_near(im.getpixel((380, 20)), RED), ("the top-right corner moved", im.getpixel((380, 20))))
        self.assertTrue(_near(im.getpixel((40, 40)), BG), ("the pulled-away corner is not background", im.getpixel((40, 40))))

    def test_a_folded_shape_renders_unwarped_not_blank(self):
        """Top-left dragged onto the line between the other two corners is a triangle no perspective
        can draw. It must not cost the picture: it renders as if undistorted."""
        im = _render(self.tmp, [0.5, 0.5, 1, 0, 0, 1, 1, 1])
        self.assertTrue(_near(im.getpixel((20, 20)), RED) and _near(im.getpixel((300, 300)), RED),
                        (im.getpixel((20, 20)), im.getpixel((300, 300))))

    def test_an_untouched_layer_renders_as_before(self):
        im = _render(self.tmp, None)
        same = _render(self.tmp, [0, 0, 1, 0, 0, 1, 1, 1])
        self.assertTrue(_near(im.getpixel((20, 20)), RED))
        self.assertEqual(list(im.getdata())[:4000], list(same.getdata())[:4000])

    def test_corners_are_clamped_to_the_box_and_junk_is_ignored(self):
        self.assertIsNone(meme_builder_service._warp_corners([0, 0, 1, 0, 0, 1, 1, 1]))
        self.assertIsNone(meme_builder_service._warp_corners("x"))
        self.assertIsNone(meme_builder_service._warp_corners([1, 2, 3]))
        self.assertEqual(meme_builder_service._warp_corners([-5, 0, 9, 0, 0.2, 1, 1, 1])[:5], [0.0, 0.0, 1.0, 0.0, 0.2])


if __name__ == "__main__":
    unittest.main()
