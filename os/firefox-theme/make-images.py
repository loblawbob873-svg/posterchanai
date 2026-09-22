#!/usr/bin/env python3
"""Draw the theme's images from the PosterChan logo (static/icon-512.png). Needs Pillow.

The PNGs are committed (so building the .xpi needs only the standard library — build.py); run this
again only when the logo or the frame colour changes, and commit what it writes.

THE BANNER IS A WATERMARK ON PURPOSE. Firefox paints `theme_frame` at the top-right of the window,
behind the TAB STRIP, and nothing stops a row of tabs running over it — so every pixel of it is a
background that tab titles can sit on. It is therefore dimmed until the background-tab text colour
reads at WCAG AA (4.5:1) against EVERY pixel of it composited over the frame colour, measured here
and asserted again by tests/test_firefox_theme.py. A full-strength logo would look better in a
screenshot with one tab open and make tab titles unreadable with twelve — the exact failure that
makes "many dark themes not readable".
"""
import json
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(os.path.dirname(HERE))
LOGO = os.path.join(ROOT, "static", "icon-512.png")
LOGO_BG = (26, 26, 46)           # the logo's own backdrop, keyed out for the banner
BANNER_H = 40                    # CSS px; the tab strip is ~40-44 high, the toolbar below is opaque
BANNER_W = 420
NEON, NEON2 = (0x3C, 0xE8, 0xFF), (0xFF, 0x5C, 0xF0)   # client.css --neon / --neon2


def hex_rgb(h):
    h = h.lstrip("#")
    return tuple(int(h[i:i + 2], 16) for i in (0, 2, 4))


def lum(rgb):
    def ch(c):
        c = c / 255.0
        return c / 12.92 if c <= 0.04045 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(v) for v in rgb[:3])
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def contrast(a, b):
    la, lb = lum(a), lum(b)
    hi, lo = max(la, lb), min(la, lb)
    return (hi + 0.05) / (lo + 0.05)


def over(fg, alpha, bg):
    return tuple(round(f * alpha + b * (1 - alpha)) for f, b in zip(fg, bg))


def keyed_logo():
    """The mascot with its navy square removed: near the backdrop colour → transparent, soft edge."""
    im = Image.open(LOGO).convert("RGBA")
    px = im.load()
    for y in range(im.height):
        for x in range(im.width):
            r, g, b, a = px[x, y]
            d = abs(r - LOGO_BG[0]) + abs(g - LOGO_BG[1]) + abs(b - LOGO_BG[2])
            if d <= 14:
                px[x, y] = (r, g, b, 0)
            elif d < 40:
                px[x, y] = (r, g, b, int(a * (d - 14) / 26))
    return im


# THE STRIP, not a watermark. The first version dimmed one tinted mascot until tab text read over
# it, which left a black frame and a purple smudge — readable, and not cyberpunk at all. Now the
# whole tab strip is drawn: a synthwave gradient, a faint neon grid, a lit skyline and a neon rule,
# every pixel of which tab titles can sit on is kept dark enough for them (4.5:1, measured below and
# in the test). The mascot is the one exception, in FULL colour, in MASCOT_ZONE at the right end —
# the last place a row of tabs reaches, and her orange hair is the logo; dimmed, it is not.
BANNER_W = 3840                  # wider than any screen, so the strip is covered edge to edge
MASCOT_ZONE = 150                # px at the right end that hold the mascot at full strength
RULE_H = 2                       # the neon rule under the tabs, where no title is ever drawn
STOPS = [(0.0, (70, 18, 140)), (0.4, (140, 20, 130)), (0.75, (10, 90, 120)), (1.0, (80, 16, 130))]


def _grad(t):
    for (t0, c0), (t1, c1) in zip(STOPS, STOPS[1:]):
        if t <= t1:
            u = (t - t0) / (t1 - t0)
            return tuple(round(c0[i] * (1 - u) + c1[i] * u) for i in range(3))
    return STOPS[-1][1]


def _cap(col, bg, text, min_ratio):
    """Darken a decoration colour just enough that tab text still reads over it."""
    c = col
    while contrast(text, c) < min_ratio:
        c = tuple(max(0, round(v * 0.92)) for v in c)
    return c


def banner(frame, text, min_ratio=4.6):
    import random
    rnd = random.Random(1984)
    W, H = BANNER_W, BANNER_H
    im = Image.new("RGBA", (W, H))
    d = ImageDraw.Draw(im)
    for x in range(W):
        d.line([(x, 0), (x, H - 1)], fill=_cap(_grad(x / (W - 1)), frame, text, min_ratio) + (255,))
    # Scanlines and a neon grid, dim.
    for y in range(0, H - RULE_H, 3):
        d.line([(0, y), (W, y)], fill=(0, 0, 0, 40))
    for x in range(0, W, 28):
        t = x / W
        col = tuple(round(NEON[i] * (1 - t) + NEON2[i] * t) for i in range(3))
        d.line([(x, 0), (x, H - RULE_H)], fill=_cap(tuple(round(v * .7) for v in col), frame, text, min_ratio) + (255,))
    # A skyline along the bottom, with lit windows.
    x = 0
    while x < W - MASCOT_ZONE:
        bw, bh = rnd.randint(14, 34), rnd.randint(8, H - 10)
        d.rectangle([x, H - RULE_H - bh, x + bw, H - RULE_H - 1], fill=(8, 4, 18, 255))
        for wy in range(H - RULE_H - bh + 3, H - RULE_H - 2, 4):
            for wx in range(x + 3, x + bw - 2, 5):
                if rnd.random() < .35:
                    col = NEON if rnd.random() < .6 else NEON2
                    d.rectangle([wx, wy, wx + 1, wy + 1], fill=_cap(col, frame, text, min_ratio) + (255,))
        x += bw + rnd.randint(2, 10)
    # The neon rule: full strength, cyan into magenta.
    for x in range(W):
        t = x / (W - 1)
        col = tuple(round(NEON[i] * (1 - t) + NEON2[i] * t) for i in range(3))
        d.line([(x, H - RULE_H), (x, H - 1)], fill=col + (255,))
    # Every pixel a tab title can sit on is checked here, not only in the test.
    px = im.load()
    for y in range(H - RULE_H):
        for x in range(W - MASCOT_ZONE):
            if contrast(text, px[x, y][:3]) < min_ratio:
                px[x, y] = _cap(px[x, y][:3], frame, text, min_ratio) + (255,)
    # The mascot, in full colour, on a dark plate with a neon ring.
    logo = keyed_logo()
    bust = logo.crop((150, 44, 366, 244))
    bh = H - RULE_H - 2
    bust = bust.resize((round(bust.width * bh / bust.height), bh), Image.LANCZOS)
    bx = W - bust.width - 44
    d.rounded_rectangle([bx - 6, 0, bx + bust.width + 6, H - RULE_H - 1], radius=8,
                        fill=(10, 6, 24, 255), outline=NEON2 + (255,), width=1)
    im.alpha_composite(bust, (bx, 1))
    return im, (bx, bust.width)


def pixels(im):
    return im.get_flattened_data() if hasattr(im, "get_flattened_data") else im.getdata()


def dim(layer, frame, text, min_ratio):
    for step in range(100, 4, -1):
        a = step / 100.0
        if all(contrast(text, over((r, g, b), a * al / 255.0, frame)) >= min_ratio
               for r, g, b, al in pixels(layer) if al):
            break
    out = layer.copy()
    out.putalpha(out.getchannel("A").point(lambda v: round(v * a)))
    return out, a


def icon(size):
    logo = Image.open(LOGO).convert("RGBA")
    im = logo.resize((size, size), Image.LANCZOS)
    mask = Image.new("L", (size, size), 0)
    ImageDraw.Draw(mask).rounded_rectangle([0, 0, size - 1, size - 1], radius=size // 5, fill=255)
    im.putalpha(mask)
    return im


if __name__ == "__main__":
    m = json.load(open(os.path.join(HERE, "manifest.json")))
    colors = m["theme"]["colors"]
    img_dir = os.path.join(HERE, "images")
    os.makedirs(img_dir, exist_ok=True)
    b, a = banner(hex_rgb(colors["frame"]), hex_rgb(colors["tab_background_text"]))
    b.convert("RGB").save(os.path.join(img_dir, "logo-banner.png"), optimize=True)
    for s in (48, 96, 128):
        icon(s).save(os.path.join(img_dir, "icon-%d.png" % s), optimize=True)
    print("wrote images/ (mascot at x=%d, %dpx wide)" % a)
