"""Creative image effects — the "Effects" group: meme captions and the dildo / poo
scatter gags.

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

    # Irregular central pool: a core plus protruding lobes at random angles so the
    # outline is jagged (not a clean disc).
    _dot(cx, cy, main_r * 0.85)
    for _ in range(7):
        a = random.uniform(0, math.tau)
        d = main_r * random.uniform(0.2, 0.85)
        lr = main_r * random.uniform(0.4, 0.8)
        _dot(cx + math.cos(a) * d, cy + math.sin(a) * d, lr)

    def _trail(x0, y0, x1, y1, wa, wb):
        """A smooth tapering trail from (x0,y0,width wa) to (x1,y1,width wb):
        overlapping dots spaced finer than their radius so it reads continuous."""
        seg = math.hypot(x1 - x0, y1 - y0)
        n = max(int(seg / max(min(wa, wb) * 0.5, 1.0)), 6)
        for s in range(n + 1):
            f = s / n
            _dot(x0 + (x1 - x0) * f, y0 + (y1 - y0) * f, max(wa + (wb - wa) * f, 1.0))

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
