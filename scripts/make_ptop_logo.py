#!/usr/bin/env python3
"""Rebuild ptop's LOGO from the app icon's drawing (branding/posterchan-art-source.png, cut out exactly as
scripts/make_app_icon.py cuts it -- the icon itself is a round badge now, and keying its corner would key out her
black hoodie and keep the badge): PosterChan from the hood to the waist, a neon rim (cyan -> magenta) so the black hoodie reads on a dark terminal.
Prints the Python tuple to paste over LOGO in os/overlay/app-misc/posterchanos-shell/files/ptop."""
import importlib.util
import os
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEIGHT = 36            # pixels = 18 terminal rows of half blocks
CROP = 0.55            # of the figure's height: hood to waist
CYAN, MAGENTA = (0, 240, 255), (255, 43, 214)


def _icon_maker():
    spec = importlib.util.spec_from_file_location("make_app_icon", os.path.join(ROOT, "scripts", "make_app_icon.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def build(height=HEIGHT):
    maker = _icon_maker()
    im = maker.cutout(Image.open(maker.SOURCE))
    x0, y0, x1, y1 = im.getchannel("A").getbbox()
    im = im.crop((x0, y0, x1, y0 + int((y1 - y0) * CROP)))
    w = round(im.width * height / im.height)
    small = im.resize((w, height), Image.LANCZOS)
    W, H = w + 2, height + 2
    solid = [[False] * W for _ in range(H)]
    for y in range(height):
        for x in range(w):
            solid[y + 1][x + 1] = small.getpixel((x, y))[3] > 110
    rows = []
    for y in range(H):
        row = []
        for x in range(W):
            if solid[y][x]:
                row.append("%02x%02x%02x" % small.getpixel((x - 1, y - 1))[:3])
            elif any(0 <= x + dx < W and 0 <= y + dy < H and solid[y + dy][x + dx]
                     for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1))):
                t = y / (H - 1)
                row.append("%02x%02x%02x" % tuple(round(a + (b - a) * t) for a, b in zip(CYAN, MAGENTA)))
            else:
                row.append("......")
        rows.append("".join(row))
    if len(rows) % 2:
        rows.append("......" * W)
    return rows


if __name__ == "__main__":
    rows = build(int(sys.argv[1]) if len(sys.argv) > 1 else HEIGHT)
    print("LOGO = (")
    for r in rows:
        print('    "%s",' % r)
    print(")")
