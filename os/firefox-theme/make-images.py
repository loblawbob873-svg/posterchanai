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


HOLO_DARK, HOLO_LIGHT = (46, 20, 84), (150, 34, 158)   # deep violet → neon magenta, both DARK


def hologram(im):
    """Recolour the logo as a violet-to-magenta duotone. Readability caps how BRIGHT a pixel here
    may be, not how saturated: a deep magenta is dark enough for light tab text to read over it and
    still unmistakably neon, where the logo's own orange hair would have to be dimmed to mud. Dark
    parts (the black hoodie — most of her) start at violet rather than black, or she vanishes."""
    out = im.copy()
    px = out.load()
    for y in range(out.height):
        for x in range(out.width):
            r, g, b, a = px[x, y]
            if not a:
                continue
            t = 0.25 + 0.75 * (lum((r, g, b)) ** 0.5)
            px[x, y] = tuple(round(HOLO_DARK[i] * (1 - t) + HOLO_LIGHT[i] * t) for i in range(3)) + (a,)
    return out


def banner(frame, text, min_ratio=4.5):
    logo = keyed_logo()
    # Head and hood: the part that is still recognisably her at 40 pixels tall.
    bust = logo.crop((150, 44, 366, 244))
    bust = bust.resize((round(bust.width * BANNER_H / bust.height), BANNER_H), Image.LANCZOS)
    bust = hologram(bust)
    mascot = Image.new("RGBA", (BANNER_W, BANNER_H), (0, 0, 0, 0))
    # Clear of the right edge, where Firefox puts the "list all tabs" button in every layout.
    mascot.alpha_composite(bust, (BANNER_W - bust.width - 52, 0))
    # A neon rule along the bottom, cyan fading in from the left to magenta under the mascot.
    rule = Image.new("RGBA", (BANNER_W, BANNER_H), (0, 0, 0, 0))
    d = ImageDraw.Draw(rule)
    for x in range(BANNER_W):
        t = x / (BANNER_W - 1)
        col = tuple(round(NEON[i] * (1 - t) + NEON2[i] * t) for i in range(3))
        d.point((x, BANNER_H - 1), fill=col + (round(255 * t ** 1.6),))
    # Each layer is dimmed on its own until the tab text reads on every pixel of it — the largest
    # opacity that still passes — then the two are stacked. They do not overlap except along the
    # bottom row, and the stack is re-checked below, so the guarantee holds for what is SHIPPED.
    mascot, a1 = dim(mascot, frame, text, min_ratio)
    rule, a2 = dim(rule, frame, text, min_ratio)
    out = Image.new("RGBA", (BANNER_W, BANNER_H), (0, 0, 0, 0))
    out.alpha_composite(rule)
    out.alpha_composite(mascot)
    out, a3 = dim(out, frame, text, min_ratio)
    return out, (a1, a2, a3)


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
    b.save(os.path.join(img_dir, "logo-banner.png"), optimize=True)
    for s in (48, 96, 128):
        icon(s).save(os.path.join(img_dir, "icon-%d.png" % s), optimize=True)
    print("wrote images/ (banner opacity: mascot %.2f, rule %.2f, stack %.2f)" % a)
