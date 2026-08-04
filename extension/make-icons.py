#!/usr/bin/env python3
"""Generate the extension's icons.

Committed as a script rather than as PNGs: a binary in a diff is a binary nobody reviews, and these
are four rectangles and a keyhole. Run by build.sh when the icons are missing.
"""
import os

from PIL import Image, ImageDraw

HERE = os.path.dirname(os.path.abspath(__file__))
BG = (20, 18, 32, 255)
KEY = (143, 210, 255, 255)


def icon(size):
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    r = size // 5
    d.rounded_rectangle([0, 0, size - 1, size - 1], radius=r, fill=BG,
                        outline=(143, 210, 255, 90), width=max(1, size // 32))
    # A keyhole: circle over a tapered stem. Recognisable at 48px, which is the only size that matters.
    cx, cy = size / 2, size * 0.42
    rad = size * 0.16
    d.ellipse([cx - rad, cy - rad, cx + rad, cy + rad], fill=KEY)
    w = size * 0.09
    d.polygon([(cx - w, cy + rad * 0.4), (cx + w, cy + rad * 0.4),
               (cx + w * 1.5, size * 0.76), (cx - w * 1.5, size * 0.76)], fill=KEY)
    return img


if __name__ == "__main__":
    os.makedirs(os.path.join(HERE, "icons"), exist_ok=True)
    for s in (48, 96):
        icon(s).save(os.path.join(HERE, "icons", f"icon-{s}.png"))
    print("wrote icons/icon-48.png icons/icon-96.png")
