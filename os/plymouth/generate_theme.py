#!/usr/bin/env python3
"""Draw the PosterChanOS boot splash's images.

Run from the repository root with the app venv (its Pillow reads the client's woff2 fonts):

    venv-unified/bin/python os/plymouth/generate_theme.py

WHY EVERY WORD ON THE PASSWORD SCREEN IS A PICTURE. A Plymouth script theme draws text through
`Image.Text`, which needs a label plugin (label.so / label-freetype.so) and a font. Plymouth 22.02 —
the amd64-stable version every PosterChanOS machine runs — does NOT put either into the initramfs:
MEASURED with `dracut -m "base plymouth"` + lsinitrd, the image carries script.so and all 41 theme
files and no label plugin, no font, no pango. So inside the initramfs, where the LUKS passphrase is
asked for, every `Image.Text` came back empty: the spinner was hidden for the prompt and nothing was
drawn in its place. Reported as "I had to press Esc when the laptop booted to even know that it was
waiting for a password". Pictures need nothing but script.so, which is always there.

Deterministic: same inputs, same bytes — tests/test_plymouth_theme.py re-runs this and compares.
"""
from __future__ import annotations

import math
import os
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

ROOT = Path(__file__).resolve().parents[2]
# PC_PLYMOUTH_OUT lets the test regenerate into a scratch directory and compare with the committed set.
OUT = Path(os.environ.get("PC_PLYMOUTH_OUT") or ROOT / "os/plymouth/posterchanos")
AVATAR_SRC = ROOT / "static/posterchan-relay.png"      # the client's own mark (app.js LOGO)
ORBITRON = ROOT / "static/fonts/orbitron.woff2"
INTER = ROOT / "static/fonts/inter.woff2"

# The client's cyberpunk palette (static/css/client.css :root).
BG = (10, 10, 15)          # --bg
BG2 = (18, 18, 26)         # --bg2
NEON = (60, 232, 255)      # --neon / --cyan
NEON2 = (255, 92, 240)     # --neon2
DANGER = (255, 107, 139)   # --danger
TEXT = (237, 238, 250)     # --text
MUTED = (159, 161, 198)    # --muted

# Reference canvas. The script scales every image by min(W/1920, H/1080) once at start-up.
W, H = 1920, 1080


def font(path, size, weight=None):
    f = ImageFont.truetype(str(path), size)
    if weight is not None:
        try:
            f.set_variation_by_axes([weight])
        except (OSError, AttributeError):
            pass                                          # a static build of the font: fine as is
    return f


def glow(layer, radius, strength=1.0):
    """A blurred copy of `layer` under itself — the neon look, without any per-frame work."""
    blurred = layer.filter(ImageFilter.GaussianBlur(radius))
    if strength != 1.0:
        a = blurred.getchannel("A").point(lambda v: min(255, int(v * strength)))
        blurred.putalpha(a)
    return Image.alpha_composite(blurred, layer)


def save(img, name):
    img.save(OUT / name, optimize=True)


def background():
    img = Image.new("RGB", (W, H), BG)
    px = img.load()
    cx, cy = W / 2, H * 0.40
    for y in range(H):
        for x in range(0, W):
            d = math.hypot((x - cx) / W, (y - cy) / H)
            k = max(0.0, 1.0 - d * 1.9)                   # cyan wash behind the avatar
            m = max(0.0, 1.0 - math.hypot(x / W - 1.0, y / H - 1.0) * 2.2)   # magenta corner
            r = BG[0] + int(8 * k + 22 * m)
            g = BG[1] + int(26 * k + 4 * m)
            b = BG[2] + int(34 * k + 24 * m)
            if y % 3 == 0:                                # scanlines, barely there
                r, g, b = int(r * 0.9), int(g * 0.9), int(b * 0.9)
            px[x, y] = (r, g, b)
    # Perspective grid on the floor: the one unmistakably "cyberpunk" element, kept dim.
    grid = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(grid)
    horizon = int(H * 0.70)
    for i in range(1, 15):
        t = (i / 14) ** 2.2
        y = horizon + int((H - horizon) * t)
        d.line([(0, y), (W, y)], fill=NEON + (int(18 + 40 * t),), width=1)
    vx = W // 2
    for i in range(-18, 19):
        x = vx + i * 150
        d.line([(vx + i * 12, horizon), (x, H)], fill=NEON + (34,), width=1)
    fade = Image.new("L", (1, H), 0)
    for y in range(H):
        fade.putpixel((0, y), 0 if y < horizon else int(255 * min(1.0, (y - horizon) / (H - horizon) * 1.4)))
    grid.putalpha(ImageChops.multiply(grid.getchannel("A"), fade.resize((W, H))))
    img = Image.alpha_composite(img.convert("RGBA"), glow(grid, 3)).convert("RGB")
    save(img, "background.png")


def avatar():
    """The mascot's face in a neon ring."""
    src = Image.open(AVATAR_SRC).convert("RGBA")
    size, pad = 300, 60
    # The head and shoulders of the 256x384 portrait.
    face = src.crop((8, 20, 248, 260)).resize((size, size), Image.LANCZOS)
    backing = Image.new("RGBA", (size, size), BG2 + (255,))
    face = Image.alpha_composite(backing, face)
    mask = Image.new("L", (size * 4, size * 4), 0)
    ImageDraw.Draw(mask).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=255)
    face.putalpha(mask.resize((size, size), Image.LANCZOS))
    total = size + pad * 2
    out = Image.new("RGBA", (total, total), (0, 0, 0, 0))
    ring = Image.new("RGBA", (total, total), (0, 0, 0, 0))
    rd = ImageDraw.Draw(ring)
    rd.ellipse((pad - 8, pad - 8, pad + size + 7, pad + size + 7), outline=NEON + (255,), width=5)
    rd.arc((pad - 22, pad - 22, pad + size + 21, pad + size + 21), 200, 320, fill=NEON2 + (230,), width=4)
    rd.arc((pad - 22, pad - 22, pad + size + 21, pad + size + 21), 20, 140, fill=NEON2 + (230,), width=4)
    out = Image.alpha_composite(out, glow(ring, 14, 1.6))
    out.alpha_composite(face, (pad, pad))
    save(out, "avatar.png")


def text_image(text, fnt, color, name, glow_radius=6, tracking=0):
    probe = ImageDraw.Draw(Image.new("RGBA", (1, 1)))
    widths = [probe.textlength(ch, font=fnt) for ch in text]
    width = int(sum(widths) + tracking * max(0, len(text) - 1)) + 2 * glow_radius + 8
    asc, desc = fnt.getmetrics()
    height = asc + desc + 2 * glow_radius + 4
    layer = Image.new("RGBA", (width, height), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    x = glow_radius + 4
    for ch, w in zip(text, widths):
        d.text((x, glow_radius + 2), ch, font=fnt, fill=color + (255,))
        x += w + tracking
    save(glow(layer, glow_radius, 0.9) if glow_radius else layer, name)


def panel():
    """The glass card the prompt sits on."""
    w, h, r, pad = 720, 300, 22, 30
    img = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    edge = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle((pad, pad, pad + w - 1, pad + h - 1), r,
                                           outline=NEON + (150,), width=2)
    body = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(body).rounded_rectangle((pad, pad, pad + w - 1, pad + h - 1), r,
                                           fill=BG2 + (215,))
    img = Image.alpha_composite(img, body)
    img = Image.alpha_composite(img, glow(edge, 12, 1.2))
    # A magenta accent along the top edge, like the client's focused cards.
    accent = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(accent).line((pad + r + 30, pad + 1, pad + w - r - 30, pad + 1), fill=NEON2 + (255,), width=3)
    save(Image.alpha_composite(img, glow(accent, 6)), "panel.png")


def field(name, color, alpha):
    w, h, r, pad = 600, 66, 14, 16
    img = Image.new("RGBA", (w + pad * 2, h + pad * 2), (0, 0, 0, 0))
    ImageDraw.Draw(img).rounded_rectangle((pad, pad, pad + w - 1, pad + h - 1), r, fill=BG + (240,))
    edge = Image.new("RGBA", img.size, (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle((pad, pad, pad + w - 1, pad + h - 1), r,
                                           outline=color + (alpha,), width=3)
    save(Image.alpha_composite(img, glow(edge, 8, 1.3)), name)


def lock():
    s = 4                                                  # draw big, downsample: smooth curves
    size = 40
    img = Image.new("RGBA", (size * s, size * s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.arc((11 * s, 3 * s, 29 * s, 23 * s), 180, 360, fill=NEON + (255,), width=3 * s)
    d.line((11 * s + 1, 13 * s, 11 * s + 1, 19 * s), fill=NEON + (255,), width=3 * s)
    d.line((29 * s - 2, 13 * s, 29 * s - 2, 19 * s), fill=NEON + (255,), width=3 * s)
    d.rounded_rectangle((6 * s, 17 * s, 34 * s, 37 * s), 4 * s, fill=NEON + (255,))
    d.ellipse((18 * s, 23 * s, 22 * s, 27 * s), fill=BG + (255,))
    d.rectangle((19 * s, 26 * s, 21 * s, 32 * s), fill=BG + (255,))
    img = img.resize((size, size), Image.LANCZOS)
    pad = 8
    out = Image.new("RGBA", (size + pad * 2, size + pad * 2), (0, 0, 0, 0))
    out.alpha_composite(img, (pad, pad))
    save(glow(out, 5), "lock.png")


def dot():
    size, pad = 14, 6
    img = Image.new("RGBA", (size * 4, size * 4), (0, 0, 0, 0))
    ImageDraw.Draw(img).ellipse((0, 0, size * 4 - 1, size * 4 - 1), fill=NEON + (255,))
    img = img.resize((size, size), Image.LANCZOS)
    out = Image.new("RGBA", (size + pad * 2, size + pad * 2), (0, 0, 0, 0))
    out.alpha_composite(img, (pad, pad))
    save(glow(out, 4, 1.4), "dot.png")


def main():
    OUT.mkdir(parents=True, exist_ok=True)
    background()
    avatar()
    panel()
    field("field.png", NEON, 230)
    field("field-error.png", DANGER, 240)
    lock()
    dot()
    title = font(ORBITRON, 40, 700)
    body = font(INTER, 22, 450)
    small = font(INTER, 17, 400)
    text_image("UNLOCK DISK", title, TEXT, "prompt-title.png", glow_radius=8, tracking=6)
    text_image("Enter the passphrase for your encrypted disk", body, MUTED, "prompt-hint.png", glow_radius=0)
    text_image("Wrong passphrase — try again", body, DANGER, "prompt-error.png", glow_radius=5)
    text_image("Press Esc for the boot log", small, MUTED, "prompt-esc.png", glow_radius=0)
    text_image("UNLOCKING", font(ORBITRON, 24, 600), NEON, "unlocking.png", glow_radius=6, tracking=4)


if __name__ == "__main__":
    main()
