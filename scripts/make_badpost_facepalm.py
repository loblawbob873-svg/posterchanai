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

from PIL import Image, ImageDraw, ImageFilter, ImageFont

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
MASCOT = os.path.join(ROOT, "static", "mascot")
OUT = os.path.join(MASCOT, "badpost-facepalm.png")
SIZE = 720
YELLOW = (255, 230, 0, 255)          # the star on her hoodie


def figure() -> Image.Image:
    body = Image.open(os.path.join(MASCOT, "dance", "dance-5.webp")).convert("RGBA")
    arm = Image.open(os.path.join(MASCOT, "dance", "dance-2.webp")).convert("RGBA").crop((80, 0, 165, 140))
    big = Image.new("RGBA", (400, 400), (0, 0, 0, 0))
    big.alpha_composite(arm, (200 - 35, 200 - 38))                 # palm centre at (35, 38) of the crop
    big = big.rotate(6, resample=Image.BICUBIC, center=(200, 200))
    body.alpha_composite(big, (209 - 200, 121 - 200))               # palm over her eyes
    bust = body.crop((100, 50, 320, 212))
    return bust.resize((bust.width * 3, bust.height * 3), Image.LANCZOS)


def font(size: int):
    for name in ("orbitron.woff2", "inter.woff2"):
        try:
            return ImageFont.truetype(os.path.join(ROOT, "static", "fonts", name), size)
        except OSError:
            continue
    return ImageFont.load_default()


def outlined(draw, xy, text, fnt, fill, anchor="mm"):
    draw.text(xy, text, font=fnt, fill=fill, anchor=anchor, stroke_width=5, stroke_fill=(0, 0, 0, 255))


def main() -> None:
    canvas = Image.new("RGBA", (SIZE, SIZE), (0, 0, 0, 255))
    grad = ImageDraw.Draw(canvas)
    for y in range(SIZE):                                            # night-purple to near black
        t = y / SIZE
        grad.line([(0, y), (SIZE, y)], fill=(int(40 - 30 * t), int(12 - 6 * t), int(70 - 50 * t), 255))
    fig = figure()
    x, y = (SIZE - fig.width) // 2, SIZE - fig.height
    glow = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    glow.alpha_composite(fig, (x, y))
    shadow = glow.split()[3].filter(ImageFilter.GaussianBlur(14))
    canvas.alpha_composite(Image.merge("RGBA", (shadow.point(lambda v: 0),) * 3 + (shadow.point(lambda v: v * 0.7),)))
    canvas.alpha_composite(fig, (x, y))
    d = ImageDraw.Draw(canvas)
    # Anime gloom lines over her head: the universal "oh no" of the drawing style she is drawn in.
    head_x, head_top = x + 330, y + 60
    for i, dx in enumerate((-90, -60, -30, 0, 30, 60)):
        d.line([(head_x + dx, head_top - 40 + (i % 2) * 12), (head_x + dx, head_top + 30 + (i % 3) * 10)],
               fill=(120, 140, 255, 200), width=5)
    # A sweat drop beside the hand.
    d.ellipse([x + 470, y + 150, x + 500, y + 185], fill=(140, 200, 255, 235), outline=(255, 255, 255, 255), width=3)
    d.polygon([(x + 473, y + 160), (x + 485, y + 118), (x + 497, y + 160)], fill=(140, 200, 255, 235))
    outlined(d, (SIZE // 2, 62), "BAD POST", font(78), YELLOW)
    outlined(d, (SIZE // 2, 132), "OF THE DAY", font(48), (255, 255, 255, 255))
    outlined(d, (SIZE // 2, SIZE - 30), "*facepalm*", font(30), (255, 255, 255, 255))
    canvas.convert("RGB").save(OUT, optimize=True)
    print(f"wrote {OUT} ({os.path.getsize(OUT)} bytes)")


if __name__ == "__main__":
    main()
