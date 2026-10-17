#!/usr/bin/env python3
"""Build static/mascot/badpost-facepalm.png -- PosterChan in tears, for the Bad Post of the Day bot.

Derived from the shipped mascot frames, never drawn from scratch, so it is the same character as the
desktop buddy: the body is dance-5 (hands on hips), cropped above the hips; her smiling face is painted out and
redrawn sad and crying. No hand: every drawn facepalm hand read as wrong ("just make her look sad and crying,
stop with the hand crap", 2026-10-16).

Run once after changing the mascot art (needs Pillow with FreeType, i.e. the app's venv):
    venv-unified/bin/python scripts/make_badpost_facepalm.py
"""
import os

import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASCOT = os.path.join(ROOT, "static", "mascot")
OUT = os.path.join(MASCOT, "badpost-facepalm.png")
SIZE = 720
YELLOW = (255, 230, 0, 255)          # the star on her hoodie


BUST = (105, 52, 315, 207)            # dance-5: hair top to mid-hoodie (the hip hands are out of frame)
SCALE = 720 * 0.95 / (BUST[2] - BUST[0])
EYES = (213, 116)                      # dance-5 coordinates: between her eyes
INK = (70, 32, 28, 255)                # the line-art brown of her outlines
SKIN = (255, 233, 220, 255)            # sampled from her face
TEAR = (150, 210, 255, 255)
TEAR_HI = (235, 248, 255, 255)
SS = 4                                 # the face is drawn at 4x and scaled down: smooth lines, no staircase
FACE = (191, 108, 233, 142)            # dance-5: brows to chin, the part of her face that is redrawn


def _up(im: Image.Image, k: float) -> Image.Image:
    return im.resize((round(im.width * k), round(im.height * k)), Image.LANCZOS)


def _wipe_features(im: Image.Image) -> Image.Image:
    """Her smiling brows, eyes and mouth painted out with her own skin, leaving the hair that falls over the face."""
    a = np.asarray(im).copy()
    x0, y0, x1, y1 = FACE
    r, g, b = (a[y0:y1, x0:x1, i].astype(int) for i in range(3))
    hair = (r > 170) & (r - b > 120) & (g < 200)
    for ex0, ex1 in ((193, 209), (215, 232)):           # her eyes are orange too: never hair
        hair[116 - y0:127 - y0, ex0 - x0:ex1 - x0] = False
    keep = hair | (a[y0:y1, x0:x1, 3] < 200)
    face = a[y0:y1, x0:x1]
    face[~keep] = SKIN
    return Image.fromarray(a)


def _sad_face(size, unit: float) -> Image.Image:
    """Sad and crying, in her line-art colours: brows raised in the middle, eyes squeezed shut, tears streaming down
    both cheeks, a wobbling frown. `unit` = one dance-5 pixel at the final scale."""
    u = unit * SS
    im = Image.new("RGBA", (size[0] * SS, size[1] * SS), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    P = lambda x, y: ((x - BUST[0]) * u, (y - BUST[1]) * u)  # noqa: E731
    lw = round(1.3 * u)
    # blush under the eyes
    for cx in (199, 224):
        d.ellipse([P(cx - 5, 125), P(cx + 5, 129)], fill=(255, 150, 160, 110))
    # brows: the inner ends lifted -- the shape of worry
    d.line([P(195.5, 115.5), P(201, 113.5), P(206.5, 110.5)], fill=INK, width=lw, joint="curve")
    d.line([P(215.5, 110.5), P(221, 113.5), P(226.5, 115.5)], fill=INK, width=lw, joint="curve")
    # eyes squeezed shut, curving down at the outside, a lash at each outer corner
    for cx, side in ((201, -1), (222, 1)):
        d.arc([P(cx - 6, 117.5), P(cx + 6, 125.5)], 200, 340, fill=INK, width=round(1.6 * u))
        d.line([P(cx + side * 5.6, 120), P(cx + side * 7.6, 122.4)], fill=INK, width=lw)
    # tears: a stream from each eye down the cheek, a drop gathering at the end
    for cx, side in ((198, -1), (225, 1)):
        x, top = cx + side * 1.5, 121.5
        stream = [P(x - 1.6, top), P(x + 1.6, top), P(x + side * 1.2 + 1.9, 134), P(x + side * 1.2 - 1.9, 134)]
        d.polygon(stream, fill=TEAR, outline=TEAR_HI, width=round(.6 * u))
        dx, dy = x + side * 1.2, 136
        d.ellipse([P(dx - 2.6, dy - 2.6), P(dx + 2.6, dy + 2.6)], fill=TEAR, outline=TEAR_HI, width=round(.6 * u))
        d.ellipse([P(dx - 1.5, dy - 1.6), P(dx - .3, dy - .4)], fill=TEAR_HI)
        d.line([P(x - .6, top + 2), P(x + side * .4 - .6, 131)], fill=TEAR_HI, width=round(.5 * u))
    # the mouth: a small open frown, wobbling
    d.chord([P(206.5, 134.5), P(215.5, 141.5)], 180, 360, fill=(120, 40, 50, 255), outline=INK, width=lw)
    d.line([P(206.5, 138.2), P(208.5, 137.6), P(211, 138.3), P(213.5, 137.6), P(215.5, 138.2)], fill=INK, width=lw,
           joint="curve")
    return im.resize(size, Image.LANCZOS)


def figure() -> Image.Image:
    """dance-5 (hands on hips, the disappointed stance), cropped above the hips, her face redrawn sad and crying."""
    src = _wipe_features(Image.open(os.path.join(MASCOT, "dance", "dance-5.webp")).convert("RGBA"))
    body = _up(src.crop(BUST), SCALE)
    body.alpha_composite(_sad_face(body.size, SCALE))
    return body.filter(ImageFilter.UnsharpMask(radius=2, percent=60, threshold=2))


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
    outlined(d, (SIZE // 2, 62), "BAD POST", font(78), YELLOW)
    outlined(d, (SIZE // 2, 132), "OF THE DAY", font(48), (255, 255, 255, 255))
    # The caption sits in the clear space beside her head -- it used to be printed across the star.
    outlined(d, (head + 222, crown + 190), "*sob*", font(30), (255, 255, 255, 255))
    canvas.convert("RGB").save(OUT, optimize=True)
    print(f"wrote {OUT} ({os.path.getsize(OUT)} bytes)")


if __name__ == "__main__":
    main()
