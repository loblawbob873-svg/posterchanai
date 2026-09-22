#!/usr/bin/env python3
"""Render the PosterChanOS desktop/OS wallpaper — the mascot's desk, full-bleed at 4K.

The windowed desktop (static/js/client/os.js) and the PosterChanOS session (which IS that desktop in
--shell) share ONE default wallpaper: static/os-wallpaper-bg.webp. This draws it at 3840x2160 so it
is crisp on a 4K monitor and downscales cleanly to 1080p — background-size:cover in client.css
handles every aspect ratio. Regenerate:

    venv-unified/bin/python os/plymouth/generate_wallpaper.py

WHAT THIS DOES THAT A COPY OF THE ARTWORK WOULD NOT: os.js lays desktop icons out from the TOP LEFT
and their labels are white with a shadow, and this picture's top left is a paper poster and a lit
lantern — measured on the source, that corner means 45/255 and peaks at 254, i.e. white text on
white paper. So the left edge is darkened, but NOT flatly: the wash is weighted by each pixel's own
brightness (`ICON_GAMMA`), so it takes the glare off the poster and the lantern glass while the
dark foliage, the sleeping cat and the books underneath keep their detail. Flat dimming reads as a
grey panel taped over a third of the picture; this reads as the light falling off.

`.os-desk::before` in client.css darkens the same corner again at display time. The two are partners
and both are needed — the CSS scrim alone was never enough for a bright picture, and baking the
whole thing in would dim the wallpaper for a user who has replaced the icons' home (.os-desk.has-bg
drops the scrim but keeps whatever image it is given).

Keep any future art's busy half on the RIGHT, and check tests/test_os_wallpaper.py still passes —
it measures the icon corner, the picture's survival on the right, and that the wash stayed local.
"""
import base64
import io
import math
import os
import re
from pathlib import Path

from PIL import Image, ImageEnhance

ROOT = Path(__file__).resolve().parents[2]
# The artwork itself, kept beside this script rather than under posterchanos/ — publish_overlay.sh
# copies that THEME directory into the initramfs, and a megapixel picture has no business in a boot image.
ART = Path(os.environ.get("PC_WALLPAPER_ART") or Path(__file__).resolve().parent / "wallpaper-art.webp")
OUT = Path(os.environ.get("PC_WALLPAPER_OUT") or ROOT / "static/os-wallpaper-bg.webp")
W, H = 3840, 2160            # 4K, 16:9; cover-scaled to any monitor
QUALITY = 72                 # a 4K picture behind windows, under a scrim: 88 cost 200 KB for nothing
LQIP_W = 40                  # the instant placeholder baked into client.css (~400 bytes)
CSS = Path(os.environ.get("PC_WALLPAPER_CSS") or ROOT / "static/css/client.css")
ICON_W = 0.42                # the wash reaches this far across before it is gone
ICON_K = 0.85                # how much of a bright pixel it takes at the very edge
ICON_GAMMA = 0.45            # <1 → dark pixels are left alone, highlights take the wash


def _art():
    if not ART.exists():
        raise SystemExit(f"wallpaper art missing: {ART}")
    return Image.open(ART).convert("RGB")


def cover(art):
    """Fill 3840x2160 the way `background-size:cover` would — scale up, crop the overflow."""
    s = max(W / art.width, H / art.height)
    big = art.resize((max(W, math.ceil(art.width * s)), max(H, math.ceil(art.height * s))), Image.LANCZOS)
    x, y = (big.width - W) // 2, (big.height - H) // 2
    return big.crop((x, y, x + W, y + H))


def icon_wash(img):
    """Take the glare off the icon side, in proportion to how bright each pixel is."""
    px = img.load()
    lut = [(v / 255.0) ** ICON_GAMMA for v in range(256)]
    # Resolution-independent and cheap: one row profile, applied per column.
    reach = [max(0.0, 1.0 - (x / (W * ICON_W))) ** 1.35 for x in range(W)]
    for y in range(H):
        drop = 1.0 - 0.18 * (y / H)          # icons flow downwards; ease off towards the taskbar
        for x in range(int(W * ICON_W)):
            k = reach[x] * drop
            if k <= 0.0:
                break
            r, g, b = px[x, y]
            f = 1.0 - ICON_K * k * lut[(r * 299 + g * 587 + b * 114) // 1000]
            px[x, y] = (int(r * f), int(g * f), int(b * f))
    return img


def vignette(img):
    """Corners pulled down — it holds the eye on the desk and keeps the taskbar edge quiet."""
    v = Image.new("L", (W // 8, H // 8), 0)
    vp = v.load()
    for y in range(v.height):
        for x in range(v.width):
            d = math.hypot((x / v.width - 0.5) * 1.05, (y / v.height - 0.5) * 1.25)
            vp[x, y] = int(255 * min(1.0, max(0.0, (d - 0.42) / 0.60)) ** 1.6 * 0.55)
    dark = Image.new("RGB", (W, H), (0, 0, 0))
    return Image.composite(dark, img, v.resize((W, H), Image.LANCZOS)).convert("RGB")


def lqip(img):
    """A 40px-wide copy of the wallpaper as a data: URI — the thing that paints IMMEDIATELY.

    "The background image takes a while to load." It is a 4K photograph: even at 380 KB it arrives
    well after the page, and until it does the desktop is a flat near-black rectangle that looks
    like a failure. This is ~400 bytes, it travels INSIDE client.css (so it costs no request and
    cannot arrive late), and the browser scales it to fill the screen — a blurred wash of the right
    picture in the right colours, replaced by the real one the moment it lands. Nothing in the app
    has to know it happened.

    It is generated here, from the same image, so it cannot drift into a wash of the OLD wallpaper,
    which would be a lie that only shows up for the half-second nobody is looking."""
    small = img.resize((LQIP_W, round(LQIP_W * H / W)), Image.LANCZOS)
    buf = io.BytesIO()
    small.save(buf, "WEBP", quality=72, method=6)
    return "data:image/webp;base64," + base64.b64encode(buf.getvalue()).decode("ascii")


def _write_lqip(uri):
    """Replace the placeholder inside client.css's `.os-desk` rule, between its own markers."""
    if not CSS.exists():
        return
    css = CSS.read_text(encoding="utf-8")
    new, n = re.subn(r"(/\* pc-lqip \*/url\(')[^']*('\))", lambda m: m.group(1) + uri + m.group(2), css)
    if not n:
        raise SystemExit("client.css has no `/* pc-lqip */url('…')` placeholder to fill")
    if new != css:
        CSS.write_text(new, encoding="utf-8")
        print("wrote", CSS, f"lqip {len(uri)} chars")


def main():
    img = vignette(icon_wash(cover(_art())))
    img = ImageEnhance.Color(img).enhance(1.04)      # the crop + wash cost a little of the neon
    img.save(OUT, "WEBP", quality=QUALITY, method=6)
    print("wrote", OUT, img.size, f"{OUT.stat().st_size // 1024} KiB")
    # Only the real render owns the stylesheet: a test rendering into a temp dir must not touch the
    # working tree (the checks run concurrently against a live deployment).
    if OUT == Path(ROOT / "static/os-wallpaper-bg.webp") or os.environ.get("PC_WALLPAPER_CSS"):
        _write_lqip(lqip(img))


if __name__ == "__main__":
    main()
