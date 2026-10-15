#!/usr/bin/env python3
"""Build static/mascot/badpost-facepalm.png -- PosterChan facepalming, for the Bad Post of the Day bot.

Derived from the shipped mascot frames, never drawn from scratch, so it is the same character as the
desktop buddy: the body is dance-5 (hands on hips, the disappointed stance), the hand is dance-2's raised
open palm, laid over her eyes. The crop stops above the hips, so the two hip hands leave the frame and
the facepalm arm is the only one that reaches the face.

Run once after changing the mascot art (needs Pillow with FreeType, i.e. the app's venv):
    venv-unified/bin/python scripts/make_badpost_facepalm.py
"""
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASCOT = os.path.join(ROOT, "static", "mascot")
OUT = os.path.join(MASCOT, "badpost-facepalm.png")
SIZE = 720
YELLOW = (255, 230, 0, 255)          # the star on her hoodie


BUST = (105, 52, 315, 207)            # dance-5: hair top to mid-hoodie (the hip hands are out of frame)
SCALE = 720 * 0.95 / (BUST[2] - BUST[0])
PALM_AT = (212, 117)                   # dance-5 coordinates: both eyes behind the hand
ARM_TILT = -14                         # degrees; the elbow goes out to her right, off the bottom edge
HAND = 1.08                            # the hand a little larger than the waving one, so it covers both eyes
INK = (70, 32, 28, 255)                # the line-art brown of her outlines


def _up(im: Image.Image, k: float) -> Image.Image:
    return im.resize((round(im.width * k), round(im.height * k)), Image.LANCZOS)


def _arm() -> Image.Image:
    """dance-2's waving arm, cut out along the skin (no hair, no hoodie), MIRRORED -- a palm-out right
    hand mirrored is the BACK of her right hand, which is what a facepalm shows the viewer."""
    src = Image.open(os.path.join(MASCOT, "dance", "dance-2.webp")).convert("RGBA")
    cut = Image.new("L", src.size, 0)
    ImageDraw.Draw(cut).polygon([(86, 0), (150, 0), (150, 40), (140, 70), (146, 100), (150, 125),
                                 (122, 128), (106, 104), (98, 62), (86, 40)], fill=255)
    arm = Image.new("RGBA", src.size, (0, 0, 0, 0))
    arm.paste(src, (0, 0), Image.composite(src.split()[3], cut, cut))
    return ImageOps.mirror(arm.crop((80, 0, 165, 150)))


def figure() -> Image.Image:
    """Composed at the FINAL scale: the art is upscaled first and the arm's edge and outline are cut
    afterwards, so they are smooth lines rather than a 3x-magnified pixel staircase (the old image's
    pale, jagged halo). The forearm runs off the bottom of the frame, so there is no seam where an arm
    stops in mid-air."""
    body = _up(Image.open(os.path.join(MASCOT, "dance", "dance-5.webp")).convert("RGBA").crop(BUST), SCALE)
    k = SCALE * HAND
    arm = _up(_arm(), k)
    alpha = arm.split()[3].filter(ImageFilter.GaussianBlur(1.2)).point(lambda v: 255 if v > 150 else 0)
    alpha = alpha.filter(ImageFilter.MinFilter(3)).filter(ImageFilter.GaussianBlur(0.8))
    ring = alpha.filter(ImageFilter.MaxFilter(7))
    inked = Image.new("RGBA", arm.size, (0, 0, 0, 0))
    inked.paste(Image.new("RGBA", arm.size, INK), (0, 0), ring)
    arm.putalpha(alpha)
    inked.alpha_composite(arm)
    palm = ((84 - (112 - 80)) * k, 30 * k)          # palm centre of the mirrored crop, scaled
    pad = max(inked.size) * 2
    big = Image.new("RGBA", (pad, pad), (0, 0, 0, 0))
    big.alpha_composite(inked, (round(pad / 2 - palm[0]), round(pad / 2 - palm[1])))
    big = big.rotate(ARM_TILT, resample=Image.BICUBIC, center=(pad / 2, pad / 2))
    at = ((PALM_AT[0] - BUST[0]) * SCALE, (PALM_AT[1] - BUST[1]) * SCALE)
    out = body.copy()
    out.alpha_composite(big.crop((round(pad / 2 - at[0]), round(pad / 2 - at[1]),
                                  round(pad / 2 - at[0]) + out.width, round(pad / 2 - at[1]) + out.height)))
    return out.filter(ImageFilter.UnsharpMask(radius=2, percent=90, threshold=2))


def font(size: int):
    for name in ("orbitron.woff2", "inter.woff2"):
        try:
            return ImageFont.truetype(os.path.join(ROOT, "static", "fonts", name), size)
        except OSError:
            continue
    return ImageFont.load_default()


def outlined(draw, xy, text, fnt, fill, anchor="mm"):
    draw.text(xy, text, font=fnt, fill=fill, anchor=anchor, stroke_width=5, stroke_fill=(0, 0, 0, 255))


def _gloom(fig_alpha, head, top):
    """Anime gloom: a cold shadow that falls over the top of her head and fades out by her eyes. It used to
    be a fan of hard blue lines, which on a phone read as a rendering glitch ("what are those long blue
    vertical lines? ugly", 2026-10-15). Only where she is drawn (masked by the figure), so the background
    stays clean."""
    size = fig_alpha.size
    ramp = Image.new("L", size, 0)
    rd = ImageDraw.Draw(ramp)
    for yy in range(max(0, top - 20), min(size[1], top + 60)):
        t = (yy - (top - 20)) / 80
        rd.line([(head - 170, yy), (head + 170, yy)], fill=int(130 * (1 - t) ** 1.6))
    ramp = ramp.filter(ImageFilter.GaussianBlur(12))
    mask = Image.fromarray((np.asarray(ramp, dtype=np.float32) * np.asarray(fig_alpha, dtype=np.float32) / 255).astype("uint8"))
    return Image.merge("RGBA", (Image.new("L", size, 40), Image.new("L", size, 50), Image.new("L", size, 120), mask))


def _sweat(d, x, y):
    d.ellipse([x - 22, y - 10, x + 22, y + 34], fill=(150, 205, 255, 255), outline=(255, 255, 255, 255), width=4)
    d.polygon([(x - 20, y + 4), (x, y - 48), (x + 20, y + 4)], fill=(150, 205, 255, 255))
    d.line([(x - 21, y + 6), (x, y - 48), (x + 21, y + 6)], fill=(255, 255, 255, 255), width=4)
    d.ellipse([x - 10, y + 2, x - 2, y + 14], fill=(255, 255, 255, 255))     # the glint


def main() -> None:
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 255))
    grad = ImageDraw.Draw(canvas)
    for y in range(SIZE):                                            # night-purple to near black
        t = y / SIZE
        grad.line([(0, y), (SIZE, y)], fill=(int(40 - 30 * t), int(12 - 6 * t), int(70 - 50 * t), 255))
    fig = figure()
    x, y = (SIZE - fig.width) // 2, SIZE - fig.height
    head = x + round((209 - BUST[0]) * SCALE)                        # dance-5's face centre
    crown = y + round((84 - BUST[1]) * SCALE)              # the top of her hair
    # A soft spotlight behind her, so the dark hoodie does not sink into the dark background.
    spot = Image.new("L", canvas.size, 0)
    ImageDraw.Draw(spot).ellipse([head - 300, crown - 60, head + 300, SIZE + 260], fill=150)
    spot = spot.filter(ImageFilter.GaussianBlur(70))
    canvas.alpha_composite(Image.merge("RGBA", (Image.new("L", canvas.size, 120), Image.new("L", canvas.size, 70),
                                                 Image.new("L", canvas.size, 170), spot)))
    glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    glow.alpha_composite(fig, (x, y))
    shadow = glow.split()[3].filter(ImageFilter.GaussianBlur(14))
    canvas.alpha_composite(Image.merge("RGBA", (shadow.point(lambda v: 0),) * 3 + (shadow.point(lambda v: v * 0.7),)))
    canvas.alpha_composite(fig, (x, y))
    canvas.alpha_composite(_gloom(glow.split()[3], head, crown))
    d = ImageDraw.Draw(canvas)
    _sweat(d, head + 122, crown + 70)
    outlined(d, (SIZE // 2, 62), "BAD POST", font(78), YELLOW)
    outlined(d, (SIZE // 2, 132), "OF THE DAY", font(48), (255, 255, 255, 255))
    # The caption sits in the clear space beside her head -- it used to be printed across the star.
    outlined(d, (head + 222, crown + 190), "*facepalm*", font(30), (255, 255, 255, 255))
    canvas.convert("RGB").save(OUT, optimize=True)
    print(f"wrote {OUT} ({os.path.getsize(OUT)} bytes)")


if __name__ == "__main__":
    main()
