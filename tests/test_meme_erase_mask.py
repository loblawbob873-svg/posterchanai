"""Meme Builder: erase/hide part of a layer (the ✂ tool).

Run: venv-unified/bin/python -m unittest tests.test_meme_erase_mask

A layer can carry a `mask`: a PNG the size of that layer's SOURCE whose ALPHA is the part to KEEP —
opaque where the picture stays, transparent where it was rubbed out. The editor paints it in source
space (so it survives resizing/re-fitting the layer afterwards) and uploads it like any other layer
media, so it arrives here as a URL resolved through `sources`.

These render for real and sample PIXELS, because every way this can be wrong produces a file that is
perfectly valid and merely shows the wrong thing:

  * mask ignored          -> nothing is erased, and the graph still renders
  * alpha REPLACED rather than multiplied -> the erase works, but the layer's own transparency is
    destroyed: a cut-out PNG's removed background comes back as black, and so do the transparent
    letterbox bars `pad` adds under "contain". This is the failure `alphamerge` alone gives you, and
    it is invisible in a filter-string assertion.
  * mask seated by different geometry than the layer -> the erase lands offset or scaled, which reads
    as a brush bug rather than a geometry one.
  * input-index bookkeeping -> a masked layer adds TWO ffmpeg inputs. Advance by one and every LATER
    layer points at the wrong input, so an unrelated layer renders somebody else's footage.
"""
import unittest

from PIL import Image

from app.services import meme_builder_service as mb

import io
import os
import tempfile

CANVAS = 200
BG = "#0000ff"          # blue — what shows through anywhere the composite is transparent
RED, GREEN, BLUE = "red", "green", "blue"


def _dom(px):
    """Which primary a rendered pixel is. Compared by DOMINANT CHANNEL rather than by equality: the
    composite goes through ffmpeg's rgba->rgb24 conversion, which returns 253 for a 255 input. An
    exact-match assertion fails on that and says nothing about whether the erase worked."""
    r, g, b = px[0], px[1], px[2]
    if r > 200 and g < 60 and b < 60:
        return RED
    if g > 200 and r < 60 and b < 60:
        return GREEN
    if b > 200 and r < 60 and g < 60:
        return BLUE
    return f"other{px}"


def _write(img, path):
    img.save(path)
    return path


def _solid(path, rgba, size=(CANVAS, CANVAS)):
    return _write(Image.new("RGBA", size, rgba), path)


def _half_mask(path, size=(CANVAS, CANVAS)):
    """Keep the LEFT half (opaque), erase the right (transparent)."""
    m = Image.new("RGBA", size, (255, 255, 255, 255))
    for x in range(size[0] // 2, size[0]):
        for y in range(size[1]):
            m.putpixel((x, y), (255, 255, 255, 0))
    return _write(m, path)


def _render_still(edit, sources):
    out, ctype = mb.render(edit, sources)
    assert ctype == "image/png", ctype
    return Image.open(io.BytesIO(out)).convert("RGB")


def _project(layers, **kw):
    e = {"w": CANVAS, "h": CANVAS, "fps": 10, "bg": BG, "duration": 1.0,
         "fmt": "png", "still": 0.0, "layers": layers}
    e.update(kw)
    return e


def _layer(src, **kw):
    l = {"type": "image", "src": src, "x": 0, "y": 0, "w": CANVAS, "h": CANVAS,
         "start": 0, "dur": 1.0, "opacity": 1.0, "fit": "cover"}
    l.update(kw)
    return l


@unittest.skipUnless(mb.media_service.resolve_ffmpeg(), "ffmpeg not available")
class EraseMask(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="pcmasktest-")

    def tearDown(self):
        import shutil
        shutil.rmtree(self.tmp, ignore_errors=True)

    def p(self, name):
        return os.path.join(self.tmp, name)

    def test_without_a_mask_the_whole_layer_is_drawn(self):
        """The control. If this ever fails the others prove nothing."""
        red = _solid(self.p("red.png"), (255, 0, 0, 255))
        img = _render_still(_project([_layer("u://red")]), {"u://red": red})
        self.assertEqual(_dom(img.getpixel((50, 100))), RED)
        self.assertEqual(_dom(img.getpixel((150, 100))), RED)

    def test_the_erased_part_is_gone_and_the_kept_part_is_not(self):
        red = _solid(self.p("red.png"), (255, 0, 0, 255))
        mask = _half_mask(self.p("mask.png"))
        img = _render_still(_project([_layer("u://red", mask="u://mask")]),
                            {"u://red": red, "u://mask": mask})
        self.assertEqual(_dom(img.getpixel((50, 100))), RED, "the kept half should be untouched")
        self.assertEqual(_dom(img.getpixel((150, 100))), BLUE,
                         "the erased half should show the background through it")

    def test_erasing_does_not_make_the_layers_own_transparency_opaque(self):
        """The alphamerge trap: REPLACING the alpha with the mask un-erases everything the source had
        already made transparent. Source is a cut-out — transparent top, red bottom — under a mask that
        keeps everything, so the top must still show the background rather than a black rectangle."""
        cut = Image.new("RGBA", (CANVAS, CANVAS), (255, 0, 0, 255))
        for x in range(CANVAS):
            for y in range(CANVAS // 2):
                cut.putpixel((x, y), (0, 0, 0, 0))
        src = _write(cut, self.p("cut.png"))
        keep = _solid(self.p("keep.png"), (255, 255, 255, 255))
        img = _render_still(_project([_layer("u://cut", mask="u://keep")]),
                            {"u://cut": src, "u://keep": keep})
        self.assertEqual(_dom(img.getpixel((100, 50))), BLUE,
                         "the source's own transparency must survive being masked")
        self.assertEqual(_dom(img.getpixel((100, 150))), RED)

    def test_a_later_layer_still_gets_its_own_source(self):
        """A masked layer adds TWO inputs. If `idx` advances by one, the NEXT layer reads the mask (or
        the wrong clip) as its source — the bug is in a different layer than the one being edited."""
        red = _solid(self.p("red.png"), (255, 0, 0, 255))
        green = _solid(self.p("green.png"), (0, 255, 0, 255))
        keep = _solid(self.p("keep.png"), (255, 255, 255, 255))
        edit = _project([
            _layer("u://red", mask="u://keep", x=0, y=0, w=CANVAS // 2, h=CANVAS),
            _layer("u://green", x=CANVAS // 2, y=0, w=CANVAS // 2, h=CANVAS),
        ])
        img = _render_still(edit, {"u://red": red, "u://green": green, "u://keep": keep})
        self.assertEqual(_dom(img.getpixel((50, 100))), RED)
        self.assertEqual(_dom(img.getpixel((150, 100))), GREEN,
                         "the unmasked layer after a masked one must render its OWN source")

    def test_the_mask_is_seated_by_the_same_geometry_as_the_layer(self):
        """The mask is painted in SOURCE space, so it only lines up if it is fitted into the layer box
        by the same scale/pad. Here the source is WIDE and the box is square under "contain", so the
        layer is letterboxed — and the mask has to be letterboxed identically or the erase is offset."""
        wide = _solid(self.p("wide.png"), (255, 0, 0, 255), size=(400, 100))
        mask = _half_mask(self.p("wmask.png"), size=(400, 100))
        img = _render_still(
            _project([_layer("u://wide", mask="u://wmask", fit="contain")]),
            {"u://wide": wide, "u://wmask": mask})
        # Letterboxed: a 400x100 source in a 200x200 box occupies y=75..125, centred.
        self.assertEqual(_dom(img.getpixel((50, 100))), RED, "kept half of the letterboxed strip")
        self.assertEqual(_dom(img.getpixel((150, 100))), BLUE, "erased half of the letterboxed strip")
        self.assertEqual(_dom(img.getpixel((100, 20))), BLUE, "above the strip is background, not picture")

    def test_the_mask_survives_a_flip(self):
        """The mask is applied BEFORE flip/rotate, so it means 'this part of the picture'. Flipping the
        layer must carry the erased region with it, not erase a fixed corner of the frame."""
        red = _solid(self.p("red.png"), (255, 0, 0, 255))
        mask = _half_mask(self.p("mask.png"))
        img = _render_still(
            _project([_layer("u://red", mask="u://mask", flipH=True)]),
            {"u://red": red, "u://mask": mask})
        self.assertEqual(_dom(img.getpixel((150, 100))), RED, "the kept half moved with the flip")
        self.assertEqual(_dom(img.getpixel((50, 100))), BLUE, "the erased half moved with the flip")


if __name__ == "__main__":
    unittest.main()
