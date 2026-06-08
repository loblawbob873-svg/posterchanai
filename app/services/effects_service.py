"""Creative image effects — the "Effects" group: meme captions, the dildo / poo
scatter gags, the BLACKED wordmark and the KOSHER seal.

Split out of media_service so the byte-level transforms (compress/clip/convert/PDF)
stay separate from these Pillow-drawn novelty overlays. All three expose the same
``(output_files, summary)`` shape as the media_service ``*_attachments`` processors,
so the web UI, Telegram, Matrix and the fedi bots deliver them through one path. The
dildo/poo tiles are drawn entirely in Pillow (no shipped image assets), reusing the
``_shade`` / ``_gradient_*`` shading primitives below.
"""

import io
import logging
import os
from pathlib import Path
from typing import List, Tuple

from app.services.media_service import OutputFile, _human_size, is_image

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Meme text (outlined white caption on the lower half of an image)
# ---------------------------------------------------------------------------

# Ordered candidate font files: a heavy/bold face reads best for meme captions.
# Falls back across distros; the last resort is Pillow's bundled default.
_MEME_FONT_CANDIDATES = [
    "/usr/share/fonts/impact/impact.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Impact.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/liberation-fonts/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
]


def _load_meme_font(size: int):
    """Load a bold TTF at `size`, falling back to Pillow's default."""
    from PIL import ImageFont
    for path in _MEME_FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    # Last resort: scalable default (Pillow >= 10 supports a size arg).
    try:
        return ImageFont.load_default(size=size)
    except TypeError:
        return ImageFont.load_default()


def _wrap_text_to_width(draw, text: str, font, max_width: int) -> List[str]:
    """Greedy word-wrap `text` so each line fits within `max_width` pixels.

    A single word longer than the line is hard-broken character-by-character so
    it can never overflow the image.
    """
    def width_of(s: str) -> int:
        return int(draw.textbbox((0, 0), s, font=font)[2])

    lines: List[str] = []
    for paragraph in text.split("\n"):
        words = paragraph.split()
        if not words:
            lines.append("")
            continue
        current = ""
        for word in words:
            candidate = f"{current} {word}".strip()
            if width_of(candidate) <= max_width or not current:
                # Hard-break an over-long single word.
                if not current and width_of(word) > max_width:
                    piece = ""
                    for ch in word:
                        if width_of(piece + ch) <= max_width or not piece:
                            piece += ch
                        else:
                            lines.append(piece)
                            piece = ch
                    current = piece
                else:
                    current = candidate
            else:
                lines.append(current)
                current = word
        if current:
            lines.append(current)
    return lines


def add_meme_text(data: bytes, text: str) -> bytes:
    """Draw outlined white meme text across the lower half of an image.

    The font size auto-scales: it starts large and shrinks until the wrapped text
    fits within the image width and the bottom half's height. Returns JPEG bytes.
    """
    from PIL import Image, ImageDraw, ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except Exception:
        pass

    text = (text or "").strip().upper()  # uppercase = classic meme look
    if not text:
        raise ValueError("no caption text given")

    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img)
        # Flatten transparency/palette onto white (a bare convert("RGB") would turn
        # transparent areas black), matching compress_image's handling.
        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            background.paste(rgba, mask=rgba.split()[-1])
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        W, H = img.size
        draw = ImageDraw.Draw(img)
        margin = max(int(W * 0.04), 8)
        max_width = W - 2 * margin
        max_height = int(H * 0.5) - margin  # caption lives in the bottom half

        # Auto-size: from ~1/6 of the height down to a readable floor, pick the
        # largest font whose wrapped text fits the width and the bottom-half box.
        chosen_font = None
        chosen_lines: List[str] = []
        line_spacing = 0
        start = max(int(H / 6), 14)
        for size in range(start, 11, -2):
            font = _load_meme_font(size)
            lines = _wrap_text_to_width(draw, text, font, max_width)
            ascent_box = draw.textbbox((0, 0), "Ay", font=font)
            line_h = ascent_box[3] - ascent_box[1]
            spacing = max(int(line_h * 0.2), 2)
            total_h = len(lines) * line_h + (len(lines) - 1) * spacing
            widest = max((draw.textbbox((0, 0), ln, font=font)[2] for ln in lines), default=0)
            if total_h <= max_height and widest <= max_width:
                chosen_font, chosen_lines, line_spacing = font, lines, spacing
                break
        if chosen_font is None:
            # Even the floor size overflows — use it anyway (best effort).
            chosen_font = _load_meme_font(12)
            chosen_lines = _wrap_text_to_width(draw, text, chosen_font, max_width)
            ascent_box = draw.textbbox((0, 0), "Ay", font=chosen_font)
            line_spacing = max(int((ascent_box[3] - ascent_box[1]) * 0.2), 2)

        ascent_box = draw.textbbox((0, 0), "Ay", font=chosen_font)
        line_h = ascent_box[3] - ascent_box[1]
        total_h = len(chosen_lines) * line_h + (len(chosen_lines) - 1) * line_spacing
        # Anchor the block to the bottom of the image, inside the margin.
        y = H - margin - total_h
        # Outline thickness scales with font size so it stays visible when large.
        stroke = max(int(line_h * 0.08), 2)

        for line in chosen_lines:
            lw = draw.textbbox((0, 0), line, font=chosen_font)[2]
            x = (W - lw) / 2
            draw.text(
                (x, y), line, font=chosen_font, fill="white",
                stroke_width=stroke, stroke_fill="black",
            )
            y += line_h + line_spacing

        out = io.BytesIO()
        img.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue()


def meme_attachments(
    attachments: List[Tuple[str, bytes, str]],
    text: str,
) -> Tuple[List[OutputFile], str]:
    """Add outlined white meme text to the first image attachment.

    Returns (output_files, summary_text). Mirrors compress_attachments so the
    web UI, Telegram and Matrix share one delivery path.
    """
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image to caption — attach an image first."
    if not (text or "").strip():
        return [], "Add a caption: `meme <text>`."

    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_meme_text(data, text)
        out: OutputFile = {
            "filename": f"{stem}_meme.jpg",
            "data": result,
            "content_type": "image/jpeg",
        }
        summary = f"## 🖼️ Meme\n\n🖼️ {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"meme failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Shared shading primitives (used by the dildo + poo tile renderers)
# ---------------------------------------------------------------------------

def _shade(c, f: float):
    """Lighten (f>1) or darken (f<1) an RGB(A) colour, clamped, alpha forced opaque."""
    return (min(255, int(c[0] * f)), min(255, int(c[1] * f)),
            min(255, int(c[2] * f)), 255)


def _gradient_sphere(base, size: int = 64):
    """A diffuse-lit sphere (light from upper-left) as an RGBA image of `size`px.

    Rendered small once and resized by the caller — used for the glans and balls so
    they read as rounded volumes rather than flat discs.
    """
    import math
    from PIL import Image
    img = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    px = img.load()
    r = size / 2.0
    lx, ly, lz = -0.45, -0.5, 0.74  # light direction (upper-left, toward viewer)
    for y in range(size):
        for x in range(size):
            dx = (x - r + 0.5) / r
            dy = (y - r + 0.5) / r
            d2 = dx * dx + dy * dy
            if d2 > 1.0:
                continue
            nz = math.sqrt(1.0 - d2)
            shade = 0.42 + 0.9 * max(0.0, dx * lx + dy * ly + nz * lz)
            shade = min(1.4, shade)
            px[x, y] = (min(255, int(base[0] * shade)),
                        min(255, int(base[1] * shade)),
                        min(255, int(base[2] * shade)), 255)
    return img


def _gradient_cylinder(w: int, h: int, base):
    """A vertical cylinder gradient (bright stripe left-of-centre, darkening to the
    edges) as an RGB image — gives the shaft a rounded, lit look."""
    import math
    from PIL import Image
    w = max(int(w), 2)
    h = max(int(h), 2)
    strip = Image.new("RGB", (w, 1))
    px = strip.load()
    hl = 0.38  # highlight position across the width
    for x in range(w):
        t = x / (w - 1)
        shade = 0.5 + 0.78 * max(0.0, math.cos((t - hl) * math.pi))
        shade = min(1.32, shade)
        px[x, 0] = (min(255, int(base[0] * shade)),
                    min(255, int(base[1] * shade)),
                    min(255, int(base[2] * shade)))
    return strip.resize((w, h))


def _scatter_overlay(data: bytes, make_tile, count: int = 0,
                     max_rotation: float = 180.0) -> bytes:
    """Scatter randomly sized/rotated overlay tiles over an image.

    `make_tile(size)` renders one RGBA tile (e.g. `_make_dildo`/`_make_poo`);
    `count` <= 0 auto-scales with the image area; `max_rotation` bounds the random
    spin per tile (±deg). Returns JPEG bytes. Shared by the dildo and poo gags so
    the scatter/flatten/save logic lives in one place.
    """
    import random
    from PIL import Image, ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except Exception:
        pass

    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img)
        # Flatten transparency/palette onto white (matches add_meme_text) so the
        # final RGB save never turns transparent areas black.
        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            background.paste(rgba, mask=rgba.split()[-1])
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        W, H = img.size
        img = img.convert("RGBA")  # composite layer
        if count <= 0:
            count = max(14, min(60, (W * H) // 38000))
        base = min(W, H)
        lo, hi = max(int(base * 0.12), 12), max(int(base * 0.28), 24)
        for _ in range(count):
            size = random.randint(lo, hi)
            tile = make_tile(size)
            tile = tile.rotate(random.uniform(-max_rotation, max_rotation),
                               expand=True, resample=Image.BICUBIC)
            # Allow partial overhang off every edge so the scatter reaches the borders.
            x = random.randint(-tile.width // 3, max(W - tile.width * 2 // 3, 1))
            y = random.randint(-tile.height // 3, max(H - tile.height * 2 // 3, 1))
            img.alpha_composite(tile, (x, y))

        img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue()


# ---------------------------------------------------------------------------
# Dildo overlay (the "dildo" gag — scatter cartoon dildos across an image)
# ---------------------------------------------------------------------------

# A few flesh/pink/novelty tones so the scattered dildos aren't all identical.
_DILDO_COLORS = [
    (240, 200, 175, 255),  # light flesh
    (212, 162, 130, 255),  # medium flesh
    (168, 120, 95, 255),   # dark flesh
    (246, 150, 182, 255),  # pink
    (152, 92, 200, 255),   # purple
]


def _make_dildo(h: int):
    """Render one shaded, semi-anatomical dildo (pointing up) on a transparent tile.

    Pure Pillow — sphere-lit balls + glans, a cylinder-shaded shaft, a flared
    corona, urethral slit, veins and base ambient occlusion, finished with a single
    clean outer outline. Ships no image asset (the meme path is also pure Pillow).
    """
    import math
    import random
    from PIL import Image, ImageDraw, ImageFilter, ImageChops

    W = max(int(h * 0.66), 16)
    H = max(int(h * 1.18), 20)
    base = random.choice(_DILDO_COLORS)[:3]
    outline = _shade(base, 0.45)

    cx = W / 2.0
    sw = W * 0.40                       # shaft width
    ball_r = sw * 0.62
    top = H * 0.04
    base_y = H - ball_r * 1.0
    head_h = sw * 1.16                  # glans (bell head)
    head_w = sw * 1.24
    shaft_top = top + head_h * 0.5

    tile = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    # --- balls (sphere-shaded, reused for both) ---
    ball_sphere = _gradient_sphere(_shade(base, 0.95)[:3])
    bd = max(int(ball_r * 2), 2)
    bimg = ball_sphere.resize((bd, bd))
    for dx in (-sw * 0.40, sw * 0.40):
        bx = cx + dx
        tile.alpha_composite(bimg, (int(bx - ball_r), int(base_y - ball_r)))

    # --- shaft (cylinder gradient clipped to a rounded-pill mask) ---
    sh_h = max(int(base_y - shaft_top), 2)
    sh_w = max(int(sw), 2)
    cyl = _gradient_cylinder(sh_w, sh_h, base).convert("RGBA")
    smask = Image.new("L", (sh_w, sh_h), 0)
    ImageDraw.Draw(smask).rounded_rectangle([0, 0, sh_w - 1, sh_h - 1],
                                            radius=int(sw / 2), fill=255)
    cyl.putalpha(smask)
    tile.alpha_composite(cyl, (int(cx - sw / 2), int(shaft_top)))

    # --- glans (sphere-shaded, slightly pinker, squashed into a bell) ---
    pink = (min(255, base[0] + 20), max(0, base[1] - 6), min(255, base[2] + 8))
    glans = _gradient_sphere(pink).resize((max(int(head_w), 2), max(int(head_h), 2)))
    tile.alpha_composite(glans, (int(cx - head_w / 2), int(top)))

    # Silhouette of the solid body so far — confines the soft passes below.
    sil = tile.split()[-1]

    # --- ambient occlusion where the shaft meets the balls ---
    ao = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(ao).ellipse([cx - sw * 0.75, base_y - ball_r * 0.5,
                                cx + sw * 0.75, base_y + ball_r * 0.6],
                               fill=(0, 0, 0, 95))
    ao = ao.filter(ImageFilter.GaussianBlur(max(sw * 0.16, 1)))
    ao.putalpha(ImageChops.multiply(ao.split()[-1], sil))
    tile.alpha_composite(ao)

    # --- veins (two soft wavy lines down the shaft) ---
    veins = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    vd = ImageDraw.Draw(veins)
    vcol = _shade(base, 0.8)[:3] + (95,)
    for vx, ph in ((cx - sw * 0.16, 0.0), (cx + sw * 0.1, 1.7)):
        pts = []
        for i in range(9):
            yy = shaft_top + sh_h * i / 8.0
            xx = vx + math.sin(i * 0.9 + ph) * sw * 0.12
            pts.append((xx, yy))
        vd.line(pts, fill=vcol, width=max(int(sw * 0.05), 1), joint="curve")
    veins = veins.filter(ImageFilter.GaussianBlur(max(sw * 0.03, 0.6)))
    veins.putalpha(ImageChops.multiply(veins.split()[-1], sil))
    tile.alpha_composite(veins)

    # --- corona (flared rim) + urethral slit ---
    fd = ImageDraw.Draw(tile)
    fd.arc([cx - head_w / 2, top + head_h * 0.42, cx + head_w / 2, top + head_h * 1.28],
           start=18, end=162, fill=_shade(base, 0.55)[:3] + (160,),
           width=max(int(sw * 0.07), 1))
    slit_y = top + head_h * 0.16
    fd.line([(cx, slit_y), (cx, slit_y + head_h * 0.18)],
            fill=_shade(base, 0.38)[:3] + (190,), width=max(int(sw * 0.05), 1))

    # --- single clean outer outline (edge of the union silhouette) ---
    ow = max(int(W * 0.03), 1)
    binr = tile.split()[-1].point(lambda a: 255 if a > 40 else 0)
    eroded = binr.filter(ImageFilter.MinFilter(ow * 2 + 1))
    edge = ImageChops.subtract(binr, eroded)
    line_layer = Image.new("RGBA", (W, H), outline[:3] + (255,))
    tile.paste(line_layer, (0, 0), edge)

    return tile


def add_dildos(data: bytes, count: int = 0) -> bytes:
    """Scatter cartoon dildos at random positions/sizes/angles over an image.

    `count` <= 0 auto-scales with the image area. Returns JPEG bytes.
    """
    return _scatter_overlay(data, _make_dildo, count)


def dildo_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Scatter dildos over the first image attachment.

    Returns (output_files, summary_text). Mirrors meme_attachments so the web UI,
    Telegram, Matrix and the fedi bots share one delivery path.
    """
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."

    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_dildos(data)
        out: OutputFile = {
            "filename": f"{stem}_dildo.jpg",
            "data": result,
            "content_type": "image/jpeg",
        }
        summary = f"## 🍆 Dildo\n\n🍆 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"dildo failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Poo overlay (the "poo" gag — scatter realistic piles of poop across an image)
# ---------------------------------------------------------------------------

# A few brown tones so the scattered piles aren't all identical.
_POO_COLORS = [
    (110, 70, 36, 255),    # classic brown
    (92, 58, 30, 255),     # medium brown
    (74, 47, 26, 255),     # dark chocolate
    (128, 84, 44, 255),    # light brown
]


def _make_poo(h: int):
    """Render one realistic coiled stool on a transparent tile (pure Pillow).

    A tapering stack of DISTINCT sphere-shaded coils swaying side-to-side up to a
    pinched tip, finished with dark grooves between coils, surface speckle
    (texture), base ambient occlusion, and a few moist specular highlights. No
    cartoon face — aims for a believable turd. Ships no image asset (like dildo).
    """
    import math
    import random
    from PIL import Image, ImageDraw, ImageFilter, ImageChops

    W = max(int(h * 0.95), 16)
    H = max(int(h * 1.12), 18)
    base = random.choice(_POO_COLORS)[:3]
    cx = W / 2.0
    phase = random.uniform(0, math.tau)

    tile = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    # A handful of DISTINCT coils (not a smooth cone): each a wide sphere-shaded
    # bulge, stacked bottom→top with ~45% overlap so the seams read as separate
    # soft-serve rings, tapering and swaying side-to-side toward a pinched tip.
    n = 5
    base_w = W * 0.96
    coils = []  # (cx, cy, w, h) per bulge, low→high
    for i in range(n):
        t = i / (n - 1)
        w = base_w * (1 - 0.60 * t)
        hgt = w * 0.66
        cy = H * 0.84 - t * (H * 0.60)
        seg_cx = cx + math.sin(i * 1.7 + phase) * W * 0.13 * (1 - 0.5 * t)
        shade = 0.82 + 0.30 * (1 - t)
        seg = _gradient_sphere(_shade(base, shade)[:3]).resize(
            (max(int(w), 2), max(int(hgt), 2)))
        tile.alpha_composite(seg, (int(seg_cx - w / 2), int(cy - hgt / 2)))
        coils.append((seg_cx, cy, w, hgt))

    # Pinched tip — a small narrow bulge crowning the top coil.
    tcx, tcy, tw, th = coils[-1]
    tip = _gradient_sphere(_shade(base, 1.18)[:3]).resize(
        (max(int(tw * 0.42), 2), max(int(th * 0.95), 2)))
    tile.alpha_composite(tip, (int(tcx - tw * 0.21), int(tcy - th * 0.85)))

    sil = tile.split()[-1]  # silhouette — confines every soft pass below

    # --- grooves: a soft dark band where each coil tucks under the one above ---
    grv = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grv)
    for (lx, ly, lw, lh), (ux, uy, uw, uh) in zip(coils, coils[1:]):
        gy = (ly - lh / 2 + uy + uh / 2) / 2  # seam between the two bulges
        gw = min(lw, uw) * 0.5
        gx = (lx + ux) / 2
        gd.ellipse([gx - gw, gy - lh * 0.14, gx + gw, gy + lh * 0.14],
                   fill=(0, 0, 0, 130))
    grv = grv.filter(ImageFilter.GaussianBlur(max(W * 0.03, 1)))
    grv.putalpha(ImageChops.multiply(grv.split()[-1], sil))
    tile.alpha_composite(grv)

    # --- surface speckle (subtle lighter/darker flecks for matte texture) ---
    spk = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    sd = ImageDraw.Draw(spk)
    for _ in range(max((W * H) // 240, 24)):
        x, y = random.randint(0, W - 1), random.randint(0, H - 1)
        rr = random.uniform(0.6, 1.9)
        c = _shade(base, random.uniform(0.55, 1.4))[:3] + (random.randint(35, 85),)
        sd.ellipse([x - rr, y - rr, x + rr, y + rr], fill=c)
    spk = spk.filter(ImageFilter.GaussianBlur(0.5))
    spk.putalpha(ImageChops.multiply(spk.split()[-1], sil))
    tile.alpha_composite(spk)

    # --- ambient occlusion pooled where it meets the ground ---
    bx, by, bw, bh = coils[0]
    ao = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(ao).ellipse(
        [bx - bw * 0.55, by + bh * 0.1, bx + bw * 0.55, by + bh * 0.6],
        fill=(0, 0, 0, 120))
    ao = ao.filter(ImageFilter.GaussianBlur(max(bw * 0.10, 1)))
    ao.putalpha(ImageChops.multiply(ao.split()[-1], sil))
    tile.alpha_composite(ao)

    # --- moist specular highlights (a small soft sheen upper-left of each coil) ---
    hl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hl)
    for seg_cx, cy, w, hgt in coils:
        hx, hy = seg_cx - w * 0.26, cy - hgt * 0.30
        hrx, hry = w * 0.10, hgt * 0.075
        hd.ellipse([hx - hrx, hy - hry, hx + hrx, hy + hry],
                   fill=(255, 250, 238, 95))
    hl = hl.filter(ImageFilter.GaussianBlur(max(W * 0.015, 0.6)))
    hl.putalpha(ImageChops.multiply(hl.split()[-1], sil))
    tile.alpha_composite(hl)

    return tile


def add_poo(data: bytes, count: int = 0) -> bytes:
    """Scatter realistic piles of poop at random positions/sizes over an image.

    `count` <= 0 auto-scales with the image area. The spin is kept small so each
    coil stays roughly upright. Returns JPEG bytes.
    """
    return _scatter_overlay(data, _make_poo, count, max_rotation=22.0)


def poo_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Scatter poop over the first image attachment.

    Returns (output_files, summary_text). Mirrors dildo_attachments so the web UI,
    Telegram, Matrix and the fedi bots share one delivery path.
    """
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."

    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_poo(data)
        out: OutputFile = {
            "filename": f"{stem}_poo.jpg",
            "data": result,
            "content_type": "image/jpeg",
        }
        summary = f"## 💩 Poo\n\n💩 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"poo failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Cum overlay (the "cum" gag — scatter glossy off-white splatters over an image)
# ---------------------------------------------------------------------------

# A few off-white / cream tones so the scattered splatters aren't all identical.
_CUM_COLORS = [
    (246, 244, 236, 255),  # cream white
    (238, 236, 228, 255),  # off white
    (250, 249, 244, 255),  # bright white
    (240, 238, 224, 255),  # warm ivory
]


def _make_cum(h: int):
    """Render one glossy off-white splatter (the "cum" gag) on a transparent tile.

    Pure Pillow — an irregular central blob plus a few radiating strands tipped
    with droplets (and the odd satellite speck), given a soft translucent dark rim
    so the near-white body still reads on light backgrounds, plus wet specular
    highlights and slight translucency. Ships no image asset (like the poo path).
    """
    import math
    import random
    from PIL import Image, ImageDraw, ImageFilter, ImageChops

    W = max(int(h * 1.15), 18)
    H = max(int(h * 1.15), 18)
    base = random.choice(_CUM_COLORS)[:3]
    cx, cy = W * 0.5, H * 0.52
    phase = random.uniform(0, math.tau)

    # --- build the splatter SHAPE on an alpha mask (lets us rim/shade it after) ---
    mask = Image.new("L", (W, H), 0)
    md = ImageDraw.Draw(mask)
    main_r = W * 0.19

    def _dot(x, y, r):
        md.ellipse([x - r, y - r, x + r, y + r], fill=255)

    # A guaranteed-solid core so the blob never has an interior pinhole (a gap
    # would let the outer-rim pass leak inward as a dark ring).
    md.ellipse([cx - main_r, cy - main_r * 0.85, cx + main_r, cy + main_r * 0.85], fill=255)

    # Cohesive central blob: several big, tightly-overlapping ellipses (lots of
    # overlap so there are no interior gaps that would shade into dark artifacts).
    for _ in range(5):
        ox = cx + random.uniform(-1, 1) * main_r * 0.35
        oy = cy + random.uniform(-1, 1) * main_r * 0.30
        rx = main_r * random.uniform(0.8, 1.15)
        ry = main_r * random.uniform(0.7, 1.0)
        md.ellipse([ox - rx, oy - ry, ox + rx, oy + ry], fill=255)

    # Flung streaks: a tapered tail (wide at the blob, thinning out) capped by a
    # fatter droplet head — reads like fluid thrown outward, not a molecule graph.
    for i in range(random.randint(4, 6)):
        ang = phase + i * (math.tau / 5) + random.uniform(-0.4, 0.4)
        dist = main_r * random.uniform(1.4, 3.0)
        dx, dy = math.cos(ang), math.sin(ang)
        steps = 12
        for s in range(steps + 1):
            f = s / steps
            px = cx + dx * (main_r * 0.5 + f * dist)
            py = cy + dy * (main_r * 0.5 + f * dist)
            rad = main_r * (0.26 * (1 - f) ** 1.4 + 0.04)
            _dot(px, py, rad)
        # droplet head at the tip, slightly past the tail end
        hx, hy = cx + dx * (main_r * 0.5 + dist), cy + dy * (main_r * 0.5 + dist)
        _dot(hx, hy, main_r * random.uniform(0.16, 0.30))
        # an occasional small satellite fleck beyond the head
        if random.random() < 0.5:
            _dot(hx + dx * main_r * 0.7, hy + dy * main_r * 0.7,
                 main_r * random.uniform(0.06, 0.13))

    # Morphological close (dilate→erode) to seal any thin gaps between strokes,
    # then a light blur for soft edges.
    _k = max(int(W * 0.02) | 1, 3)
    mask = mask.filter(ImageFilter.MaxFilter(_k)).filter(ImageFilter.MinFilter(_k))
    sil = mask.filter(ImageFilter.GaussianBlur(max(W * 0.012, 0.7)))  # soft edges

    tile = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    # --- soft translucent dark rim just outside the shape (so white reads on white) ---
    grow = max(int(W * 0.03) | 1, 3)
    ring = ImageChops.subtract(sil.filter(ImageFilter.MaxFilter(grow)), sil)
    ring = ring.filter(ImageFilter.GaussianBlur(max(W * 0.02, 1)))
    rim = Image.new("RGBA", (W, H), (50, 50, 60, 0))
    rim.putalpha(ring.point(lambda a: int(a * 0.55)))
    tile.alpha_composite(rim)

    # --- body fill (slightly translucent for a wet look) ---
    body = Image.new("RGBA", (W, H), base + (236,))
    body.putalpha(ImageChops.multiply(body.split()[-1], sil))
    tile.alpha_composite(body)

    # --- inner edge shading (darker cream rim) for a little volume ---
    inner = ImageChops.subtract(sil, sil.filter(ImageFilter.MinFilter(grow)))
    inner = inner.filter(ImageFilter.GaussianBlur(max(W * 0.012, 0.6)))
    shade = Image.new("RGBA", (W, H), _shade(base, 0.82)[:3] + (0,))
    shade.putalpha(ImageChops.multiply(inner, sil).point(lambda a: int(a * 0.33)))
    tile.alpha_composite(shade)

    # --- wet specular highlights (a few bright spots on the blob) ---
    # Draw + blur on an ALPHA mask, then tint a uniformly-white layer with it: if
    # we blurred a coloured RGBA layer instead, its transparent (black) RGB would
    # bleed into a dark halo — very visible on a near-white body.
    hlmask = Image.new("L", (W, H), 0)
    hd = ImageDraw.Draw(hlmask)
    for _ in range(3):
        hx = cx + random.uniform(-main_r * 0.5, main_r * 0.3)
        hy = cy + random.uniform(-main_r * 0.5, main_r * 0.1)
        hr = main_r * random.uniform(0.12, 0.26)
        hd.ellipse([hx - hr, hy - hr * 0.7, hx + hr, hy + hr * 0.7], fill=235)
    hlmask = hlmask.filter(ImageFilter.GaussianBlur(max(W * 0.012, 0.6)))
    hl = Image.new("RGBA", (W, H), (255, 255, 255, 0))
    hl.putalpha(ImageChops.multiply(hlmask, sil))
    tile.alpha_composite(hl)

    return tile


def add_cum(data: bytes, count: int = 0) -> bytes:
    """Scatter glossy off-white splatters at random positions/sizes/angles over an image.

    `count` <= 0 auto-scales with the image area. Returns JPEG bytes.
    """
    return _scatter_overlay(data, _make_cum, count)


def cum_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Scatter splatters over the first image attachment.

    Returns (output_files, summary_text). Mirrors poo_attachments so the web UI,
    Telegram, Matrix and the fedi bots share one delivery path.
    """
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."

    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_cum(data)
        out: OutputFile = {
            "filename": f"{stem}_cum.jpg",
            "data": result,
            "content_type": "image/jpeg",
        }
        summary = f"## 💦 Cum\n\n💦 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"cum failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Blood overlay (the "blood" gag — scatter wet blood splatters across an image)
# ---------------------------------------------------------------------------

# A few deep-red tones so the scattered splatters aren't all identical.
_BLOOD_COLORS = [
    (140, 8, 8, 255),     # crimson
    (110, 3, 3, 255),     # dark red
    (92, 6, 6, 255),      # dried red
    (165, 16, 12, 255),   # bright arterial
]


def _make_blood(h: int):
    """Render one wet blood splatter on a transparent tile (pure Pillow).

    An irregular central pool with thin radial impact spatter (droplet-tipped
    arms) AND the signature gravity DRIPS running downward into rounded beads,
    finished with a soft dark rim, a slightly darker inner edge for depth, and a
    small wet specular highlight. Ships no image asset (like the cum path).
    """
    import math
    import random
    from PIL import Image, ImageDraw, ImageFilter, ImageChops

    W = max(int(h * 1.5), 24)
    H = max(int(h * 1.9), 32)             # roomy: fits the spray + downward drips
    base = random.choice(_BLOOD_COLORS)[:3]
    cx, cy = W * 0.5, H * 0.30            # pool sits high; drips fall below it
    main_r = W * 0.14

    mask = Image.new("L", (W, H), 0)
    md = ImageDraw.Draw(mask)

    def _dot(x, y, r):
        md.ellipse([x - r, y - r, x + r, y + r], fill=255)

    def _trail(x0, y0, x1, y1, wa, wb):
        """A smooth tapering trail from (x0,y0,width wa) to (x1,y1,width wb):
        overlapping dots spaced finer than their radius so it reads continuous."""
        seg = math.hypot(x1 - x0, y1 - y0)
        n = max(int(seg / max(min(wa, wb) * 0.5, 1.0)), 6)
        for s in range(n + 1):
            f = s / n
            _dot(x0 + (x1 - x0) * f, y0 + (y1 - y0) * f, max(wa + (wb - wa) * f, 1.0))

    # Directional, irregular pool: globs spread ALONG a random impact axis (so it's
    # elongated, not a round blob) with jagged pointed fingers around the rim — the
    # surface-tension spikes that make it read as a splat rather than balls.
    axis = random.uniform(0, math.tau)
    ax, ay = math.cos(axis), math.sin(axis)
    perpx, perpy = -ay, ax
    _dot(cx, cy, main_r * 0.5)
    for _ in range(10):
        t = random.uniform(-1.1, 1.1)            # along the impact axis
        s = random.uniform(-0.4, 0.4)            # small perpendicular jitter
        ox = cx + ax * t * main_r * 1.15 + perpx * s * main_r
        oy = cy + ay * t * main_r * 1.15 + perpy * s * main_r
        _dot(ox, oy, main_r * random.uniform(0.28, 0.58))
    # pointed rim fingers (tapering spikes sticking out of the pool edge)
    for _ in range(random.randint(8, 13)):
        a = random.uniform(0, math.tau)
        r0 = main_r * random.uniform(0.5, 0.95)
        fl = main_r * random.uniform(0.35, 1.2)
        sx, sy = cx + math.cos(a) * r0, cy + math.sin(a) * r0
        ex, ey = cx + math.cos(a) * (r0 + fl), cy + math.sin(a) * (r0 + fl)
        _trail(sx, sy, ex, ey, main_r * random.uniform(0.09, 0.16), main_r * 0.02)
        if random.random() < 0.45:
            _dot(ex, ey, main_r * random.uniform(0.04, 0.09))

    # Cast-off arms: a few thin tapering streaks at RANDOM angles (not an even
    # star), each tipped with a droplet — irregular like real impact spatter.
    for _ in range(random.randint(4, 7)):
        ang = random.uniform(0, math.tau)
        dist = main_r * random.uniform(1.2, 2.9)
        dx, dy = math.cos(ang), math.sin(ang)
        sx, sy = cx + dx * main_r * 0.5, cy + dy * main_r * 0.5
        ex, ey = cx + dx * (main_r * 0.5 + dist), cy + dy * (main_r * 0.5 + dist)
        _trail(sx, sy, ex, ey, main_r * random.uniform(0.12, 0.20), main_r * 0.03)
        _dot(ex, ey, main_r * random.uniform(0.08, 0.20))            # droplet head

    # Fine secondary droplets: a spray clustered along a random impact direction,
    # plus a few stray specks — the detail that sells it as spatter, not paint.
    spray = random.uniform(0, math.tau)
    for _ in range(random.randint(14, 28)):
        a = spray + random.uniform(-1.0, 1.0)
        d = main_r * random.uniform(1.0, 3.2)
        _dot(cx + math.cos(a) * d, cy + math.sin(a) * d,
             main_r * random.uniform(0.03, 0.13))
    for _ in range(random.randint(4, 9)):
        a = random.uniform(0, math.tau)
        d = main_r * random.uniform(0.8, 3.0)
        _dot(cx + math.cos(a) * d, cy + math.sin(a) * d,
             main_r * random.uniform(0.02, 0.07))

    # Gravity drips: a few tapering trails running DOWN from the pool, each ending
    # in a rounded bead — the detail that makes it read as blood rather than paint.
    for _ in range(random.randint(2, 4)):
        x0 = cx + random.uniform(-main_r * 0.8, main_r * 0.8)
        top = cy + main_r * 0.3
        length = H * random.uniform(0.28, 0.58)
        w0 = main_r * random.uniform(0.16, 0.30)
        drift = random.uniform(-main_r * 0.18, main_r * 0.18)        # slight lean
        _trail(x0, top, x0 + drift, top + length, w0, max(w0 * 0.4, 1.2))
        _dot(x0 + drift, top + length, w0 * random.uniform(1.0, 1.5))  # swelling bead

    sil = mask.filter(ImageFilter.GaussianBlur(max(W * 0.005, 0.5)))

    tile = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    # Soft dark rim just outside the shape → depth / separation from the photo.
    grow = max(int(W * 0.025) | 1, 3)
    ring = ImageChops.subtract(sil.filter(ImageFilter.MaxFilter(grow)), sil)
    ring = ring.filter(ImageFilter.GaussianBlur(max(W * 0.02, 1)))
    rim = Image.new("RGBA", (W, H), (20, 0, 0, 0))
    rim.putalpha(ring.point(lambda a: int(a * 0.6)))
    tile.alpha_composite(rim)

    # Body fill (nearly opaque — wet blood).
    body = Image.new("RGBA", (W, H), base + (250,))
    body.putalpha(ImageChops.multiply(body.split()[-1], sil))
    tile.alpha_composite(body)

    # Darker inner edge for a pooled, glossy look.
    inner = ImageChops.subtract(sil, sil.filter(ImageFilter.MinFilter(grow)))
    inner = inner.filter(ImageFilter.GaussianBlur(max(W * 0.012, 0.6)))
    shade = Image.new("RGBA", (W, H), _shade(base, 0.55)[:3] + (0,))
    shade.putalpha(ImageChops.multiply(inner, sil).point(lambda a: int(a * 0.5)))
    tile.alpha_composite(shade)

    # Wet specular highlight: one soft vertical-ish sheen on the upper-left of the
    # pool (vertical so it doesn't read like a pair of eyes), via the alpha-mask
    # method to avoid a dark blur halo. A tiny offset speck adds wetness.
    hlmask = Image.new("L", (W, H), 0)
    hd = ImageDraw.Draw(hlmask)
    hx, hy = cx - main_r * 0.30, cy - main_r * 0.28
    hd.ellipse([hx - main_r * 0.11, hy - main_r * 0.22,
                hx + main_r * 0.11, hy + main_r * 0.22], fill=200)
    hd.ellipse([cx + main_r * 0.12, cy - main_r * 0.02,
                cx + main_r * 0.20, cy + main_r * 0.06], fill=120)
    hlmask = hlmask.filter(ImageFilter.GaussianBlur(max(W * 0.012, 0.6)))
    hl = Image.new("RGBA", (W, H), (255, 235, 235, 0))
    hl.putalpha(ImageChops.multiply(hlmask, sil))
    tile.alpha_composite(hl)

    return tile


def add_blood(data: bytes, count: int = 0) -> bytes:
    """Scatter wet blood splatters over an image.

    `count` <= 0 auto-scales with the image area. Spin is kept small so the drips
    keep running downward. Returns JPEG bytes.
    """
    return _scatter_overlay(data, _make_blood, count, max_rotation=10.0)


def blood_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Scatter blood over the first image attachment.

    Returns (output_files, summary_text). Mirrors cum_attachments so the web UI,
    Telegram, Matrix and the fedi bots share one delivery path.
    """
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."

    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_blood(data)
        out: OutputFile = {
            "filename": f"{stem}_blood.jpg",
            "data": result,
            "content_type": "image/jpeg",
        }
        summary = f"## 🩸 Blood\n\n🩸 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"blood failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Bullethole overlay (the "bullethole" gag — punch cracked holes into an image)
# ---------------------------------------------------------------------------

def _make_bullethole(h: int):
    """Render one bullet hole on a transparent tile (pure Pillow).

    A small IRREGULAR punched hole with a torn rim, surrounded by dense BRANCHING
    radial cracks and jagged concentric cracks (the dominant feature) plus only a
    very faint stress discolouration — so it reads as shattered impact, not a ball.
    Ships no image asset.
    """
    import math
    import random
    from PIL import Image, ImageDraw, ImageFilter

    W = max(int(h * 1.5), 24)
    H = W
    cx = cy = W / 2.0
    hole_r = W * 0.075
    tile = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    # Very faint, irregular stress discolouration (NOT a bright disc — that was the
    # thing that read as a ball). Low alpha, blurred, lumpy.
    disc = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dd = ImageDraw.Draw(disc)
    for _ in range(10):
        a = random.uniform(0, math.tau)
        d = random.uniform(0, W * 0.16)
        rr = W * random.uniform(0.10, 0.22)
        ox, oy = cx + math.cos(a) * d, cy + math.sin(a) * d
        dd.ellipse([ox - rr, oy - rr, ox + rr, oy + rr], fill=(210, 210, 215, 16))
    disc = disc.filter(ImageFilter.GaussianBlur(max(W * 0.045, 1)))
    tile.alpha_composite(disc)

    # Cracks (the dominant feature): jagged radial cracks that branch, + concentric.
    cracks = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    cd = ImageDraw.Draw(cracks)
    crack_col = (20, 20, 23, 235)

    def _crack(a, length, width, x=cx, y=cy, depth=0):
        steps = random.randint(4, 7)
        seg = length / steps
        pts = [(x, y)]
        for _ in range(steps):
            a += random.uniform(-0.32, 0.32)
            x += math.cos(a) * seg
            y += math.sin(a) * seg
            pts.append((x, y))
            if depth < 2 and width > 1 and random.random() < 0.28:
                _crack(a + random.uniform(-1.1, 1.1), length * 0.45,
                       max(width - 1, 1), x, y, depth + 1)
        cd.line(pts, fill=crack_col, width=int(width), joint="curve")

    n = random.randint(12, 18)
    for i in range(n):
        a = (i / n) * math.tau + random.uniform(-0.12, 0.12)
        _crack(a, W * random.uniform(0.30, 0.52), max(W * 0.006, 1))
    for _ in range(random.randint(2, 4)):
        ar = W * random.uniform(0.12, 0.32)
        st = random.uniform(0, 360)
        cd.arc([cx - ar, cy - ar, cx + ar, cy + ar], st, st + random.uniform(50, 160),
               fill=(30, 30, 33, 200), width=max(int(W * 0.004), 1))
    tile.alpha_composite(cracks)

    # The hole itself — a jagged dark polygon (not a clean circle) with a torn rim.
    hole = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hole)
    m = 12
    pts = []
    for i in range(m):
        a = (i / m) * math.tau
        r = hole_r * random.uniform(0.65, 1.3)
        pts.append((cx + math.cos(a) * r, cy + math.sin(a) * r))
    hd.polygon(pts, fill=(8, 8, 10, 255))
    hd.line(pts + [pts[0]], fill=(48, 42, 38, 200),
            width=max(int(W * 0.008), 1), joint="curve")
    hole = hole.filter(ImageFilter.GaussianBlur(0.5))
    tile.alpha_composite(hole)

    return tile


def add_bulletholes(data: bytes, count: int = 0) -> bytes:
    """Punch scattered bullet holes over an image. `count` <= 0 auto-scales. JPEG bytes."""
    return _scatter_overlay(data, _make_bullethole, count)


def bullethole_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Punch bullet holes into the first image attachment. Mirrors blood_attachments."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_bulletholes(data)
        out: OutputFile = {
            "filename": f"{stem}_bulletholes.jpg",
            "data": result,
            "content_type": "image/jpeg",
        }
        summary = f"## 🕳️ Bullet holes\n\n🕳️ {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"bullethole failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Fire overlay (the "fire" gag — scatter cartoon-real flames across an image)
# ---------------------------------------------------------------------------

def _make_fire(h: int):
    """Render one flame on a transparent tile (pure Pillow).

    Nested flame silhouettes from dark-red → red → orange → yellow → near-white
    core (a hot gradient), each with wobbling licks toward a tapered tip, plus a
    soft outer glow. Slightly translucent for an additive look. No image asset.
    """
    import math
    import random
    from PIL import Image, ImageDraw, ImageFilter, ImageChops

    W = max(int(h * 0.95), 18)
    H = max(int(h * 1.35), 26)
    cxf = W * 0.5
    base_y = H * 0.92
    phase = random.uniform(0, math.tau)
    tile = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    def _flame_mask(scale: float, wob: float):
        m = Image.new("L", (W, H), 0)
        d = ImageDraw.Draw(m)
        fw = W * 0.40 * scale          # half-width at the base
        fh = H * 0.84 * scale          # height
        n = 22
        left, right = [], []
        for i in range(n + 1):
            f = i / n
            y = base_y - f * fh
            # taper to the tip, with sinusoidal licks that grow toward the top
            w = fw * (1 - f) ** 0.65 * (1 + wob * 0.5 * math.sin(f * 7 + phase))
            sway = math.sin(f * 3.0 + phase) * fw * 0.16 * f
            left.append((cxf - w + sway, y))
            right.append((cxf + w + sway, y))
        d.polygon(left + list(reversed(right)), fill=255)
        d.ellipse([cxf - fw, base_y - fw * 0.5, cxf + fw, base_y + fw * 0.45], fill=255)
        return m

    # Outer glow (dark-red, blurred) then the hot nested layers. Same phase so the
    # licks of each layer line up and read as one flame with a bright core.
    layers = [
        (1.00, (120, 18, 4), 0.9, True),    # dark red glow
        (0.94, (210, 40, 6), 0.9, False),   # red
        (0.74, (255, 130, 18), 0.7, False), # orange
        (0.52, (255, 205, 60), 0.5, False), # yellow
        (0.30, (255, 248, 210), 0.35, False),  # white-hot core
    ]
    for scale, col, wob, glow in layers:
        m = _flame_mask(scale, wob)
        if glow:
            m = m.filter(ImageFilter.GaussianBlur(max(W * 0.06, 1)))
            alpha = 150
        else:
            m = m.filter(ImageFilter.GaussianBlur(max(W * 0.012, 0.6)))
            alpha = 235
        lyr = Image.new("RGBA", (W, H), col + (0,))
        lyr.putalpha(m.point(lambda a, _al=alpha: int(a * _al / 255)))
        tile.alpha_composite(lyr)

    return tile


def add_fire(data: bytes, count: int = 0) -> bytes:
    """Set the image alight: a wall of flames across the bottom third.

    Rather than scattering flames everywhere, this builds a continuous row of
    overlapping flames of varying heights rooted at the bottom edge (rising up to
    ~a third of the image, taller licks higher), over a warm glow rising from the
    bottom. Returns JPEG bytes.
    """
    import random
    from PIL import Image, ImageOps, ImageDraw, ImageFilter
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except Exception:
        pass

    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        W, H = img.size
        img = img.convert("RGBA")
        band = int(H * 0.34)                       # flames fill the lower third

        # Warm glow rising from the bottom edge (alpha gradient → tinted layer).
        gmask = Image.new("L", (1, H), 0)
        for y in range(H):
            if y >= H - band:
                f = (y - (H - band)) / band
                gmask.putpixel((0, y), int(140 * (f ** 1.5)))
        glow = Image.new("RGBA", (W, H), (255, 95, 15, 0))
        glow.putalpha(gmask.resize((W, H)))
        img.alpha_composite(glow)

        # Wall of flames: march across the width with overlap, random heights.
        x = -int(W * 0.04)
        while x < W:
            fh = int(band * random.uniform(0.78, 1.45))   # some licks exceed the band
            size = max(int(fh / 1.35), 14)
            flame = _make_fire(size)
            if random.random() < 0.5:
                flame = flame.transpose(Image.FLIP_LEFT_RIGHT)
            # root the flame's base at the image bottom (slight sink so no gap shows)
            y = H - flame.height + int(flame.height * 0.04)
            img.alpha_composite(flame, (x, y))
            x += int(flame.width * random.uniform(0.42, 0.66))

        img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue()


def fire_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Set the first image attachment on fire. Mirrors blood_attachments."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_fire(data)
        out: OutputFile = {
            "filename": f"{stem}_fire.jpg",
            "data": result,
            "content_type": "image/jpeg",
        }
        summary = f"## 🔥 Fire\n\n🔥 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"fire failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Gay stamp (the "gay" gag — a big red rubber stamp reading GAY across an image)
# ---------------------------------------------------------------------------

def _make_gay_stamp(text_h: int):
    """Render a distressed red rubber stamp reading "GAY" on a transparent tile.

    Bold text inside a double rectangular border, inked in stamp-red with a grungy
    speckle so it looks pressed (not printed). Pure Pillow. Returned upright; the
    caller rotates + scales it onto the image.
    """
    import random
    from PIL import Image, ImageDraw

    text = "GAY"
    stroke = max(text_h // 16, 2)
    font = _load_meme_font(text_h)
    tmp = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    bbox = tmp.textbbox((0, 0), text, font=font, stroke_width=stroke)
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]

    pad = int(text_h * 0.55)
    bw = max(int(text_h * 0.11), 4)
    W, H = tw + pad * 2, th + pad * 2
    red = (200, 28, 28, 235)

    tile = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)
    # double border
    d.rectangle([bw, bw, W - bw, H - bw], outline=red, width=bw)
    off = int(bw * 2.2)
    d.rectangle([off, off, W - off, H - off], outline=red, width=max(bw // 2, 2))
    # the word
    d.text(((W - tw) / 2 - bbox[0], (H - th) / 2 - bbox[1]), text, font=font,
           fill=red, stroke_width=stroke, stroke_fill=red)

    # grunge: knock out random specks so the ink looks pressed/uneven
    px = tile.load()
    for _ in range(int(W * H * 0.05)):
        x, y = random.randint(0, W - 1), random.randint(0, H - 1)
        r, g, b, a = px[x, y]
        if a > 0:
            px[x, y] = (r, g, b, int(a * random.uniform(0.0, 0.6)))
    return tile


def add_gay(data: bytes, count: int = 0) -> bytes:
    """Stamp a big rotated red "GAY" across the image. Returns JPEG bytes."""
    import random
    from PIL import Image, ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except Exception:
        pass

    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        W, H = img.size
        img = img.convert("RGBA")
        stamp = _make_gay_stamp(max(int(min(W, H) * 0.17), 24))
        target_w = int(W * 0.66)
        scale = target_w / stamp.width
        stamp = stamp.resize((max(int(stamp.width * scale), 1),
                              max(int(stamp.height * scale), 1)), Image.BICUBIC)
        stamp = stamp.rotate(random.uniform(15, 22), expand=True, resample=Image.BICUBIC)
        img.alpha_composite(stamp, ((W - stamp.width) // 2, (H - stamp.height) // 2))

        img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue()


def gay_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Stamp GAY on the first image attachment. Mirrors blood_attachments."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_gay(data)
        out: OutputFile = {
            "filename": f"{stem}_gay.jpg",
            "data": result,
            "content_type": "image/jpeg",
        }
        summary = f"## 🏳️‍🌈 Gay\n\n🏳️‍🌈 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"gay failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# BLACKED logo (the "blacked" gag — the blacked.com wordmark across an image)
# ---------------------------------------------------------------------------

# The real wordmark is a HEAVY, WIDE grotesque (Helvetica/Akzidenz-black family) —
# deliberately NOT Impact (too condensed/tall). Prefer black/heavy faces, then a
# bold Helvetica/Arial clone; fall back to the meme font as a last resort.
_BLACKED_FONT_CANDIDATES = [
    "/usr/share/fonts/archivo-black/ArchivoBlack-Regular.ttf",
    "/usr/share/fonts/truetype/archivo-black/ArchivoBlack-Regular.ttf",
    "/usr/share/fonts/roboto/Roboto-Black.ttf",
    "/usr/share/fonts/truetype/roboto/Roboto-Black.ttf",
    "/usr/share/fonts/montserrat/Montserrat-Black.ttf",
    "/usr/share/fonts/msttcorefonts/Arial_Black.ttf",
    "/usr/share/fonts/truetype/msttcorefonts/Arial_Black.ttf",
    "/usr/share/fonts/liberation-fonts/LiberationSans-Bold.ttf",
    "/usr/share/fonts/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/usr/share/fonts/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
]


def _load_blacked_font(size: int):
    """Load a heavy, wide grotesque for the BLACKED wordmark (never Impact)."""
    from PIL import ImageFont
    for path in _BLACKED_FONT_CANDIDATES:
        if os.path.exists(path):
            try:
                return ImageFont.truetype(path, size)
            except Exception:
                continue
    return _load_meme_font(size)


def _tracked_width(draw, text: str, font, tracking: float) -> float:
    """Total pixel width of `text` rendered with `tracking` px between glyphs."""
    if not text:
        return 0.0
    return sum(draw.textlength(ch, font=font) for ch in text) + tracking * (len(text) - 1)


def _draw_tracked(draw, x: float, y: float, text: str, font, tracking: float, **kw):
    """Draw `text` glyph-by-glyph with `tracking` px added between letters.

    Pillow has no letter-spacing, so we advance manually by each glyph's own
    width + `tracking`. `kw` is passed straight to ``draw.text`` (fill/stroke)."""
    for ch in text:
        draw.text((x, y), ch, font=font, **kw)
        x += draw.textlength(ch, font=font) + tracking
    return x


def _make_blacked(diam: int):
    """Render the blacked.com logo — a BLACK CIRCLE with white "BLACKED" inside.

    The font size auto-scales so the heavy, lightly-tracked wordmark fits across
    the disc; a thin light ring + soft drop shadow keep the black roundel visible
    on dark backgrounds too. Pure Pillow (no shipped asset); the caller scales and
    places it. `diam` is the circle diameter in px.
    """
    from PIL import Image, ImageDraw, ImageFilter

    diam = max(int(diam), 80)
    W = H = diam
    cx = cy = diam / 2.0
    text = "BLACKED"
    target_w = diam * 0.80                    # wordmark spans most of the disc
    tracking_ratio = 0.10

    # Pick the largest font whose tracked wordmark fits target_w (one scale pass).
    probe = ImageDraw.Draw(Image.new("RGBA", (8, 8)))
    base = max(int(diam * 0.16), 10)
    w0 = _tracked_width(probe, text, _load_blacked_font(base), base * tracking_ratio)
    fsize = max(int(base * target_w / max(w0, 1)), 10)
    font = _load_blacked_font(fsize)
    tracking = fsize * tracking_ratio
    total_w = _tracked_width(probe, text, font, tracking)
    heavy = max(int(fsize * 0.045), 1)        # over-stroke → fake a black weight
    ink = probe.textbbox((0, 0), text, font=font, stroke_width=heavy)
    th = ink[3] - ink[1]

    tile = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    pad = max(int(diam * 0.03), 3)

    # Soft drop shadow so the black disc separates from a dark background.
    shadow = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(shadow).ellipse([pad, pad, W - pad, H - pad], fill=(0, 0, 0, 170))
    shadow = shadow.filter(ImageFilter.GaussianBlur(max(diam * 0.02, 2)))
    tile.alpha_composite(shadow)

    d = ImageDraw.Draw(tile)
    # The black roundel with a thin light ring (reads on dark photos too).
    d.ellipse([pad, pad, W - pad, H - pad], fill=(8, 8, 8, 255),
              outline=(244, 244, 244, 255), width=max(int(diam * 0.012), 2))

    # White "BLACKED" centred across the disc.
    x0 = cx - total_w / 2.0
    y0 = cy - th / 2.0 - ink[1]
    _draw_tracked(d, x0, y0, text, font, tracking,
                  fill=(255, 255, 255, 255), stroke_width=heavy,
                  stroke_fill=(255, 255, 255, 255))
    return tile


def add_blacked(data: bytes, count: int = 0) -> bytes:
    """Stamp the round BLACKED logo centred in the lower third. Returns JPEG bytes."""
    from PIL import Image, ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except Exception:
        pass

    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        W, H = img.size
        img = img.convert("RGBA")
        logo = _make_blacked(max(int(min(W, H) * 0.42), 80))
        # Horizontally centred, sitting in the lower third (centre at ~2/3 H),
        # clamped so the roundel never spills off the bottom edge.
        x = (W - logo.width) // 2
        y = min(int(H * 0.66) - logo.height // 2, H - logo.height - max(int(H * 0.03), 4))
        img.alpha_composite(logo, (x, max(y, 0)))

        img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue()


def blacked_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Slap the BLACKED logo on the first image attachment. Mirrors gay_attachments."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_blacked(data)
        out: OutputFile = {
            "filename": f"{stem}_blacked.jpg",
            "data": result,
            "content_type": "image/jpeg",
        }
        summary = f"## 🥷 Blacked\n\n🥷 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"blacked failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Kosher seal (the "kosher" gag — a 100% KOSHER certification badge on an image)
# ---------------------------------------------------------------------------

def _make_kosher(diam: int):
    """Render a circular kosher-certification seal on a transparent tile.

    A clean OU-style hechsher: a white disc with a double dark-blue ring, a bold
    "U" inscribed in an inner circle (the classic OU mark), "KOSHER" arched-style
    text below it and "100%" above — wholesome and SFW. Pure Pillow (no asset);
    the caller scales/places it. `diam` is the badge diameter in px.
    """
    from PIL import Image, ImageDraw

    diam = max(int(diam), 40)
    W = H = diam
    cx = cy = diam / 2.0
    blue = (20, 64, 140, 255)
    white = (255, 255, 255, 255)

    tile = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)

    ring = max(int(diam * 0.045), 3)
    # white disc
    d.ellipse([1, 1, W - 2, H - 2], fill=white)
    # outer + inner ring
    d.ellipse([1, 1, W - 2, H - 2], outline=blue, width=ring)
    inset = int(diam * 0.10)
    d.ellipse([inset, inset, W - inset, H - inset], outline=blue, width=max(ring // 2, 2))

    # The OU mark: a "U" inscribed in a circle, centred a touch high.
    ou_r = diam * 0.20
    ou_cy = cy - diam * 0.06
    d.ellipse([cx - ou_r, ou_cy - ou_r, cx + ou_r, ou_cy + ou_r],
              outline=blue, width=max(int(diam * 0.025), 2))
    u_font = _load_meme_font(max(int(diam * 0.26), 14))
    ub = d.textbbox((0, 0), "U", font=u_font)
    uw, uh = ub[2] - ub[0], ub[3] - ub[1]
    d.text((cx - uw / 2 - ub[0], ou_cy - uh / 2 - ub[1]), "U", font=u_font, fill=blue)

    # "100%" above the mark, "KOSHER" below — straight lines, centred.
    top_font = _load_meme_font(max(int(diam * 0.11), 8))
    tb = d.textbbox((0, 0), "100%", font=top_font)
    d.text((cx - (tb[2] - tb[0]) / 2 - tb[0], diam * 0.16 - tb[1]),
           "100%", font=top_font, fill=blue)
    bot_font = _load_meme_font(max(int(diam * 0.13), 9))
    bb = d.textbbox((0, 0), "KOSHER", font=bot_font)
    d.text((cx - (bb[2] - bb[0]) / 2 - bb[0], diam * 0.70 - bb[1]),
           "KOSHER", font=bot_font, fill=blue)

    return tile


def add_kosher(data: bytes, count: int = 0) -> bytes:
    """Stamp a 100% KOSHER certification seal centred in the lower third. JPEG bytes."""
    from PIL import Image, ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except Exception:
        pass

    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        W, H = img.size
        img = img.convert("RGBA")
        diam = max(int(min(W, H) * 0.42), 48)
        seal = _make_kosher(diam)
        # Horizontally centred, sitting in the lower third (its centre at ~2/3 H),
        # clamped so it never spills off the bottom edge.
        x = (W - seal.width) // 2
        y = min(int(H * 0.66) - seal.height // 2, H - seal.height - max(int(H * 0.03), 4))
        img.alpha_composite(seal, (x, max(y, 0)))

        img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue()


def kosher_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Stamp the KOSHER seal on the first image attachment. Mirrors gay_attachments."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_kosher(data)
        out: OutputFile = {
            "filename": f"{stem}_kosher.jpg",
            "data": result,
            "content_type": "image/jpeg",
        }
        summary = f"## ✡️ Kosher\n\n✡️ {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"kosher failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Barked (the "barked" gag — a smirking cartoon dog + "#BARKED" caption)
# ---------------------------------------------------------------------------

def _make_barked_dog(h: int):
    """Render a smirking cartoon dog face on a transparent square tile (pure Pillow).

    Floppy ears, a lighter muzzle with a black nose, half-lidded sly eyes, a cocked
    eyebrow and an asymmetric raised-corner smirk (with a cheeky tongue). Ships no
    image asset. `h` is the tile size in px.
    """
    from PIL import Image, ImageDraw

    W = H = max(int(h), 48)
    cx = W / 2.0
    fur = (176, 132, 86)
    dark = _shade(fur, 0.72)[:3]
    muzzle = (228, 205, 170)
    outline = _shade(fur, 0.5)[:3]
    lw = max(int(W * 0.012), 2)

    tile = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)

    # --- floppy ears (behind the head) ---
    d.ellipse([W * 0.02, H * 0.16, W * 0.32, H * 0.74], fill=dark, outline=outline, width=lw)
    d.ellipse([W * 0.68, H * 0.16, W * 0.98, H * 0.74], fill=dark, outline=outline, width=lw)

    # --- head ---
    d.ellipse([W * 0.15, H * 0.12, W * 0.85, H * 0.88], fill=fur, outline=outline, width=lw)

    # --- muzzle (lighter) ---
    d.ellipse([W * 0.30, H * 0.50, W * 0.70, H * 0.88], fill=muzzle, outline=outline, width=lw)

    # --- eyes: half-lidded / sly. A white lens with a big pupil, a fur "lid" over
    # the top half, plus a cocked right eyebrow → smug. ---
    for ex in (W * 0.39, W * 0.61):
        ey = H * 0.42
        rx, ry = W * 0.085, H * 0.085
        d.ellipse([ex - rx, ey - ry, ex + rx, ey + ry], fill=(255, 255, 255, 255),
                  outline=outline, width=max(lw - 1, 1))
        # pupil sits low (looking down its nose)
        pr = rx * 0.62
        d.ellipse([ex - pr, ey - pr * 0.4, ex + pr, ey + pr * 1.6], fill=(25, 22, 20, 255))
        d.ellipse([ex - pr * 0.2, ey + pr * 0.1, ex + pr * 0.3, ey + pr * 0.6],
                  fill=(255, 255, 255, 230))  # catch-light
        # heavy upper lid (fur) covering the top third → half-closed sly look
        d.chord([ex - rx - 1, ey - ry - 1, ex + rx + 1, ey + ry * 0.7], 180, 360, fill=fur)
    # cocked eyebrow over the right eye
    d.line([(W * 0.54, H * 0.30), (W * 0.69, H * 0.26)], fill=outline, width=lw + 1, joint="curve")
    d.line([(W * 0.31, H * 0.30), (W * 0.46, H * 0.31)], fill=outline, width=lw + 1, joint="curve")

    # --- nose ---
    d.ellipse([cx - W * 0.075, H * 0.52, cx + W * 0.075, H * 0.63], fill=(28, 24, 22, 255))
    d.ellipse([cx - W * 0.03, H * 0.535, cx, H * 0.565], fill=(120, 110, 105, 220))  # sheen

    # --- smirk: philtrum down from the nose, a small relaxed left side and a raised
    # right corner; a cheeky tongue peeks from the high corner. ---
    mouth_col = _shade(fur, 0.35)[:3]
    d.line([(cx, H * 0.63), (cx, H * 0.70)], fill=mouth_col, width=lw, joint="curve")
    d.line([(cx, H * 0.70), (W * 0.40, H * 0.76), (W * 0.36, H * 0.72)],
           fill=mouth_col, width=lw, joint="curve")                       # relaxed left
    d.line([(cx, H * 0.70), (W * 0.62, H * 0.72), (W * 0.70, H * 0.65)],
           fill=mouth_col, width=lw + 1, joint="curve")                   # raised right (smirk)
    # tongue at the raised corner
    d.ellipse([W * 0.60, H * 0.70, W * 0.70, H * 0.80], fill=(228, 120, 130, 255),
              outline=mouth_col, width=max(lw - 1, 1))
    d.line([(W * 0.65, H * 0.71), (W * 0.65, H * 0.78)], fill=_shade((228, 120, 130), 0.8)[:3],
           width=max(lw - 1, 1))

    return tile


def add_barked(data: bytes, count: int = 0) -> bytes:
    """Drop a smirking cartoon dog with a "#BARKED" caption onto an image. JPEG bytes."""
    from PIL import Image, ImageOps, ImageDraw
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except Exception:
        pass

    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        W, H = img.size
        img = img.convert("RGBA")

        dog_size = max(int(min(W, H) * 0.5), 64)
        dog = _make_barked_dog(dog_size)

        # "#BARKED" caption (outlined white) sits below the dog; the dog + caption
        # are centred horizontally and sit as a group in the lower third.
        text = "#BARKED"
        font = _load_meme_font(max(int(dog_size * 0.24), 14))
        stroke = max(int(dog_size * 0.012), 2)
        d = ImageDraw.Draw(img)
        tb = d.textbbox((0, 0), text, font=font, stroke_width=stroke)
        tw, th = tb[2] - tb[0], tb[3] - tb[1]
        gap = max(int(dog_size * 0.06), 6)

        total_h = dog.height + gap + th
        # Group centre sits at ~2/3 H (lower third), clamped to the bottom margin.
        bottom_margin = max(int(H * 0.03), 4)
        top = int(H * 0.66) - total_h // 2
        top = max(min(top, H - total_h - bottom_margin), 0)
        img.alpha_composite(dog, ((W - dog.width) // 2, top))
        d.text(((W - tw) / 2 - tb[0], top + dog.height + gap - tb[1]),
               text, font=font, fill="white", stroke_width=stroke, stroke_fill="black")

        img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue()


def barked_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Drop the smirking dog + #BARKED on the first image attachment. Mirrors gay_attachments."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_barked(data)
        out: OutputFile = {
            "filename": f"{stem}_barked.jpg",
            "data": result,
            "content_type": "image/jpeg",
        }
        summary = f"## 🐶 Barked\n\n🐶 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"barked failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"
