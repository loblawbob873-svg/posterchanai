#!/usr/bin/env python3
"""THE POSTERCHAN APP ICON, from two masters to every file that shows it.

    venv-unified/bin/python scripts/make_app_icon.py [--art branding/posterchan-art-source.png --face X,Y,SIZE]

"postercham needs a proper app icon like you did for posterfox. the square one is ugly ... that can be the favicon and
main icon" (2026-10-29). The old icon was the full standing figure on a dark square, which at favicon and launcher
sizes is a thin sliver. This is PosterFox's recipe (posterfox docs/MAINTAINING.md, "The icon"): a drawing by this
node's own image model on a plain light background, cut out by FLOOD FILL from the edges (an AI background remover
cut parts of PosterFox's drawing off), outlined white like a sticker, on a round night-navy badge with the
cyan-to-magenta PosterChan rim and glow. Two masters, as there: the full drawing above 48 px, her face at 48 px
and below (the launcher's mdpi size, favicons), where a whole scene turns to mud.

With --art, the masters are (re)made from a new drawing (--face: the square, in drawing pixels, that holds her face);
without it, every output is redrawn from the masters in branding/. Outputs: static/favicon.png, icon-192/512,
the maskable PWA icons, apple-touch-icon, desktop/icon.png, and the Android launcher (legacy, round and the adaptive
foreground at every density; its plate colour is the badge's navy).
"""
import argparse
import math
import pathlib
import re

from PIL import Image, ImageChops, ImageDraw, ImageFilter

ROOT = pathlib.Path(__file__).resolve().parent.parent
BRAND = ROOT / "branding"
FULL, SMALL, SOURCE = BRAND / "posterchan-1024.png", BRAND / "posterchan-small-1024.png", BRAND / "posterchan-art-source.png"
NAVY = (20, 15, 47)                       # the badge, sampled from PosterFox's
CYAN, MAGENTA = (60, 232, 255), (255, 79, 216)
RES = ROOT / "mobile/android/app/src/main/res"
DENS = {"mdpi": 1, "hdpi": 1.5, "xhdpi": 2, "xxhdpi": 3, "xxxhdpi": 4}


def cutout(img, tol=38):
    """The drawing without its background: flood fill from every edge pixel over colours near the background's."""
    img = img.convert("RGBA")
    w, h = img.size
    px = img.load()
    bg = px[2, 2][:3]
    near = lambda c: sum(abs(c[i] - bg[i]) for i in range(3)) <= tol
    seen = bytearray(w * h)
    stack = [(x, y) for x in range(w) for y in (0, h - 1)] + [(x, y) for y in range(h) for x in (0, w - 1)]
    while stack:
        x, y = stack.pop()
        i = y * w + x
        if seen[i] or not near(px[x, y]):
            continue
        seen[i] = 1
        px[x, y] = (0, 0, 0, 0)
        if x > 0: stack.append((x - 1, y))
        if x < w - 1: stack.append((x + 1, y))
        if y > 0: stack.append((x, y - 1))
        if y < h - 1: stack.append((x, y + 1))
    return img


def sticker(art, outline=14):
    """A white outline around the cut-out drawing, as a sticker has."""
    a = art.getchannel("A").point(lambda v: 255 if v > 8 else 0)
    grown = a.filter(ImageFilter.MaxFilter(outline * 2 + 1)).filter(ImageFilter.GaussianBlur(1.2))
    white = Image.new("RGBA", art.size, (255, 255, 255, 0)); white.putalpha(grown)
    return Image.alpha_composite(white, art)


def badge(art, size=1024):
    """Round navy badge, cyan->magenta rim with a soft glow, the drawing inside it."""
    S = size
    out = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    c, r_rim, r_disc = S / 2, S * 0.462, S * 0.442
    # the rim's colour runs around the circle: cyan at the top-left, magenta at the bottom-right
    rim = Image.new("RGBA", (S, S))
    rp = rim.load()
    for y in range(S):
        for x in range(S):
            t = (math.atan2(y - c, x - c) + math.pi * 0.75) % (2 * math.pi) / (2 * math.pi)
            k = 1 - abs(2 * t - 1)
            rp[x, y] = tuple(int(CYAN[i] * (1 - k) + MAGENTA[i] * k) for i in range(3)) + (255,)
    ring = Image.new("L", (S, S), 0)
    ImageDraw.Draw(ring).ellipse([c - r_rim, c - r_rim, c + r_rim, c + r_rim], fill=255)
    glow = ring.filter(ImageFilter.GaussianBlur(S * 0.018))
    halo = rim.copy(); halo.putalpha(glow.point(lambda v: int(v * 0.85)))
    out = Image.alpha_composite(out, halo)
    band = rim.copy(); band.putalpha(ring)
    out = Image.alpha_composite(out, band)
    disc = Image.new("L", (S, S), 0)
    ImageDraw.Draw(disc).ellipse([c - r_disc, c - r_disc, c + r_disc, c + r_disc], fill=255)
    fill = Image.new("RGBA", (S, S), NAVY + (255,)); fill.putalpha(disc)
    out = Image.alpha_composite(out, fill)
    # the drawing: fit its visible box inside the disc, then clip to the disc
    bbox = art.getchannel("A").getbbox() or (0, 0) + art.size
    a = art.crop(bbox)
    room = r_disc * 2 * 0.84
    k = room / max(a.size)
    a = a.resize((max(1, int(a.size[0] * k)), max(1, int(a.size[1] * k))), Image.LANCZOS)
    layer = Image.new("RGBA", (S, S), (0, 0, 0, 0))
    layer.paste(a, (int(c - a.size[0] / 2), int(c - a.size[1] / 2)), a)
    layer.putalpha(ImageChops.multiply(layer.getchannel("A"), disc))
    return Image.alpha_composite(out, layer)


def masters(art_path, face):
    src = Image.open(art_path).convert("RGBA")
    BRAND.mkdir(exist_ok=True)
    if pathlib.Path(art_path).resolve() != SOURCE.resolve():
        src.save(SOURCE)
    art = sticker(cutout(src))
    badge(art).save(FULL)
    x, y, s = face
    badge(sticker(cutout(src.crop((x, y, x + s, y + s))))).save(SMALL)


def icon(px):
    return Image.open(SMALL if px <= 48 else FULL).convert("RGBA").resize((px, px), Image.LANCZOS)


def on_plate(px, scale):
    """The badge at `scale` of a navy square: for icons something else masks (PWA maskable, iOS)."""
    plate = Image.new("RGBA", (px, px), NAVY + (255,))
    b = icon(int(px * scale)); o = (px - b.size[0]) // 2
    plate.paste(b, (o, o), b)
    return plate


def outputs():
    icon(64).save(ROOT / "static/favicon.png")
    icon(192).save(ROOT / "static/icon-192.png")
    icon(512).save(ROOT / "static/icon-512.png")
    on_plate(192, 0.8).save(ROOT / "static/icon-192-maskable.png")     # the maskable safe zone is the inner 80%
    on_plate(512, 0.8).save(ROOT / "static/icon-512-maskable.png")
    on_plate(180, 0.92).convert("RGB").save(ROOT / "static/apple-touch-icon.png")   # iOS rounds its own corners
    icon(512).save(ROOT / "desktop/icon.png")
    for d, k in DENS.items():
        icon(int(48 * k)).save(RES / f"mipmap-{d}/ic_launcher.png")
        icon(int(48 * k)).save(RES / f"mipmap-{d}/ic_launcher_round.png")
        # Adaptive foreground: 108dp, of which the launcher shows the middle 72dp; the badge fills 66dp of it.
        side = int(108 * k)
        fg = Image.new("RGBA", (side, side), (0, 0, 0, 0))
        b = icon(int(66 * k)); o = (side - b.size[0]) // 2
        fg.paste(b, (o, o), b)
        fg.save(RES / f"mipmap-{d}/ic_launcher_foreground.png")
    p = RES / "values/ic_launcher_background.xml"
    p.write_text(re.sub(r"#[0-9A-Fa-f]{6}", "#%02X%02X%02X" % NAVY, p.read_text(), count=1))


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--art")
    ap.add_argument("--face", help="X,Y,SIZE of the square holding her face, in drawing pixels")
    a = ap.parse_args()
    if a.art:
        masters(a.art, tuple(int(v) for v in a.face.split(",")))
    outputs()
