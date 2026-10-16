#!/usr/bin/env python3
"""Build static/mascot/badpost-facepalm.png -- PosterChan facepalming, for the Bad Post of the Day bot.

Derived from the shipped mascot frames, never drawn from scratch, so it is the same character as the
desktop buddy: the body is dance-5 (hands on hips, the disappointed stance), cropped above the hips so the hip
hands leave the frame; her right hand -- drawn in her line-art colours, back of the hand to the viewer, out of
the hoodie sleeve -- covers her eyes.

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
SKIN_SHADE = (240, 196, 182, 255)
SLEEVE = (26, 24, 30, 255)             # the hoodie
SLEEVE_HI = (58, 54, 66, 255)
SS = 4                                 # the hand is drawn at 4x and scaled down: smooth lines, no staircase


def _up(im: Image.Image, k: float) -> Image.Image:
    return im.resize((round(im.width * k), round(im.height * k)), Image.LANCZOS)


def _hand(unit: float) -> Image.Image:
    """Her RIGHT hand seen from the BACK, the way a facepalm looks to the person watching: fingers together over her
    eyes, thumb on the outside, the wrist going into the hoodie's cuff and the sleeve running off the bottom of the
    frame. Drawn (no frame of the mascot shows a hand like this) in her own line-art colours; `unit` = one dance-5
    pixel at the final scale, so the hand is sized to HER face (the previous image reused the waving arm: a bare arm,
    palm to the viewer -- "the hand is backwards and longer than normal", 2026-10-16).
    Local frame: wrist at (0, 0), fingers pointing up (-y); rotated by the caller."""
    u = unit * SS
    W, H = int(160 * u), int(160 * u)
    im = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    ox, oy = W / 2, H * 0.40                    # the wrist
    P = lambda x, y: (ox + x * u, oy + y * u)  # noqa: E731
    lw = max(2, round(1.0 * u))
    # the sleeve: a loose hoodie sleeve -- wide, rounded, a fold or two -- from the cuff down out of the frame
    sleeve = [P(-12, 3), P(-15, 14), P(-21, 30), P(-26, 50), P(-27, 70), P(25, 70), P(23, 48), P(19, 28), P(14, 12), P(12, 3)]
    d.polygon(sleeve, fill=SLEEVE, outline=(0, 0, 0, 255), width=lw)
    for x0, y0, x1, y1 in ((-4, 14, -9, 40), (6, 20, 9, 52), (-15, 30, -19, 58)):
        d.line([P(x0, y0), P(x1, y1)], fill=SLEEVE_HI, width=round(1.2 * u))
    d.rounded_rectangle([P(-13, -1), P(13, 7)], radius=3 * u, fill=SLEEVE, outline=(0, 0, 0, 255), width=lw)
    d.line([P(-11, 3), P(11, 3)], fill=SLEEVE_HI, width=round(.8 * u))
    # the back of the hand
    back = [P(-9.5, -1), P(9.5, -1), P(13, -12), P(13.5, -22), P(-12.5, -23), P(-11.5, -11)]
    d.polygon(back, fill=SKIN, outline=INK, width=lw)
    d.line([P(-4, -6), P(-2, -18)], fill=SKIN_SHADE, width=round(.9 * u))          # a tendon or two
    d.line([P(4, -6), P(5, -18)], fill=SKIN_SHADE, width=round(.9 * u))
    # four fingers side by side (index on the thumb side = the viewer's left), together, gently fanned
    fingers = [(-9.3, -22, 6.4, 20, -3), (-3.1, -23, 6.6, 23, -1), (3.2, -23, 6.4, 21.5, 1.5), (9.2, -22, 5.8, 17, 4)]
    for cx, top, w, ln, lean in fingers:
        tx = cx + lean
        tip = top - ln
        d.polygon([P(cx - w / 2, top + 1.5), P(cx + w / 2, top + 1.5), P(tx + w / 2, tip + w / 2), P(tx - w / 2, tip + w / 2)],
                  fill=SKIN, outline=INK, width=lw)
        d.ellipse([P(tx - w / 2, tip), P(tx + w / 2, tip + w)], fill=SKIN, outline=INK, width=lw)
        d.polygon([P(cx - w / 2 + .7, top), P(cx + w / 2 - .7, top), P(tx + w / 2 - .7, tip + w / 2),
                   P(tx - w / 2 + .7, tip + w / 2)], fill=SKIN)                                         # no seam at the tip
        d.ellipse([P(tx - w * .22, tip + w * .25), P(tx + w * .22, tip + w * .75)], fill=(255, 220, 210, 255))   # the nail
        k = top - ln * .48
        d.arc([P(cx + lean * .5 - w * .3, k - 1.2), P(cx + lean * .5 + w * .3, k + 1.2)], 200, 340, fill=SKIN_SHADE,
              width=round(.8 * u))                                                                  # the middle joint
    # the thumb, along the outer edge toward her temple
    th = [P(-10.5, -1.5), P(-15.5, -6.5), P(-19.6, -13.5), P(-14.4, -16.5), P(-11.8, -12.5)]
    d.polygon(th, fill=SKIN, outline=INK, width=lw)
    d.ellipse([P(-20.6, -17.8), P(-14.2, -11.4)], fill=SKIN, outline=INK, width=lw)               # the thumb tip, rounded
    d.polygon([P(-10.8, -2.4), P(-15.2, -7), P(-19, -13.6), P(-14.8, -15.8), P(-12.4, -12.4)], fill=SKIN)  # no seam
    d.ellipse([P(-19, -16.6), P(-16.2, -13.8)], fill=(255, 220, 210, 255))                       # its nail
    d.line([P(-11.6, -12.4), P(-10.6, -2)], fill=INK, width=lw)
    d.line([P(12, -4), P(12.8, -20)], fill=SKIN_SHADE, width=round(1.6 * u))      # shade on the far edge
    return im


def figure() -> Image.Image:
    """dance-5 (hands on hips, the disappointed stance), cropped above the hips, with her right hand over her eyes."""
    body = _up(Image.open(os.path.join(MASCOT, "dance", "dance-5.webp")).convert("RGBA").crop(BUST), SCALE)
    hand = _hand(SCALE)
    hand = hand.rotate(-20, resample=Image.BICUBIC, center=(hand.width / 2, hand.height * 0.40), expand=False)
    hand = hand.resize((hand.width // SS, hand.height // SS), Image.LANCZOS)
    # the fingers' middle lands on her eyes: in the hand's frame that is ~26 dance-5 px above the wrist, tilted
    import math
    a = math.radians(20)
    tip_dx, tip_dy = 31 * math.sin(a) * SCALE, -31 * math.cos(a) * SCALE
    eyes = ((EYES[0] - BUST[0]) * SCALE, (EYES[1] - BUST[1]) * SCALE)
    wrist = (eyes[0] - tip_dx, eyes[1] - tip_dy)
    out = body.copy()
    out.alpha_composite(hand, (round(wrist[0] - hand.width / 2), round(wrist[1] - hand.height * 0.40)))
    return out.filter(ImageFilter.UnsharpMask(radius=2, percent=60, threshold=2))


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
