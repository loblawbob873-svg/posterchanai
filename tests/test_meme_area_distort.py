"""Meme Builder Distort, AN AREA: a rectangle picked out of a picture is moved onto four new corners in the EXPORT.

Asked 2026-10-27: "Meme Builder: distort does not let you transform an area in the photo like photo editors let
you". The editor's Distort -> "An area" picks a rectangle on the picture and drags that rectangle's corners
(`warpArea = {r: [x, y, w, h], q: [8 corners]}`, fractions of the layer box). The renderer must move exactly that
part of the picture, leave everything else where it was (the original stays under the moved area), keep a cut-out's
transparency, and render an untouched or broken one as before.
"""
import io
import shutil
import unittest

from app.services import meme_builder_service as M

BG = (0, 0, 255)        # blue canvas
GREEN = (0, 160, 0)     # the picture
RED = (255, 0, 0)       # the area: a red square at 40-60% of the picture


def _have(cmd):
    return shutil.which(cmd) is not None


def _render(tmp, area=None, flip=False, cutout=False):
    from PIL import Image, ImageDraw
    src = tmp + "/pic.png"
    im = Image.new("RGBA", (400, 400), (0, 0, 0, 0) if cutout else GREEN + (255,))
    d = ImageDraw.Draw(im)
    if cutout:
        d.rectangle([40, 40, 359, 359], fill=GREEN + (255,))
    d.rectangle([160, 160, 239, 239], fill=RED + (255,))
    im.save(src)
    layer = {"type": "image", "src": "u1", "x": 0, "y": 0, "w": 400, "h": 400, "start": 0, "dur": 1, "fit": "cover"}
    if area is not None:
        layer["warpArea"] = area
    if flip:
        layer["flipH"] = True
    edit = {"w": 400, "h": 400, "fps": 10, "duration": 1, "bg": "#0000ff", "fmt": "png", "layers": [layer]}
    data, _ctype = M.render(edit, {"u1": src})
    return Image.open(io.BytesIO(data)).convert("RGB")


def _near(px, want, tol=40):
    return all(abs(a - b) <= tol for a, b in zip(px, want))


# The red square (40%-60%) enlarged to 20%-80%: "make the face bigger".
SQUARE = [0.4, 0.4, 0.2, 0.2]
BIGGER = [0.2, 0.2, 0.8, 0.2, 0.2, 0.8, 0.8, 0.8]


@unittest.skipUnless(_have("ffmpeg"), "needs ffmpeg")
class TestAreaDistort(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.mkdtemp(prefix="pctest-area-")
        self.addCleanup(shutil.rmtree, self.tmp, True)

    def test_the_picked_area_moves_and_the_rest_of_the_picture_stays(self):
        im = _render(self.tmp, {"r": SQUARE, "q": BIGGER})
        # Where the enlarged square now reaches -- green before.
        for p in [(100, 100), (300, 100), (100, 300), (300, 300), (200, 200)]:
            self.assertTrue(_near(im.getpixel(p), RED), (p, "the area did not move onto its new corners", im.getpixel(p)))
        # Outside the new corners: the picture, untouched.
        for p in [(40, 40), (360, 40), (40, 360), (360, 360), (200, 50)]:
            self.assertTrue(_near(im.getpixel(p), GREEN), (p, "the rest of the picture changed", im.getpixel(p)))

    def test_a_shrunk_area_leaves_the_original_underneath(self):
        """Corners pulled IN: the moved copy is smaller and the picture under it is unchanged (the original
        square shows around the moved one) -- it never punches a hole through to the background."""
        im = _render(self.tmp, {"r": SQUARE, "q": [0.45, 0.45, 0.55, 0.45, 0.45, 0.55, 0.55, 0.55]})
        self.assertTrue(_near(im.getpixel((200, 200)), RED), im.getpixel((200, 200)))
        self.assertTrue(_near(im.getpixel((165, 165)), RED), ("the original square was cut away", im.getpixel((165, 165))))
        self.assertFalse(_near(im.getpixel((165, 165)), BG), "a hole through to the background")

    def test_a_cut_out_stays_see_through_around_the_moved_area(self):
        im = _render(self.tmp, {"r": SQUARE, "q": BIGGER}, cutout=True)
        self.assertTrue(_near(im.getpixel((10, 10)), BG), ("the cut-out's transparent edge filled in", im.getpixel((10, 10))))
        self.assertTrue(_near(im.getpixel((100, 100)), RED), im.getpixel((100, 100)))

    def test_the_area_belongs_to_the_picture_when_it_is_flipped(self):
        """The area is part of the photo (applied before the flip): an area on the LEFT of the picture is on
        the right of a mirrored layer."""
        # The red square moved to the picture's LEFT edge.
        to_left = {"r": SQUARE, "q": [0.0, 0.4, 0.2, 0.4, 0.0, 0.6, 0.2, 0.6]}
        im = _render(self.tmp, to_left, flip=True)
        self.assertTrue(_near(im.getpixel((380, 200)), RED), ("the area was not mirrored with its picture", im.getpixel((380, 200))))
        self.assertTrue(_near(im.getpixel((20, 200)), GREEN), im.getpixel((20, 200)))

    def test_untouched_folded_and_junk_areas_render_as_before(self):
        plain = _render(self.tmp)
        same = _render(self.tmp, {"r": SQUARE, "q": [0.4, 0.4, 0.6, 0.4, 0.4, 0.6, 0.6, 0.6]})
        self.assertEqual(list(plain.getdata())[:6000], list(same.getdata())[:6000])
        # A bow-tie (two corners crossed) cannot be drawn: the picture renders unmoved, never blank.
        folded = _render(self.tmp, {"r": SQUARE, "q": [0.6, 0.4, 0.4, 0.4, 0.4, 0.6, 0.6, 0.6]})
        self.assertTrue(_near(folded.getpixel((200, 200)), RED) and _near(folded.getpixel((100, 100)), GREEN))
        for junk in (None, "x", {"r": [0, 0, 1]}, {"r": SQUARE, "q": [1, 2]}, {"r": [0.5, 0.5, 0.01, 0.2], "q": BIGGER},
                     {"r": ["a", 0, 1, 1], "q": BIGGER}):
            self.assertIsNone(M._warp_area(junk), junk)

    def test_a_small_area_like_a_face_is_not_refused_as_folded(self):
        """The whole-picture fold margin (2% of the box) would refuse a 10% area before it moved."""
        area = M._warp_area({"r": [0.45, 0.45, 0.1, 0.1], "q": [0.43, 0.43, 0.57, 0.43, 0.43, 0.57, 0.57, 0.57]})
        self.assertIsNotNone(area)
        self.assertFalse(M._warp_folded(area[1], 400, 400, M.AREA_FOLD))
        self.assertTrue(M._warp_folded(area[1], 400, 400), "the test does not exercise the small-area margin")


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(_have("ffmpeg"), "needs ffmpeg")
class TestSoftAreaResizesTheNoseNotTheCheek(unittest.TestCase):
    """'I was trying a simple test of resizing a nose and it looked like it was more of the outer area than the nose area'
    (2026-10-30). A hard rectangle scaled up carries the cheek round the nose with it and shows a seam; a new area has a
    soft oval edge (`f`), so the middle grows and the corners stay. Measured on a 1.4x nose: hard changed 8.3% of the skin
    round the grown nose, soft 0%."""

    def _nose(self, f):
        import numpy as np, tempfile
        from PIL import Image, ImageDraw
        W = H = 400
        yy, xx = np.mgrid[0:H, 0:W]
        skin = np.stack([200 + 30 * np.sin(xx / 23.0), 150 + 20 * np.cos(yy / 17.0), 120 + 15 * np.sin((xx + yy) / 31.0)], -1)
        im = Image.fromarray(skin.clip(0, 255).astype(np.uint8))
        ImageDraw.Draw(im).ellipse([170, 160, 230, 250], fill=(150, 90, 70))
        tmp = tempfile.mkdtemp(prefix="pctest-nose-"); self.addCleanup(shutil.rmtree, tmp, True)
        im.save(tmp + "/pic.png")
        r, cx, cy, s = [0.375, 0.35, 0.25, 0.325], 0.5, 0.5125, 1.4
        q = []
        for x, y in [(r[0], r[1]), (r[0] + r[2], r[1]), (r[0], r[1] + r[3]), (r[0] + r[2], r[1] + r[3])]:
            q += [cx + (x - cx) * s, cy + (y - cy) * s]
        area = {"r": r, "q": q}
        if f:
            area["f"] = f
        layer = {"type": "image", "src": "u1", "x": 0, "y": 0, "w": W, "h": H, "start": 0, "dur": 1, "fit": "cover", "warpArea": area}
        data, _ = M.render({"w": W, "h": H, "fps": 10, "duration": 1, "bg": "#000000", "fmt": "png", "layers": [layer]}, {"u1": tmp + "/pic.png"})
        return np.asarray(Image.open(io.BytesIO(data)).convert("RGB")).astype(int), np.asarray(im).astype(int)

    @staticmethod
    def _cheek_changed(out, src):
        """Share of the moved rectangle, outside the grown nose, whose skin changed visibly (>25 levels)."""
        import numpy as np
        yy, xx = np.mgrid[0:400, 0:400]
        box = (xx >= 115) & (xx < 285) & (yy >= 114) & (yy < 297)
        nose = ((xx - 200) / (30 * 1.4 * 1.15)) ** 2 + ((yy - 205) / (45 * 1.4 * 1.15)) ** 2 <= 1
        diff = np.abs(out - src).max(-1)
        return float((diff[box & ~nose] > 25).mean())

    def test_a_soft_area_grows_the_nose_and_leaves_the_cheek(self):
        out, src = self._nose(0.35)
        dark = lambda a: ((a[..., 0] < 175) & (a[..., 1] < 115)).sum()
        self.assertGreater(dark(out), dark(src) * 1.7, "the nose did not grow")
        self.assertLess(self._cheek_changed(out, src), 0.005, "the skin round the nose moved with it")

    def test_an_area_made_before_the_soft_edge_still_renders_hard(self):
        out, src = self._nose(0)
        self.assertGreater(self._cheek_changed(out, src), 0.03, "an old (hard) area no longer renders as it did")
