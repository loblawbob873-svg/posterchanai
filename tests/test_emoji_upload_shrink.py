"""Tests for the custom-emoji upload compressor (app/services/emoji_service._shrink_upload).

Run: venv-unified/bin/python -m unittest tests.test_emoji_upload_shrink

Uploads used to be stored VERBATIM — the only guard was an 8 MB reject — so a multi-megabyte PNG was
served at full size to every client rendering it inline at ~20px. These cover the ways compressing it
can go wrong, each of which is silent (the emoji still "works", it just looks or weighs wrong):

- transparency must survive. The house image compressor (media_service.compress_image) flattens alpha
  onto WHITE and emits a JPEG, which would put a white box behind every emoji on a dark theme — this
  is why emoji do NOT reuse it, and the test pins that;
- animation must survive. The same flattening would reduce an animated emoji to its first frame;
- the FORMAT must be preserved, because the stored extension is what the served Content-Type derives
  from — "upgrading" the encoding silently mismatches the two;
- an already-small emoji must pass through BYTE-IDENTICAL rather than being re-encoded for nothing;
- the result must never be BIGGER than the source. A small, already-optimised file can re-encode
  larger, and storing that is strictly worse than doing nothing;
- unreadable bytes must return the original, never raise. An upload is not worth a 500.

Fixtures are deliberately NOISY. A flat synthetic image compresses to under 1 KB, and at that size the
no-inflation guard correctly declines to re-encode — so a flat fixture silently tests the guard instead
of the path it claims to. Identical animation frames are likewise wrong: PIL collapses them into a
single-frame file, so an "animated" fixture built that way is not animated at all.

Pillow only — no database, no network.
"""
import io
import random
import unittest

from PIL import Image

from app.services.emoji_service import EMOJI_MAX_PX, _shrink_upload


def noisy(px, seed=0, alpha=True):
    """A noisy RGBA square with a transparent MARGIN, i.e. the shape of a real emoji: artwork in the
    middle, empty space around it.

    The margin is a solid region on purpose. Scattered single transparent pixels do not survive a 4x
    downscale — resampling averages each output pixel from ~16 inputs, so a lone hole comes back
    nearly opaque and an alpha assertion on it fails for reasons that have nothing to do with the
    code under test. Noise in the middle keeps the encoded size realistic."""
    rnd = random.Random(seed)
    im = Image.new("RGBA", (px, px), (0, 0, 0, 0))
    lo, hi = px // 4, px * 3 // 4
    for x in range(px):
        for y in range(px):
            inside = lo <= x < hi and lo <= y < hi
            a = 255 if (inside or not alpha) else 0
            im.putpixel((x, y), (rnd.randrange(256), rnd.randrange(256), rnd.randrange(256), a))
    return im


def encode(frames, fmt, **kw):
    buf = io.BytesIO()
    if len(frames) > 1:
        frames[0].save(buf, fmt, save_all=True, append_images=frames[1:], duration=100, loop=0, **kw)
    else:
        frames[0].save(buf, fmt, **kw)
    return buf.getvalue()


class ShrinkStills(unittest.TestCase):
    def test_big_transparent_png_shrinks_and_keeps_alpha(self):
        src = encode([noisy(512)], "PNG")
        out = _shrink_upload(src, ".png")
        im = Image.open(io.BytesIO(out))
        self.assertLess(len(out), len(src))
        self.assertLessEqual(max(im.size), EMOJI_MAX_PX)
        self.assertEqual(im.format, "PNG")
        # Transparency survived — NOT flattened onto white. Asserted over the whole alpha channel
        # rather than one pixel: downscaling RESAMPLES, so a lone transparent pixel averages with its
        # opaque neighbours and legitimately comes back nearly opaque. What must not happen is the
        # alpha channel going uniformly opaque, which is exactly what flattening does.
        alpha_min, _ = im.convert("RGBA").getchannel("A").getextrema()
        self.assertLess(alpha_min, 128, "alpha channel came back opaque — transparency was flattened")

    def test_jpeg_stays_jpeg(self):
        src = encode([noisy(600, alpha=False).convert("RGB")], "JPEG", quality=95)
        out = _shrink_upload(src, ".jpg")
        im = Image.open(io.BytesIO(out))
        self.assertLess(len(out), len(src))
        self.assertLessEqual(max(im.size), EMOJI_MAX_PX)
        self.assertEqual(im.format, "JPEG")


class ShrinkAnimated(unittest.TestCase):
    def test_animated_gif_keeps_every_frame(self):
        frames = [noisy(320, s) for s in range(3)]
        src = encode(frames, "GIF")
        self.assertEqual(getattr(Image.open(io.BytesIO(src)), "n_frames", 1), 3,
                         "fixture is not animated — vary the frames")
        out = _shrink_upload(src, ".gif")
        im = Image.open(io.BytesIO(out))
        self.assertLess(len(out), len(src))
        self.assertLessEqual(max(im.size), EMOJI_MAX_PX)
        self.assertEqual(im.format, "GIF")
        self.assertEqual(getattr(im, "n_frames", 1), 3)

    def test_variable_frame_delays_survive(self):
        """`im.info["duration"]` is the FIRST frame's delay only. Reusing it for every frame retimes
        a variable-speed emoji to a constant rate — visible, and silent."""
        frames = [noisy(320, s) for s in range(3)]
        buf = io.BytesIO()
        frames[0].save(buf, "GIF", save_all=True, append_images=frames[1:],
                       duration=[40, 300, 120], loop=0)
        src = buf.getvalue()

        def delays(blob):
            im = Image.open(io.BytesIO(blob))
            out = []
            for i in range(getattr(im, "n_frames", 1)):
                im.seek(i)
                out.append(im.info.get("duration"))
            return out

        self.assertEqual(delays(src), [40, 300, 120], "fixture lost its per-frame delays")
        self.assertEqual(delays(_shrink_upload(src, ".gif")), [40, 300, 120])

    def test_animated_webp_keeps_every_frame(self):
        frames = [noisy(320, s) for s in range(3)]
        src = encode(frames, "WEBP")
        self.assertEqual(getattr(Image.open(io.BytesIO(src)), "n_frames", 1), 3,
                         "fixture is not animated — vary the frames")
        out = _shrink_upload(src, ".webp")
        im = Image.open(io.BytesIO(out))
        self.assertLess(len(out), len(src))
        self.assertLessEqual(max(im.size), EMOJI_MAX_PX)
        self.assertEqual(im.format, "WEBP")
        self.assertEqual(getattr(im, "n_frames", 1), 3)


class LeavesWellEnoughAlone(unittest.TestCase):
    def test_already_emoji_sized_is_untouched(self):
        src = encode([noisy(64)], "PNG")
        self.assertEqual(_shrink_upload(src, ".png"), src)

    def test_never_returns_more_bytes_than_it_got(self):
        # just over the limit, and already tiny: re-encoding can easily come out bigger
        for px in (129, 130, 160):
            src = encode([Image.new("RGBA", (px, px), (0, 0, 0, 0))], "PNG")
            self.assertLessEqual(len(_shrink_upload(src, ".png")), len(src), f"{px}px inflated")

    def test_unreadable_bytes_return_the_original(self):
        src = b"this is not an image at all"
        self.assertEqual(_shrink_upload(src, ".png"), src)


if __name__ == "__main__":
    unittest.main()
