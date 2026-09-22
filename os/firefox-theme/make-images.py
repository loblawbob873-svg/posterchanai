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
# The CLOSE-UP portrait, not the full-body logo: at tab-strip height a whole figure is a smudge,
# and "improve the PosterChan avatar so it's more visible" means a FACE. Same character.
FACE = os.path.join(ROOT, "static", "posterchan-relay.png")
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
# A calm wash: indigo → violet → a hint of teal at the far right, all dark enough for tab text.
STOPS = [(0.0, (46, 16, 96)), (0.45, (92, 20, 110)), (0.8, (30, 40, 104)), (1.0, (12, 58, 92))]


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
    """A CALM strip with ONE thing on it: her.

    The first attempt drew a lit skyline, a neon grid and scanlines across the whole tab bar —
    "too noisy with the city background", and every bright dot competed with the mascot it was
    supposed to frame. Art themes that work (the Sleeping Miku theme the user pointed at) are a
    smooth dark wash with a single character on one side. So: a soft horizontal gradient, one hair-
    thin neon rule under the tabs, and the mascot HEAD (not a bust — at 40px a head is a face and a
    bust is a smudge) at full colour in MASCOT_ZONE at the right end.

    Every pixel a tab title can sit on is still capped at 4.5:1 against the tab text; the mascot's
    own corner is the exception, and it is the last place a row of tabs reaches.
    """
    W, H = BANNER_W, BANNER_H
    im = Image.new("RGBA", (W, H))
    d = ImageDraw.Draw(im)
    for x in range(W):
        d.line([(x, 0), (x, H - 1)], fill=_cap(_grad(x / (W - 1)), frame, text, min_ratio) + (255,))
    # A single neon rule along the bottom: cyan into magenta, full strength — tab titles are centred,
    # never on the bottom rows, and it is what makes the strip read as PosterChan at a glance.
    for x in range(W):
        t = x / (W - 1)
        col = tuple(round(NEON[i] * (1 - t) + NEON2[i] * t) for i in range(3))
        d.line([(x, H - RULE_H), (x, H - 1)], fill=col + (255,))
    # The mascot: her HEAD, as tall as the strip, full colour, with a soft glow behind it so she
    # separates from the wash without a hard plate (a bordered box is the "sticker" look).
    face = Image.open(FACE).convert("RGBA")
    head = face.crop((36, 64, 212, 240))           # hood, face and chin, square
    hh = H - RULE_H
    head = head.resize((hh, hh), Image.LANCZOS)
    # Feathered into the wash: an elliptical alpha ramp, so there is no box edge or sticker outline.
    mask = Image.new("L", head.size, 0)
    md = ImageDraw.Draw(mask)
    for r in range(hh // 2, 0, -1):
        md.ellipse([hh // 2 - r, hh // 2 - r, hh // 2 + r, hh // 2 + r],
                   fill=int(255 * min(1.0, (1 - r / (hh / 2)) * 3.2)))
    head.putalpha(Image.composite(head.getchannel("A"), Image.new("L", head.size, 0), mask.point(lambda v: 255 if v else 0))
                  .point(lambda v: v) if False else Image.eval(mask, lambda v: v))
    hx = W - head.width - 40
    glow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    cx, cy = hx + head.width // 2, hh // 2
    for r in range(hh, 0, -2):
        a = int(46 * (1 - r / hh) ** 2)
        gd.ellipse([cx - r, cy - r // 2 - 2, cx + r, cy + r // 2 + 2], fill=NEON2 + (a,))
    im.alpha_composite(glow)
    # Everything a tab title can sit on, checked here as well as in the test.
    px = im.load()
    for y in range(H - RULE_H):
        for x in range(W - MASCOT_ZONE):
            if contrast(text, px[x, y][:3]) < min_ratio:
                px[x, y] = _cap(px[x, y][:3], frame, text, min_ratio) + (255,)
    im.alpha_composite(head, (hx, 0))
    return im, (hx, head.width)


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
