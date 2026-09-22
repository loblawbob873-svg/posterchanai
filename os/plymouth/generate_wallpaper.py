#!/usr/bin/env python3
"""Render the PosterChanOS desktop/OS wallpaper — the mascot's desk, full-bleed at 4K.

The windowed desktop (static/js/client/os.js) and the PosterChanOS session (which IS that desktop in
--shell) share ONE default wallpaper: static/os-wallpaper-bg.webp. This draws it at 3840x2160 so it
is crisp on a 4K monitor and downscales cleanly to 1080p — background-size:cover in client.css
handles every aspect ratio. Regenerate:

    venv-unified/bin/python os/plymouth/generate_wallpaper.py

THE ART IS SQUARE AND THE SCREEN IS NOT, and how that gap is filled is the whole job. A cover-crop of
a 1:1 picture to 16:9 throws away 44% of it — here the moon, the city skyline and every slogan — and
upscales what is left by 3x, which on a 4K panel is visibly soft. So the picture is fitted to the
HEIGHT (a 1.7x upscale, all of it kept) and set flush right, and the rest of the canvas is the same
picture blurred, dimmed and washed with the boot splash's palette: an out-of-focus continuation of
its own light, not a border. The seam is feathered over 300px and lands in the artwork's dark
left-hand foliage, so there is nothing to see.

Icons and their labels live in the TOP LEFT (os.js lays them out from there), which is precisely
where that dimmed ambient side is, and `.os-desk::before` in client.css darkens the same corner
further — the two are deliberate partners. Keep any future art's busy half on the right.
"""
import math
import os
import sys
from pathlib import Path

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_theme as T   # palette + glow (import-safe)

ROOT = Path(__file__).resolve().parents[2]
ART = Path(os.environ.get("PC_WALLPAPER_ART") or Path(__file__).resolve().parent / "wallpaper-art.webp")
OUT = Path(os.environ.get("PC_WALLPAPER_OUT") or ROOT / "static/os-wallpaper-bg.webp")
W, H = 3840, 2160            # 4K, 16:9; cover-scaled to any monitor
FEATHER = 520                # px the sharp picture fades into its own blur
ZOOM, ANCHOR = 1.22, 0.45    # the art is set a little larger than the screen is tall, cropped there


def gradient():
    """The cyan wash + magenta corner, rendered small (it is smooth) and upscaled — fast and clean."""
    w, h = W // 8, H // 8
    img = Image.new("RGB", (w, h), T.BG)
    px = img.load()
    for y in range(h):
        for x in range(w):
            d = math.hypot((x / w) - 0.5, (y / h) - 0.40)
            k = max(0.0, 1.0 - d * 1.9)
            m = max(0.0, 1.0 - math.hypot(x / w - 1.0, y / h - 1.0) * 2.2)
            px[x, y] = (T.BG[0] + int(8 * k + 22 * m),
                        T.BG[1] + int(26 * k + 4 * m),
                        T.BG[2] + int(34 * k + 24 * m))
    return img.resize((W, H), Image.LANCZOS)


def _art():
    if not ART.exists():
        raise SystemExit(f"wallpaper art missing: {ART}")
    return Image.open(ART).convert("RGB")


def ambient(art):
    """The whole canvas: the art cover-scaled, blurred and dimmed, washed with the OS palette.

    This is what the icon corner is drawn on, so it is dark and featureless on purpose — a blur is
    not enough by itself (a bright out-of-focus neon sign still eats a label)."""
    s = max(W / art.width, H / art.height)
    big = art.resize((math.ceil(art.width * s), math.ceil(art.height * s)), Image.LANCZOS)
    x = (big.width - W) // 2
    y = (big.height - H) // 2
    bg = big.crop((x, y, x + W, y + H)).filter(ImageFilter.GaussianBlur(90))
    bg = ImageEnhance.Brightness(bg).enhance(0.46)
    bg = ImageEnhance.Color(bg).enhance(0.72)
    return Image.blend(bg, gradient(), 0.22)


def _panel_size(art):
    h = round(H * ZOOM)
    return round(art.width * h / art.height), h


def panel(art):
    """The art, set a little larger than the screen and flush right, with a feathered left edge.

    Fitted exactly to the height it filled only 56% of a 4K canvas and the empty third read as a
    letterbox. ZOOM trades the top and bottom tenth of the picture (shelf edge, foreground leaves)
    for a scene that reaches the middle of the screen; ANCHOR keeps the crop off her face."""
    w, h = _panel_size(art)
    im = art.resize((w, h), Image.LANCZOS)
    top = round((h - H) * ANCHOR)
    im = im.crop((0, top, w, top + H))
    h = H
    im = ImageEnhance.Sharpness(im).enhance(1.4)      # a 1.7x upscale is soft; this is not a halo
    mask = Image.new("L", (w, h), 255)
    md = ImageDraw.Draw(mask)
    for i in range(FEATHER):
        # smoothstep, so neither end of the fade has a visible edge
        t = i / FEATHER
        md.line([(i, 0), (i, h)], fill=int(255 * (t * t * (3 - 2 * t))))
    im.putalpha(mask)
    return im


def vignette():
    """Corners pulled down — it holds the eye on the desk and keeps the taskbar edge quiet."""
    v = Image.new("L", (W // 8, H // 8), 0)
    px = v.load()
    for y in range(v.height):
        for x in range(v.width):
            d = math.hypot((x / v.width - 0.5) * 1.05, (y / v.height - 0.5) * 1.25)
            px[x, y] = int(255 * min(1.0, max(0.0, (d - 0.40) / 0.62)) ** 1.6 * 0.60)
    return v.resize((W, H), Image.LANCZOS)


def main():
    art = _art()
    img = ambient(art).convert("RGBA")
    img.alpha_composite(panel(art), (W - _panel_size(art)[0], 0))
    dark = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    dark.putalpha(vignette())
    img.alpha_composite(dark)
    img = img.convert("RGB")
    img.save(OUT, "WEBP", quality=88, method=6)
    print("wrote", OUT, img.size, f"{OUT.stat().st_size // 1024} KiB")


if __name__ == "__main__":
    main()
