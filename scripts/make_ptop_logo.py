#!/usr/bin/env python3
"""Rebuild ptop's LOGO from the app icon (static/icon-512.png): PosterChan from the hood to the waist,
background keyed out, a neon rim (cyan -> magenta) so the black hoodie reads on a dark terminal.
Prints the Python tuple to paste over LOGO in os/overlay/app-misc/posterchanos-shell/files/ptop."""
import os
import sys

from PIL import Image

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
HEIGHT = 36            # pixels = 18 terminal rows of half blocks
CROP = 0.55            # of the figure's height: hood to waist
CYAN, MAGENTA = (0, 240, 255), (255, 43, 214)


def build(height=HEIGHT):
    im = Image.open(os.path.join(ROOT, "static", "icon-512.png")).convert("RGBA")
    bg = im.getpixel((2, 2))[:3]
    px = im.load()
    for y in range(im.height):
        for x in range(im.width):
            r, g, b, _ = px[x, y]
            if abs(r - bg[0]) + abs(g - bg[1]) + abs(b - bg[2]) < 18:
                px[x, y] = (r, g, b, 0)
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
