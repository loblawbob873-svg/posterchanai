#!/usr/bin/env python3
"""Render the PosterChanOS desktop/OS wallpaper — the boot-splash look as a full-bleed 4K picture.

The windowed desktop (static/js/client/os.js) and the PosterChanOS session (which IS that desktop in
--shell) share ONE default wallpaper: static/os-wallpaper-bg.webp. This draws it at 3840x2160 so it is
crisp on a 4K monitor and downscales cleanly to 1080p — background-size:cover in client.css handles
every aspect ratio. It reuses the boot splash's palette, grid and mascot ring (os/plymouth/
generate_theme.py) so the OS looks the same from power-on to desktop. Regenerate:

    venv-unified/bin/python os/plymouth/generate_wallpaper.py
"""
import math
import os
import sys
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent))
import generate_theme as T   # palette + glow + font + AVATAR_SRC + ORBITRON (import-safe)

ROOT = Path(__file__).resolve().parents[2]
OUT = Path(os.environ.get("PC_WALLPAPER_OUT") or ROOT / "static/os-wallpaper-bg.webp")
W, H = 3840, 2160            # 4K, 16:9; cover-scaled to any monitor


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


def grid():
    """The perspective floor grid, drawn crisp at full 4K (not upscaled)."""
    g = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(g)
    horizon = int(H * 0.70)
    for i in range(1, 15):
        t = (i / 14) ** 2.2
        y = horizon + int((H - horizon) * t)
        d.line([(0, y), (W, y)], fill=T.NEON + (int(18 + 40 * t),), width=2)
    vx = W // 2
    for i in range(-18, 19):
        d.line([(vx + i * 24, horizon), (vx + i * 300, H)], fill=T.NEON + (34,), width=2)
    fade = Image.new("L", (1, H), 0)
    for y in range(H):
        fade.putpixel((0, y), 0 if y < horizon else int(255 * min(1.0, (y - horizon) / (H - horizon) * 1.4)))
    g.putalpha(ImageChops.multiply(g.getchannel("A"), fade.resize((W, H))))
    return T.glow(g, 5)


def mascot(size=620, pad=110):
    """The mascot's face in the neon ring — same construction as the splash, rendered large for 4K."""
    src = Image.open(T.AVATAR_SRC).convert("RGBA")
    face = src.crop((8, 20, 248, 260)).resize((size, size), Image.LANCZOS)
    face = Image.alpha_composite(Image.new("RGBA", (size, size), T.BG2 + (255,)), face)
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    face.putalpha(mask.resize((size, size), Image.LANCZOS))
    total = size + pad * 2
    ring = Image.new("RGBA", (total, total), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring)
    rd.ellipse((pad - 16, pad - 16, pad + size + 15, pad + size + 15), outline=T.NEON + (255,), width=10)
    rd.arc((pad - 44, pad - 44, pad + size + 43, pad + size + 43), 200, 320, fill=T.NEON2 + (230,), width=8)
    rd.arc((pad - 44, pad - 44, pad + size + 43, pad + size + 43), 20, 140, fill=T.NEON2 + (230,), width=8)
    out = T.glow(ring, 26, 1.6)
    out.alpha_composite(face, (pad, pad))
    return out


def wordmark():
    logo = ROOT / "os/plymouth/posterchanos/logo.png"
    if not logo.exists():
        return None
    im = Image.open(logo).convert("RGBA")
    scale = 1100 / im.width
    return im.resize((int(im.width * scale), int(im.height * scale)), Image.LANCZOS)


def main():
    img = Image.alpha_composite(gradient().convert("RGBA"), grid())
    m = mascot()
    mx = (W - m.width) // 2
    my = int(H * 0.34) - m.height // 2
    img.alpha_composite(m, (mx, my))
    wm = wordmark()
    if wm is not None:
        img.alpha_composite(wm, ((W - wm.width) // 2, my + m.height + 40))
    img.convert("RGB").save(OUT, "WEBP", quality=90, method=6)
    print("wrote", OUT, img.size, f"{OUT.stat().st_size // 1024} KiB")


if __name__ == "__main__":
    main()
