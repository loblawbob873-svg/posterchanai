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
import re
from pathlib import Path
from typing import List, Tuple

from app.services.media_service import OutputFile, _human_size, is_image

logger = logging.getLogger(__name__)


def _alive_or_still(still_bytes: bytes, stem: str, suffix: str) -> OutputFile:
    """Return a stamp/overlay gag (meme/dildo/poo/bullethole/gay/blacked/kosher/barked/
    consider) as a STILL image. Auto-animating every stamp with parallax was a bad
    default — motion is now opt-in via the `alive` modifier (e.g. `dildo alive`) or the
    other motion modifiers (`dildo zoom`), so these return a flat image unless asked."""
    return {"filename": f"{stem}_{suffix}.jpg", "data": still_bytes, "content_type": "image/jpeg"}


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
        # Measure the WHOLE wrapped block with Pillow's multiline metrics (incl.
        # stroke + real line advance) — estimating per-line from a tight "Ay" bbox
        # undercounts the height and overflows the bottom on tall/portrait photos.
        def _spacing_for(font) -> int:
            lh = draw.textbbox((0, 0), "Ay", font=font)[3]
            return max(int(lh * 0.2), 2)

        def _block_bbox(font, lines, stroke, spacing):
            return draw.multiline_textbbox(
                (0, 0), "\n".join(lines), font=font, stroke_width=stroke,
                spacing=spacing, align="center",
            )

        chosen_font = None
        chosen_lines: List[str] = []
        line_spacing = 0
        stroke = 2
        start = max(int(H / 6), 14)
        for size in range(start, 11, -2):
            font = _load_meme_font(size)
            lines = _wrap_text_to_width(draw, text, font, max_width)
            spacing = _spacing_for(font)
            st = max(int(size * 0.06), 2)  # outline scales with font size
            bbox = _block_bbox(font, lines, st, spacing)
            bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
            if bh <= max_height and bw <= max_width:
                chosen_font, chosen_lines, line_spacing, stroke = font, lines, spacing, st
                break
        if chosen_font is None:
            # Even the floor size overflows — use it anyway (best effort).
            chosen_font = _load_meme_font(12)
            chosen_lines = _wrap_text_to_width(draw, text, chosen_font, max_width)
            line_spacing = _spacing_for(chosen_font)
            stroke = 2

        block = "\n".join(chosen_lines)
        bbox = _block_bbox(chosen_font, chosen_lines, stroke, line_spacing)
        bw, bh = bbox[2] - bbox[0], bbox[3] - bbox[1]
        # Anchor: horizontally centred, block bottom inside the bottom margin.
        # Offset by the bbox origin so the placement is exact (stroke can push the
        # measured top negative).
        x = (W - bw) / 2 - bbox[0]
        y = (H - margin - bh) - bbox[1]
        draw.multiline_text(
            (x, y), block, font=chosen_font, fill="white",
            stroke_width=stroke, stroke_fill="black",
            spacing=line_spacing, align="center",
        )

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
        out = _alive_or_still(result, stem, "meme")
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


# Animated counterpart of _scatter_overlay. The scatter LAYOUT (per-tile size, spin,
# shape seed and position) is fixed once; each frame re-renders every tile with an
# advancing `grow`, so the splatters stay put while their drips/strands run outward.
# Returns a list of RGB frames (encode with media_service.frames_to_video). The motion
# is a one-shot reveal — `grow` ramps up then holds — so blood/cum land and drip
# rather than looping.
_SCATTER_ANIM_FRAMES = 36
_SCATTER_ANIM_FPS = 14
_SCATTER_ANIM_MAXDIM = 1280   # cap the working long edge so re-render + encode stay cheap
_SCATTER_ANIM_MAXTILES = 30   # cap tile count for animation (the still allows up to 60)


def _scatter_frames(data: bytes, make_tile_seeded, count: int = 0,
                    max_rotation: float = 180.0, n_frames: int = _SCATTER_ANIM_FRAMES,
                    ramp_frac: float = 0.55, start_grow: float = 0.12):
    """Build the animation frames for a scatter gag. `make_tile_seeded(size, seed,
    grow)` must render one RGBA tile deterministically for a given seed (stable shape)
    with `grow` in [0,1] scaling its drip/streak extent."""
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
            background = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            background.paste(rgba, mask=rgba.split()[-1])
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")
        # Bound the working resolution so re-rendering N frames stays affordable.
        if max(img.size) > _SCATTER_ANIM_MAXDIM:
            img.thumbnail((_SCATTER_ANIM_MAXDIM, _SCATTER_ANIM_MAXDIM), Image.LANCZOS)
        base_rgb = img.convert("RGB")

    W, H = base_rgb.size
    if count <= 0:
        count = max(14, min(60, (W * H) // 38000))
    count = min(count, _SCATTER_ANIM_MAXTILES)
    base = min(W, H)
    lo, hi = max(int(base * 0.12), 12), max(int(base * 0.28), 24)

    # Fix the layout once. Position is picked from a full-grow render's rotated size;
    # the tile canvas is grow-independent, so every frame's tile lands identically.
    layout = []
    for _ in range(count):
        size = random.randint(lo, hi)
        angle = random.uniform(-max_rotation, max_rotation)
        seed = random.randrange(1 << 30)
        sample = make_tile_seeded(size, seed, 1.0).rotate(
            angle, expand=True, resample=Image.BICUBIC)
        x = random.randint(-sample.width // 3, max(W - sample.width * 2 // 3, 1))
        y = random.randint(-sample.height // 3, max(H - sample.height * 2 // 3, 1))
        layout.append((size, angle, seed, x, y))

    frames = []
    for fi in range(n_frames):
        # grow ramps start_grow→1 (smoothstep) over the first `ramp_frac`, then holds.
        p = min(fi / max(n_frames * ramp_frac, 1.0), 1.0)
        grow = start_grow + (1.0 - start_grow) * (p * p * (3 - 2 * p))
        layer = base_rgb.convert("RGBA")
        for size, angle, seed, x, y in layout:
            tile = make_tile_seeded(size, seed, grow).rotate(
                angle, expand=True, resample=Image.BICUBIC)
            layer.alpha_composite(tile, (x, y))
        frames.append(layer.convert("RGB"))
    return frames


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
        out = _alive_or_still(result, stem, "dildo")
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
        out = _alive_or_still(result, stem, "poo")
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


def _make_cum(h: int, rng=None, grow: float = 1.0):
    """Render one glossy off-white splatter (the "cum" gag) on a transparent tile.

    Pure Pillow — an irregular central blob plus a few radiating strands tipped
    with droplets (and the odd satellite speck), given a soft translucent dark rim
    so the near-white body still reads on light backgrounds, plus wet specular
    highlights and slight translucency. Ships no image asset (like the poo path).

    For animation: pass a seeded ``rng`` (a ``random.Random``) so the blob's shape is
    stable frame-to-frame, and a ``grow`` in [0,1] that scales how far the flung
    strands have shot out — advance it across frames and the strands ooze outward.
    """
    import math
    import random as _rnd
    from PIL import Image, ImageDraw, ImageFilter, ImageChops
    random = rng if rng is not None else _rnd

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
        dist = main_r * random.uniform(1.4, 3.0) * grow
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


def _cum_tile(size: int, seed: int, grow: float):
    """One seeded cum tile (stable shape, `grow`-scaled strands) for the animator."""
    import random
    return _make_cum(size, rng=random.Random(seed), grow=grow)


def add_cum_animated(data: bytes, count: int = 0) -> bytes:
    """Scatter cum as a short MP4 — the strands ooze outward then hold. Silent H.264
    bytes. The splatters are placed once; each frame re-renders them further along."""
    from app.services.media_service import frames_to_video
    frames = _scatter_frames(data, _cum_tile, count=count)
    return frames_to_video(frames, fps=_SCATTER_ANIM_FPS, loops=1)


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
        if _effects_animate():
            try:
                result = add_cum_animated(data)
                out: OutputFile = {
                    "filename": f"{stem}_cum.mp4",
                    "data": result,
                    "content_type": "video/mp4",
                }
                summary = f"## 💦 Cum\n\n💦 {filename}: {_human_size(len(result))}"
                return [out], summary
            except Exception as e:
                logger.warning(f"animated cum failed for {filename}, using still: {e}")
        result = add_cum(data)
        out = {
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


def _make_blood(h: int, rng=None, grow: float = 1.0):
    """Render one wet blood splatter on a transparent tile (pure Pillow).

    An irregular central pool with thin radial impact spatter (droplet-tipped
    arms) AND the signature gravity DRIPS running downward into rounded beads,
    finished with a soft dark rim, a slightly darker inner edge for depth, and a
    small wet specular highlight. Ships no image asset (like the cum path).

    For animation: pass a seeded ``rng`` (a ``random.Random``) so the splatter's
    shape is stable frame-to-frame, and a ``grow`` in [0,1] that scales how far the
    gravity drips have run down — advance it across frames and the blood drips.
    """
    import math
    import random as _rnd
    from PIL import Image, ImageDraw, ImageFilter, ImageChops
    random = rng if rng is not None else _rnd

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
        length = H * random.uniform(0.28, 0.58) * grow
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


def _blood_tile(size: int, seed: int, grow: float):
    """One seeded blood tile (stable shape, `grow`-scaled drips) for the animator."""
    import random
    return _make_blood(size, rng=random.Random(seed), grow=grow)


def add_blood_animated(data: bytes, count: int = 0) -> bytes:
    """Scatter blood as a short MP4 — the drips run downward then hold. Silent H.264
    bytes. Spin stays small (like the still) so the drips fall straight down."""
    from app.services.media_service import frames_to_video
    frames = _scatter_frames(data, _blood_tile, count=count, max_rotation=10.0)
    return frames_to_video(frames, fps=_SCATTER_ANIM_FPS, loops=1)


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
        if _effects_animate():
            try:
                result = add_blood_animated(data)
                out: OutputFile = {
                    "filename": f"{stem}_blood.mp4",
                    "data": result,
                    "content_type": "video/mp4",
                }
                summary = f"## 🩸 Blood\n\n🩸 {filename}: {_human_size(len(result))}"
                return [out], summary
            except Exception as e:
                logger.warning(f"animated blood failed for {filename}, using still: {e}")
        result = add_blood(data)
        out = {
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

    A punched impact on a SOLID surface: a dark irregular penetration ringed by a
    ragged, pulverised crater of deformed material, with only a few short uneven
    stress chips and a little knocked-out debris. Deliberately NOT the dense even
    radial + concentric crack pattern that read as a spider web. Ships no image asset.
    """
    import math
    import random
    from PIL import Image, ImageDraw, ImageFilter

    W = max(int(h * 1.4), 22)
    H = W
    cx = cy = W / 2.0
    hole_r = W * 0.15
    crater_r = hole_r * 1.55
    tile = Image.new("RGBA", (W, H), (0, 0, 0, 0))

    def _blob(rmin, rmax, m=20):
        """A closed irregular blob polygon of points between rmin..rmax of crater_r."""
        pts = []
        for i in range(m):
            a = (i / m) * math.tau
            rr = crater_r * random.uniform(rmin, rmax)
            pts.append((cx + math.cos(a) * rr, cy + math.sin(a) * rr))
        return pts

    # --- pulverised crater rim: a ragged lighter ring of deformed material (the
    #     surface punched out), soft-edged and irregular so it never reads as a disc. ---
    crater = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(crater).polygon(_blob(0.78, 1.18), fill=(150, 146, 143, 90))
    crater = crater.filter(ImageFilter.GaussianBlur(max(W * 0.05, 1.5)))
    tile.alpha_composite(crater)

    # --- darker bruise just inside the crater (depth toward the hole) ---
    bruise = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(bruise).polygon(_blob(0.42, 0.72), fill=(78, 74, 74, 170))
    bruise = bruise.filter(ImageFilter.GaussianBlur(max(W * 0.018, 0.8)))
    tile.alpha_composite(bruise)

    # --- a FEW short stress chips: uneven angles, no branching, no concentric rings ---
    chips = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    chd = ImageDraw.Draw(chips)
    for _ in range(random.randint(3, 6)):
        a = random.uniform(0, math.tau)
        length = W * random.uniform(0.10, 0.24)
        steps = random.randint(2, 4)
        seg = length / steps
        x, y = cx + math.cos(a) * hole_r * 0.85, cy + math.sin(a) * hole_r * 0.85
        pts, aa = [(x, y)], a
        for _ in range(steps):
            aa += random.uniform(-0.3, 0.3)
            x += math.cos(aa) * seg
            y += math.sin(aa) * seg
            pts.append((x, y))
        chd.line(pts, fill=(36, 33, 33, 210), width=max(int(W * 0.012), 1), joint="curve")
    tile.alpha_composite(chips)

    # --- knocked-out debris specks scattered just outside the crater ---
    deb = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    dbd = ImageDraw.Draw(deb)
    for _ in range(random.randint(4, 9)):
        a = random.uniform(0, math.tau)
        d = random.uniform(crater_r * 0.55, crater_r * 1.35)
        rr = W * random.uniform(0.006, 0.018)
        ox, oy = cx + math.cos(a) * d, cy + math.sin(a) * d
        dbd.ellipse([ox - rr, oy - rr, ox + rr, oy + rr],
                    fill=(46, 43, 43, random.randint(110, 200)))
    tile.alpha_composite(deb)

    # --- the hole itself: a dark irregular penetration with a faint torn rim ---
    hole = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    hd = ImageDraw.Draw(hole)
    pts = []
    for i in range(12):
        a = (i / 12) * math.tau
        r = hole_r * random.uniform(0.7, 1.25)
        pts.append((cx + math.cos(a) * r, cy + math.sin(a) * r))
    hd.polygon(pts, fill=(10, 9, 11, 255))
    hd.line(pts + [pts[0]], fill=(72, 66, 60, 170), width=max(int(W * 0.01), 1), joint="curve")
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
        out = _alive_or_still(result, stem, "bulletholes")
        summary = f"## 🕳️ Bullet holes\n\n🕳️ {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"bullethole failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Fire overlay (the "fire" gag — scatter cartoon-real flames across an image)
# ---------------------------------------------------------------------------

def _make_fire(h: int, phase=None):
    """Render one flame on a transparent tile (pure Pillow).

    Nested flame silhouettes from dark-red → red → orange → yellow → near-white
    core (a hot gradient), each with wobbling licks toward a tapered tip, plus a
    soft outer glow. Slightly translucent for an additive look. No image asset.

    `phase` (radians) sets where the flame is in its wobble cycle — pass an
    advancing value to animate the licks frame-to-frame; defaults to random (a
    fixed still flame).
    """
    import math
    import random
    from PIL import Image, ImageDraw, ImageFilter, ImageChops

    W = max(int(h * 0.95), 18)
    H = max(int(h * 1.35), 26)
    cxf = W * 0.5
    base_y = H * 0.92
    if phase is None:
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


def _effects_animate() -> bool:
    """Whether the animatable effects (fire/blood/cum) render as a moving MP4 rather
    than a flat still. On by default; set EFFECTS_ANIMATE=0 to fall back to stills
    (no redeploy needed — flip the env on the service and restart)."""
    return os.environ.get("EFFECTS_ANIMATE", "1").strip().lower() not in ("0", "false", "no")


# How many frames make one seamless flicker cycle, and the playback rate. The cycle
# wraps (phase 0 → 2π), so `frames_to_video(..., loops=N)` plays it N times smoothly.
_FIRE_ANIM_FRAMES = 20
_FIRE_ANIM_FPS = 14
_FIRE_ANIM_LOOPS = 3


def add_fire_animated(data: bytes) -> bytes:
    """Set the image alight as a looping MP4 — the flames actually flicker.

    Same wall-of-flames layout as `add_fire`, but the per-flame positions/sizes are
    fixed once and each flame's wobble `phase` advances over a wrapping cycle, so the
    licks dance frame-to-frame. Returns silent H.264 MP4 bytes (routed like the audio
    gags). Falls back to the still `add_fire` JPEG only via the caller on error.
    """
    import math
    import random
    from PIL import Image, ImageOps
    from app.services.media_service import frames_to_video
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
        base = img.convert("RGBA")
        band = int(H * 0.34)

        # Warm glow rising from the bottom edge — static across frames.
        gmask = Image.new("L", (1, H), 0)
        for y in range(H):
            if y >= H - band:
                f = (y - (H - band)) / band
                gmask.putpixel((0, y), int(140 * (f ** 1.5)))
        glow = Image.new("RGBA", (W, H), (255, 95, 15, 0))
        glow.putalpha(gmask.resize((W, H)))
        base.alpha_composite(glow)

        # Fix the flame layout ONCE (position, size, flip, phase offset) so only the
        # licks move between frames — re-randomising per frame would just boil noise.
        flames = []
        x = -int(W * 0.04)
        while x < W:
            fh = int(band * random.uniform(0.78, 1.45))
            size = max(int(fh / 1.35), 14)
            flip = random.random() < 0.5
            ph0 = random.uniform(0, math.tau)
            flames.append((x, size, flip, ph0))
            x += int(size * 0.95 * random.uniform(0.42, 0.66)) or 1

        frames = []
        for fi in range(_FIRE_ANIM_FRAMES):
            t = fi / _FIRE_ANIM_FRAMES          # 0 → just-under-1, wraps to 0
            frame = base.copy()
            for fx, size, flip, ph0 in flames:
                flame = _make_fire(size, phase=ph0 + math.tau * t)
                if flip:
                    flame = flame.transpose(Image.FLIP_LEFT_RIGHT)
                y = H - flame.height + int(flame.height * 0.04)
                frame.alpha_composite(flame, (fx, y))
            frames.append(frame.convert("RGB"))

    return frames_to_video(frames, fps=_FIRE_ANIM_FPS, loops=_FIRE_ANIM_LOOPS)


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
        if _effects_animate():
            try:
                result = add_fire_animated(data)
                out: OutputFile = {
                    "filename": f"{stem}_fire.mp4",
                    "data": result,
                    "content_type": "video/mp4",
                }
                summary = f"## 🔥 Fire\n\n🔥 {filename}: {_human_size(len(result))}"
                return [out], summary
            except Exception as e:
                logger.warning(f"animated fire failed for {filename}, using still: {e}")
        result = add_fire(data)
        out = {
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
        out = _alive_or_still(result, stem, "gay")
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
        # Horizontally centred, sitting low (centre at ~78% H), clamped so the
        # roundel never spills off the bottom edge.
        x = (W - logo.width) // 2
        y = min(int(H * 0.78) - logo.height // 2, H - logo.height - max(int(H * 0.03), 4))
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
        out = _alive_or_still(result, stem, "blacked")
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
        out = _alive_or_still(result, stem, "kosher")
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
        out = _alive_or_still(result, stem, "barked")
        summary = f"## 🐶 Barked\n\n🐶 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"barked failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Hava (the "hava" gag — turn an image into an MP4 set to Hava Nagila)
# ---------------------------------------------------------------------------

# The shipped Hava Nagila track (repo-relative), overridable with HAVA_AUDIO_PATH
# (e.g. point it at nas.lan:/raid/cloud/Music/hava.mp3 if mounted locally).
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_HAVA_AUDIO_CANDIDATES = [
    os.environ.get("HAVA_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "hava.mp3"),
    "/var/lib/posterchanai/assets/hava.mp3",
]


def _hava_audio_path() -> str:
    """First existing Hava Nagila mp3 from the candidate list ("" if none)."""
    for p in _HAVA_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


# Length of song to play over the image (seconds).
_HAVA_DURATION = 6.0


def add_hava(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a 6-second MP4 playing Hava Nagila over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _hava_audio_path()
    if not audio:
        raise RuntimeError("Hava Nagila audio (assets/hava.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_HAVA_DURATION)


def hava_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a Hava Nagila MP4. Mirrors gay_attachments,
    but the output is a video (so the bots route it through their video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_hava(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_hava.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🎻 Hava\n\n🎻 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"hava failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Indian (the "indian" gag — turn an image into an MP4 set to an Indian song)
# ---------------------------------------------------------------------------

# The shipped Indian track (repo-relative), overridable with INDIAN_AUDIO_PATH.
_INDIAN_AUDIO_CANDIDATES = [
    os.environ.get("INDIAN_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "indian.mp3"),
    "/var/lib/posterchanai/assets/indian.mp3",
]
_INDIAN_DURATION = 6.0


def _indian_audio_path() -> str:
    """First existing Indian-song mp3 from the candidate list ("" if none)."""
    for p in _INDIAN_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_indian(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a 6-second MP4 playing the Indian song over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _indian_audio_path()
    if not audio:
        raise RuntimeError("Indian audio (assets/indian.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_INDIAN_DURATION)


def indian_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into an Indian-song MP4. Mirrors hava_attachments
    (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_indian(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_indian.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🇮🇳 Indian\n\n🇮🇳 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"indian failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Yakety (the "yakety" gag — turn an image into an MP4 set to Yakety Sax)
# ---------------------------------------------------------------------------

# The shipped Yakety Sax track (repo-relative), overridable with YAKETY_AUDIO_PATH.
_YAKETY_AUDIO_CANDIDATES = [
    os.environ.get("YAKETY_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "yakety.mp3"),
    "/var/lib/posterchanai/assets/yakety.mp3",
]
_YAKETY_DURATION = 9.0


def _yakety_audio_path() -> str:
    """First existing Yakety Sax mp3 from the candidate list ("" if none)."""
    for p in _YAKETY_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_yakety(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a 9-second MP4 playing Yakety Sax over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _yakety_audio_path()
    if not audio:
        raise RuntimeError("Yakety Sax audio (assets/yakety.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_YAKETY_DURATION)


def yakety_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a Yakety Sax MP4. Mirrors hava_attachments
    (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_yakety(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_yakety.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🎷 Yakety Sax\n\n🎷 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"yakety failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Yamete (the "yamete" gag — turn an image into an MP4 set to the yamete clip)
# ---------------------------------------------------------------------------

# The shipped Yamete track (repo-relative), overridable with YAMETE_AUDIO_PATH.
_YAMETE_AUDIO_CANDIDATES = [
    os.environ.get("YAMETE_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "yamete.mp3"),
    "/var/lib/posterchanai/assets/yamete.mp3",
]
_YAMETE_DURATION = 6.0


def _yamete_audio_path() -> str:
    """First existing Yamete mp3 from the candidate list ("" if none)."""
    for p in _YAMETE_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_yamete(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a 6-second MP4 playing the yamete clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _yamete_audio_path()
    if not audio:
        raise RuntimeError("Yamete audio (assets/yamete.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_YAMETE_DURATION)


def yamete_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a yamete MP4. Mirrors hava_attachments
    (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_yamete(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_yamete.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🛑 Yamete\n\n🛑 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"yamete failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Curb (the "curb" gag — turn an image into an MP4 set to the Curb Your
# Enthusiasm theme, the awkward freeze-frame outro)
# ---------------------------------------------------------------------------

# The shipped Curb Your Enthusiasm theme (repo-relative), overridable with
# CURB_AUDIO_PATH.
_CURB_AUDIO_CANDIDATES = [
    os.environ.get("CURB_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "curb.mp3"),
    "/var/lib/posterchanai/assets/curb.mp3",
]
_CURB_DURATION = 14.0


def _curb_audio_path() -> str:
    """First existing Curb theme mp3 from the candidate list ("" if none)."""
    for p in _CURB_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_curb(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into an MP4 playing the Curb theme over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _curb_audio_path()
    if not audio:
        raise RuntimeError("Curb theme audio (assets/curb.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_CURB_DURATION)


def curb_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a Curb Your Enthusiasm MP4. Mirrors
    yamete_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_curb(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_curb.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 😬 Curb\n\n😬 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"curb failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Depressing (turn an image into an MP4 set to the first 10s of a melancholy
# track — the "depressing" gag)
# ---------------------------------------------------------------------------

# The shipped depressing track (repo-relative), overridable with
# DEPRESSING_AUDIO_PATH.
_DEPRESSING_AUDIO_CANDIDATES = [
    os.environ.get("DEPRESSING_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "depressing.mp3"),
    "/var/lib/posterchanai/assets/depressing.mp3",
]
_DEPRESSING_DURATION = 10.0


def _depressing_audio_path() -> str:
    """First existing depressing mp3 from the candidate list ("" if none)."""
    for p in _DEPRESSING_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_depressing(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a 10s MP4 playing the depressing track over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _depressing_audio_path()
    if not audio:
        raise RuntimeError("Depressing audio (assets/depressing.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_DEPRESSING_DURATION)


def depressing_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a depressing 10s MP4. Mirrors
    curb_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_depressing(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_depressing.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 😢 Depressing\n\n😢 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"depressing failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Fahh (turn an image into a short MP4 set to the fahh clip — the "fahh" gag)
# ---------------------------------------------------------------------------

# The shipped fahh track (repo-relative), overridable with FAHH_AUDIO_PATH.
_FAHH_AUDIO_CANDIDATES = [
    os.environ.get("FAHH_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "fahh.mp3"),
    "/var/lib/posterchanai/assets/fahh.mp3",
]
# fahh.mp3 is ~3.24s and ends with a little musical sting — cap the output to drop the last
# ~0.8s so it ends right after the "fahh" (image_audio_to_video applies this as a hard -t cut).
_FAHH_DURATION = 2.4


def _fahh_audio_path() -> str:
    """First existing fahh mp3 from the candidate list ("" if none)."""
    for p in _FAHH_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_fahh(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the fahh clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _fahh_audio_path()
    if not audio:
        raise RuntimeError("Fahh audio (assets/fahh.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_FAHH_DURATION)


def fahh_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a fahh MP4. Mirrors
    depressing_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_fahh(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_fahh.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🌀 Fahh\n\n🌀 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"fahh failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Helpme (turn an image into a 5s MP4 set to the helpme clip — the "helpme" gag)
# ---------------------------------------------------------------------------

# The shipped helpme track (repo-relative), overridable with HELPME_AUDIO_PATH.
_HELPME_AUDIO_CANDIDATES = [
    os.environ.get("HELPME_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "helpme.mp3"),
    "/var/lib/posterchanai/assets/helpme.mp3",
]
_HELPME_DURATION = 5.0


def _helpme_audio_path() -> str:
    """First existing helpme mp3 from the candidate list ("" if none)."""
    for p in _HELPME_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_helpme(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a 5s MP4 playing the helpme clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _helpme_audio_path()
    if not audio:
        raise RuntimeError("Helpme audio (assets/helpme.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_HELPME_DURATION)


def helpme_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a helpme 5s MP4. Mirrors
    fahh_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_helpme(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_helpme.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🆘 Helpme\n\n🆘 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"helpme failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Gong (turn an image into a short MP4 set to the gong clip — the "gong" gag)
# ---------------------------------------------------------------------------

# The shipped gong track (repo-relative), overridable with GONG_AUDIO_PATH.
_GONG_AUDIO_CANDIDATES = [
    os.environ.get("GONG_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "gong.mp3"),
    "/var/lib/posterchanai/assets/gong.mp3",
]
# Cap above the ~4s clip length; -shortest ends the video at the audio end.
_GONG_DURATION = 5.0


def _gong_audio_path() -> str:
    """First existing gong mp3 from the candidate list ("" if none)."""
    for p in _GONG_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_gong(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the gong clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _gong_audio_path()
    if not audio:
        raise RuntimeError("Gong audio (assets/gong.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_GONG_DURATION)


def gong_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a gong MP4. Mirrors
    helpme_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_gong(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_gong.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🔔 Gong\n\n🔔 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"gong failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# FBI (turn an image into a short MP4 set to the "FBI open up" clip — the
# "fbi" gag)
# ---------------------------------------------------------------------------

# The shipped fbi track (repo-relative), overridable with FBI_AUDIO_PATH.
_FBI_AUDIO_CANDIDATES = [
    os.environ.get("FBI_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "fbi.mp3"),
    "/var/lib/posterchanai/assets/fbi.mp3",
]
# Cap above the ~7s clip length; -shortest ends the video at the audio end.
_FBI_DURATION = 8.0


def _fbi_audio_path() -> str:
    """First existing fbi mp3 from the candidate list ("" if none)."""
    for p in _FBI_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_fbi(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the FBI clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _fbi_audio_path()
    if not audio:
        raise RuntimeError("FBI audio (assets/fbi.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_FBI_DURATION)


def fbi_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into an FBI MP4. Mirrors
    gong_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_fbi(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_fbi.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🚨 FBI\n\n🚨 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"fbi failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Redeem (turn an image into a short MP4 set to the "do not redeem it" clip —
# the "redeem" gag)
# ---------------------------------------------------------------------------

# The shipped redeem track (repo-relative), overridable with REDEEM_AUDIO_PATH.
_REDEEM_AUDIO_CANDIDATES = [
    os.environ.get("REDEEM_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "redeem.mp3"),
    "/var/lib/posterchanai/assets/redeem.mp3",
]
# Cap above the ~8s clip length; -shortest ends the video at the audio end.
_REDEEM_DURATION = 9.0


def _redeem_audio_path() -> str:
    """First existing redeem mp3 from the candidate list ("" if none)."""
    for p in _REDEEM_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_redeem(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the redeem clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _redeem_audio_path()
    if not audio:
        raise RuntimeError("Redeem audio (assets/redeem.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_REDEEM_DURATION)


def redeem_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a redeem MP4. Mirrors
    fbi_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_redeem(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_redeem.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 💳 Redeem\n\n💳 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"redeem failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Gigity (turn an image into a short MP4 set to the "giggity" clip — the
# "gigity" gag)
# ---------------------------------------------------------------------------

# The shipped gigity track (repo-relative), overridable with GIGITY_AUDIO_PATH.
_GIGITY_AUDIO_CANDIDATES = [
    os.environ.get("GIGITY_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "gigity.mp3"),
    "/var/lib/posterchanai/assets/gigity.mp3",
]
# Cap above the ~1s clip length; -shortest ends the video at the audio end.
_GIGITY_DURATION = 3.0


def _gigity_audio_path() -> str:
    """First existing gigity mp3 from the candidate list ("" if none)."""
    for p in _GIGITY_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_gigity(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the gigity clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _gigity_audio_path()
    if not audio:
        raise RuntimeError("Gigity audio (assets/gigity.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_GIGITY_DURATION)


def gigity_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a gigity MP4. Mirrors
    redeem_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_gigity(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_gigity.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 😏 Gigity\n\n😏 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"gigity failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Beavis (turn an image into a short MP4 set to the Beavis & Butt-Head laugh —
# the "beavis" gag)
# ---------------------------------------------------------------------------

# The shipped beavis track (repo-relative), overridable with BEAVIS_AUDIO_PATH.
_BEAVIS_AUDIO_CANDIDATES = [
    os.environ.get("BEAVIS_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "beavis.mp3"),
    "/var/lib/posterchanai/assets/beavis.mp3",
]
# Cap above the ~11s clip length; -shortest ends the video at the audio end.
_BEAVIS_DURATION = 12.0


def _beavis_audio_path() -> str:
    """First existing beavis mp3 from the candidate list ("" if none)."""
    for p in _BEAVIS_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_beavis(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the beavis laugh over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _beavis_audio_path()
    if not audio:
        raise RuntimeError("Beavis audio (assets/beavis.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_BEAVIS_DURATION)


def beavis_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a beavis MP4. Mirrors
    gigity_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_beavis(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_beavis.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🤤 Beavis\n\n🤤 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"beavis failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Smell (turn an image into a short MP4 set to the "can you imagine the smell"
# clip — the "smell" gag)
# ---------------------------------------------------------------------------

# The shipped smell track (repo-relative), overridable with SMELL_AUDIO_PATH.
_SMELL_AUDIO_CANDIDATES = [
    os.environ.get("SMELL_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "smell.mp3"),
    "/var/lib/posterchanai/assets/smell.mp3",
]
# Cap above the ~4s clip length; -shortest ends the video at the audio end.
_SMELL_DURATION = 5.0


def _smell_audio_path() -> str:
    """First existing smell mp3 from the candidate list ("" if none)."""
    for p in _SMELL_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_smell(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the smell clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _smell_audio_path()
    if not audio:
        raise RuntimeError("Smell audio (assets/smell.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_SMELL_DURATION)


def smell_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a smell MP4. Mirrors
    beavis_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_smell(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_smell.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 👃 Smell\n\n👃 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"smell failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Hood (turn an image into a short MP4 set to the first 10s of the hood clip —
# the "hood" gag)
# ---------------------------------------------------------------------------

# The shipped hood track (repo-relative), overridable with HOOD_AUDIO_PATH.
_HOOD_AUDIO_CANDIDATES = [
    os.environ.get("HOOD_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "hood.mp3"),
    "/var/lib/posterchanai/assets/hood.mp3",
]
_HOOD_DURATION = 10.0


def _hood_audio_path() -> str:
    """First existing hood mp3 from the candidate list ("" if none)."""
    for p in _HOOD_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_hood(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a 10s MP4 playing the hood clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _hood_audio_path()
    if not audio:
        raise RuntimeError("Hood audio (assets/hood.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_HOOD_DURATION)


def hood_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a hood 10s MP4. Mirrors
    smell_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_hood(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_hood.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🏚️ Hood\n\n🏚️ {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"hood failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Akbar (turn an image into a short MP4 set to the akbar clip — the "akbar" gag)
# ---------------------------------------------------------------------------

# The shipped akbar track (repo-relative), overridable with AKBAR_AUDIO_PATH.
_AKBAR_AUDIO_CANDIDATES = [
    os.environ.get("AKBAR_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "akbar.mp3"),
    "/var/lib/posterchanai/assets/akbar.mp3",
]
# Cap above the ~4.6s clip length; -shortest ends the video at the audio end.
_AKBAR_DURATION = 5.0


def _akbar_audio_path() -> str:
    """First existing akbar mp3 from the candidate list ("" if none)."""
    for p in _AKBAR_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_akbar(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the akbar clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _akbar_audio_path()
    if not audio:
        raise RuntimeError("Akbar audio (assets/akbar.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_AKBAR_DURATION)


def akbar_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into an akbar MP4. Mirrors
    hood_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_akbar(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_akbar.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🕌 Akbar\n\n🕌 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"akbar failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Retard (turn an image into a short MP4 set to the retard-alert clip — the
# "retard" gag)
# ---------------------------------------------------------------------------

# The shipped retard track (repo-relative), overridable with RETARD_AUDIO_PATH.
_RETARD_AUDIO_CANDIDATES = [
    os.environ.get("RETARD_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "retard.mp3"),
    "/var/lib/posterchanai/assets/retard.mp3",
]
# Cap above the ~6.7s clip length; -shortest ends the video at the audio end.
_RETARD_DURATION = 7.5


def _retard_audio_path() -> str:
    """First existing retard mp3 from the candidate list ("" if none)."""
    for p in _RETARD_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_retard(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the retard clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _retard_audio_path()
    if not audio:
        raise RuntimeError("Retard audio (assets/retard.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_RETARD_DURATION)


def retard_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a retard MP4. Mirrors
    akbar_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_retard(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_retard.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## ⚠️ Retard\n\n⚠️ {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"retard failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Whoabuddy (turn an image into a short MP4 set to the "whoa buddy" clip — the
# "whoabuddy" gag)
# ---------------------------------------------------------------------------

# The shipped whoabuddy track (repo-relative), overridable with WHOABUDDY_AUDIO_PATH.
_WHOABUDDY_AUDIO_CANDIDATES = [
    os.environ.get("WHOABUDDY_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "whoabuddy.mp3"),
    "/var/lib/posterchanai/assets/whoabuddy.mp3",
]
# Cap above the ~5.5s clip length; -shortest ends the video at the audio end.
_WHOABUDDY_DURATION = 6.0


def _whoabuddy_audio_path() -> str:
    """First existing whoabuddy mp3 from the candidate list ("" if none)."""
    for p in _WHOABUDDY_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_whoabuddy(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the whoabuddy clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _whoabuddy_audio_path()
    if not audio:
        raise RuntimeError("Whoabuddy audio (assets/whoabuddy.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_WHOABUDDY_DURATION)


def whoabuddy_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a whoabuddy MP4. Mirrors
    retard_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_whoabuddy(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_whoabuddy.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🤠 Whoabuddy\n\n🤠 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"whoabuddy failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Robocop (turn an image into a short MP4 set to the "robocop" clip — the
# "robocop" gag)
# ---------------------------------------------------------------------------

# The shipped robocop track (repo-relative), overridable with ROBOCOP_AUDIO_PATH.
_ROBOCOP_AUDIO_CANDIDATES = [
    os.environ.get("ROBOCOP_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "robocop.mp3"),
    "/var/lib/posterchanai/assets/robocop.mp3",
]
# Cap above the ~12s clip length; -shortest ends the video at the audio end.
_ROBOCOP_DURATION = 13.0


def _robocop_audio_path() -> str:
    """First existing robocop mp3 from the candidate list ("" if none)."""
    for p in _ROBOCOP_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_robocop(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the robocop clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _robocop_audio_path()
    if not audio:
        raise RuntimeError("Robocop audio (assets/robocop.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_ROBOCOP_DURATION)


def robocop_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a robocop MP4. Mirrors
    whoabuddy_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_robocop(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_robocop.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🤖 Robocop\n\n🤖 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"robocop failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# Titan (turn an image into a short MP4 set to the "titan" clip — the
# "titan" gag)
# ---------------------------------------------------------------------------

# The shipped titan track (repo-relative), overridable with TITAN_AUDIO_PATH.
_TITAN_AUDIO_CANDIDATES = [
    os.environ.get("TITAN_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "titan.mp3"),
    "/var/lib/posterchanai/assets/titan.mp3",
]
# Cap above the ~11s clip length; -shortest ends the video at the audio end.
_TITAN_DURATION = 12.0


def _titan_audio_path() -> str:
    """First existing titan mp3 from the candidate list ("" if none)."""
    for p in _TITAN_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_titan(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the titan clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _titan_audio_path()
    if not audio:
        raise RuntimeError("Titan audio (assets/titan.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_TITAN_DURATION)


def titan_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a titan MP4. Mirrors
    whoabuddy_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_titan(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_titan.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🗿 Titan\n\n🗿 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"titan failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# Terminator (turn an image into a short MP4 set to the "terminator" clip — the
# "terminator" gag)
# ---------------------------------------------------------------------------

# The shipped terminator track (repo-relative), overridable with TERMINATOR_AUDIO_PATH.
_TERMINATOR_AUDIO_CANDIDATES = [
    os.environ.get("TERMINATOR_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "terminator.mp3"),
    "/var/lib/posterchanai/assets/terminator.mp3",
]
# Cap above the ~14s clip length; -shortest ends the video at the audio end.
_TERMINATOR_DURATION = 15.0


def _terminator_audio_path() -> str:
    """First existing terminator mp3 from the candidate list ("" if none)."""
    for p in _TERMINATOR_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_terminator(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the terminator clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _terminator_audio_path()
    if not audio:
        raise RuntimeError("Terminator audio (assets/terminator.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_TERMINATOR_DURATION)


def terminator_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a terminator MP4. Mirrors
    whoabuddy_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_terminator(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_terminator.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🦾 Terminator\n\n🦾 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"terminator failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# Reze (turn an image into a short MP4 set to the "reze" clip — the
# "reze" gag)
# ---------------------------------------------------------------------------

# The shipped reze track (repo-relative), overridable with REZE_AUDIO_PATH.
_REZE_AUDIO_CANDIDATES = [
    os.environ.get("REZE_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "reze.mp3"),
    "/var/lib/posterchanai/assets/reze.mp3",
]
# reze uses the OVERLAY path (image_gif_overlay_video), which LOOPS the audio to fill `duration`
# (unlike the audio-clip effects' -shortest). So keep duration AT/UNDER the clip length (~13.0s)
# or the 13s track restarts for the last second and gets cut off (audio "repeats at the end").
_REZE_DURATION = 13.0

# Animated transparent overlay: chibi Makima + Reze dancing (ProRes 4444 .mov for clean alpha).
_REZE_DANCE_CANDIDATES = [
    os.environ.get("REZE_DANCE_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "reze_dance.mov"),
    "/var/lib/posterchanai/assets/reze_dance.mov",
]


def _reze_audio_path() -> str:
    """First existing reze mp3 from the candidate list ("" if none)."""
    for p in _REZE_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def _reze_dance_path() -> str:
    """First existing reze dance overlay (.mov) from the candidate list ("" if none)."""
    for p in _REZE_DANCE_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_reze(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Composite the chibi Makima+Reze dance overlay onto the image, set to the reze clip. MP4 bytes.
    An ANIMATED overlay effect (like chimp/clay) — the transparent dance loops over the image."""
    from app.services.media_service import image_gif_overlay_video
    if isinstance(image_data, list):  # reze is single-image (overlay), not a slideshow
        image_data = image_data[0][1]
    audio = _reze_audio_path()
    if not audio:
        raise RuntimeError("Reze audio (assets/reze.mp3) is missing on the server")
    overlay = _reze_dance_path()
    if not overlay:
        raise RuntimeError("Reze dance overlay (assets/reze_dance.mov) is missing on the server")
    return image_gif_overlay_video(image_data, source_filename, overlay,
                                   duration=_REZE_DURATION, audio_path=audio, height_frac=0.5)


def reze_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a reze MP4. Mirrors
    whoabuddy_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_reze(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_reze.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 💣 Reze\n\n💣 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"reze failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Feliz (turn an image into a short MP4 set to the "feliz" clip)
# ---------------------------------------------------------------------------

# The shipped feliz track (repo-relative), overridable with FELIZ_AUDIO_PATH.
_FELIZ_AUDIO_CANDIDATES = [
    os.environ.get("FELIZ_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "feliz.mp3"),
    "/var/lib/posterchanai/assets/feliz.mp3",
]
# Cap above the ~8s clip length; -shortest ends the video at the audio end.
_FELIZ_DURATION = 9.0


def _feliz_audio_path() -> str:
    """First existing feliz mp3 from the candidate list ("" if none)."""
    for p in _FELIZ_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_feliz(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the feliz clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _feliz_audio_path()
    if not audio:
        raise RuntimeError("Feliz audio (assets/feliz.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_FELIZ_DURATION)


def feliz_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a feliz MP4. Mirrors
    whoabuddy_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_feliz(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_feliz.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🎉 Feliz\n\n🎉 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"feliz failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Prayer (turn an image into a short MP4 set to the prayer clip)
# ---------------------------------------------------------------------------

# The shipped prayer track (repo-relative), overridable with PRAYER_AUDIO_PATH.
_PRAYER_AUDIO_CANDIDATES = [
    os.environ.get("PRAYER_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "prayer.mp3"),
    "/var/lib/posterchanai/assets/prayer.mp3",
]
# Cap just above the ~9.9s clip; -shortest ends the video at the audio end.
_PRAYER_DURATION = 11.0


def _prayer_audio_path() -> str:
    """First existing prayer mp3 from the candidate list ("" if none)."""
    for p in _PRAYER_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_prayer(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the prayer clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _prayer_audio_path()
    if not audio:
        raise RuntimeError("Prayer audio (assets/prayer.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_PRAYER_DURATION)


def prayer_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a prayer MP4 (mirrors whoabuddy_attachments)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_prayer(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_prayer.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🙏 Prayer\n\n🙏 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"prayer failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Glow — generic "make it stand out" enhancement (no gag, no audio): a gentle
# breathing zoom + colour pop + a soft light sweep. A pure-ffmpeg render, so the
# work lives in media_service; this is just the attachment wrapper.
# ---------------------------------------------------------------------------

def glow_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a glow MP4 (breathing zoom + colour pop +
    light sweep). Mirrors whoabuddy_attachments (video output, routed through the
    shared video path)."""
    from app.services.media_service import image_glow_video, images_glow_video
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        if len(images) > 1:
            # Multiple images → each image gets the FULL glow (breathe + colour pop + light
            # sweep), played in order, so EVERY image actually glows (not one sweep across a
            # flat slideshow).
            result = images_glow_video([(fn, d) for fn, d, _ct in images])
            summary = f"## ✨ Glow\n\n✨ {len(images)} images: {_human_size(len(result))}"
        else:
            # A glow-only post reads better a touch longer than the 5s default.
            result = image_glow_video(data, filename, duration=7.0)
            summary = f"## ✨ Glow\n\n✨ {filename}: {_human_size(len(result))}"
        out: OutputFile = {
            "filename": f"{stem}_glow.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        return [out], summary
    except Exception as e:
        logger.error(f"glow failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


_EMOJI_RE = re.compile(
    "[\U0001F000-\U0001FAFF\U00002600-\U000027BF\U0001F1E6-\U0001F1FF"
    "\U00002B00-\U00002BFF\U00002190-\U000021FF\U0000FE00-\U0000FE0F\U0000200D\U000023E9-\U000023FA]+",
    flags=re.UNICODE,
)


def render_glow_text_card(text: str, size: int = 1080) -> bytes:
    """Render `text` as glowing neon type centred on a dark gradient — a "glowing text
    post" graphic (PNG bytes). A bright cyan halo (blurred text screened over the
    background a couple of times for bloom) sits under a crisp near-white core. Used by
    the Telegram post flow's 🌟 Glow button to attach a graphic to a text-only post."""
    from PIL import Image, ImageDraw, ImageFilter, ImageChops
    # Drop emoji / pictographs — the meme font has no glyphs for them, so they'd render
    # as tofu boxes ([]). Collapse the whitespace the removal leaves behind.
    text = _EMOJI_RE.sub("", text or "")
    text = re.sub(r"[ \t]{2,}", " ", text).strip() or " "
    W = H = max(256, int(size))

    # Dark vertical gradient (deep blue -> near-black). Build one column then stretch
    # so it's H iterations, not W*H.
    top, bot = (12, 16, 36), (2, 2, 7)
    col = Image.new("RGB", (1, H))
    cpx = col.load()
    for y in range(H):
        t = y / max(1, H - 1)
        cpx[0, y] = (
            int(top[0] + (bot[0] - top[0]) * t),
            int(top[1] + (bot[1] - top[1]) * t),
            int(top[2] + (bot[2] - top[2]) * t),
        )
    bg = col.resize((W, H))

    # Pick the largest font that fits the text within the safe area.
    margin = int(W * 0.10)
    max_w = W - 2 * margin
    max_h = int(H * 0.74)
    probe = ImageDraw.Draw(bg)
    size_px = int(H * 0.20)
    font = _load_meme_font(size_px)
    lines = _wrap_text_to_width(probe, text, font, max_w)
    while size_px > 24:  # shrink until the wrapped text fits the safe area
        font = _load_meme_font(size_px)
        lines = _wrap_text_to_width(probe, text, font, max_w)
        line_h = int(probe.textbbox((0, 0), "Ag", font=font)[3] * 1.18)
        if line_h * len(lines) <= max_h and all(
            probe.textbbox((0, 0), ln or " ", font=font)[2] <= max_w for ln in lines
        ):
            break
        size_px -= max(6, size_px // 14)
    line_h = int(probe.textbbox((0, 0), "Ag", font=font)[3] * 1.18)
    total_h = line_h * len(lines)
    y0 = (H - total_h) // 2

    neon = (60, 210, 255)  # cyan
    # Neon text on black → blur → screen over the bg twice for a soft bloom halo.
    layer = Image.new("RGB", (W, H), (0, 0, 0))
    ld = ImageDraw.Draw(layer)
    for i, ln in enumerate(lines):
        w = ld.textbbox((0, 0), ln or " ", font=font)[2]
        ld.text(((W - w) // 2, y0 + i * line_h), ln, font=font, fill=neon)
    glow = layer.filter(ImageFilter.GaussianBlur(radius=max(W * 0.012, 6)))
    out = ImageChops.screen(bg, glow)
    out = ImageChops.screen(out, glow)

    # Crisp near-white core with a thin neon stroke so it reads as a lit sign.
    od = ImageDraw.Draw(out)
    stroke = max(2, size_px // 22)
    for i, ln in enumerate(lines):
        w = od.textbbox((0, 0), ln or " ", font=font, stroke_width=stroke)[2]
        od.text(((W - w) // 2, y0 + i * line_h), ln, font=font,
                fill=(235, 250, 255), stroke_width=stroke, stroke_fill=neon)

    buf = io.BytesIO()
    out.save(buf, "PNG")
    return buf.getvalue()


# ---------------------------------------------------------------------------
# Felted tables (turn an image into a short MP4 set to the felted-tables clip)
# ---------------------------------------------------------------------------

# The shipped felted-tables track (repo-relative), overridable with FELTEDTABLES_AUDIO_PATH.
_FELTEDTABLES_AUDIO_CANDIDATES = [
    os.environ.get("FELTEDTABLES_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "feltedtables.mp3"),
    "/var/lib/posterchanai/assets/feltedtables.mp3",
]
# Cap above the ~17s clip length; -shortest ends the video at the audio end.
_FELTEDTABLES_DURATION = 18.0


def _feltedtables_audio_path() -> str:
    """First existing felted-tables mp3 from the candidate list ("" if none)."""
    for p in _FELTEDTABLES_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_feltedtables(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the felted-tables clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _feltedtables_audio_path()
    if not audio:
        raise RuntimeError("Felted-tables audio (assets/feltedtables.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_FELTEDTABLES_DURATION)


def feltedtables_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a felted-tables MP4. Mirrors
    whoabuddy_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_feltedtables(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_feltedtables.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🎱 Felted Tables\n\n🎱 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"feltedtables failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Cheers (turn an image into a short MP4 set to the Cheers theme clip — the
# "cheers" gag)
# ---------------------------------------------------------------------------

# The shipped cheers track (repo-relative), overridable with CHEERS_AUDIO_PATH.
_CHEERS_AUDIO_CANDIDATES = [
    os.environ.get("CHEERS_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "cheers.mp3"),
    "/var/lib/posterchanai/assets/cheers.mp3",
]
# Cap above the ~10s clip length; -shortest ends the video at the audio end.
_CHEERS_DURATION = 11.0


def _cheers_audio_path() -> str:
    """First existing cheers mp3 from the candidate list ("" if none)."""
    for p in _CHEERS_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_cheers(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the Cheers clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _cheers_audio_path()
    if not audio:
        raise RuntimeError("Cheers audio (assets/cheers.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_CHEERS_DURATION)


def cheers_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a cheers MP4. Mirrors
    whoabuddy_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_cheers(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_cheers.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🍻 Cheers\n\n🍻 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"cheers failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Munsters (turn an image into a short MP4 set to the Munsters theme clip — the
# "munsters" gag)
# ---------------------------------------------------------------------------

# The shipped munsters track (repo-relative), overridable with MUNSTERS_AUDIO_PATH.
_MUNSTERS_AUDIO_CANDIDATES = [
    os.environ.get("MUNSTERS_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "munsters.mp3"),
    "/var/lib/posterchanai/assets/munsters.mp3",
]
# Cap above the ~10s clip length; -shortest ends the video at the audio end.
_MUNSTERS_DURATION = 11.0


def _munsters_audio_path() -> str:
    """First existing munsters mp3 from the candidate list ("" if none)."""
    for p in _MUNSTERS_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_munsters(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the Munsters clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _munsters_audio_path()
    if not audio:
        raise RuntimeError("Munsters audio (assets/munsters.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_MUNSTERS_DURATION)


def munsters_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a munsters MP4. Mirrors
    whoabuddy_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_munsters(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_munsters.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🧛 Munsters\n\n🧛 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"munsters failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Happy Days (turn an image into a short MP4 set to the Happy Days theme clip —
# the "happydays" gag)
# ---------------------------------------------------------------------------

# The shipped happydays track (repo-relative), overridable with HAPPYDAYS_AUDIO_PATH.
_HAPPYDAYS_AUDIO_CANDIDATES = [
    os.environ.get("HAPPYDAYS_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "happydays.mp3"),
    "/var/lib/posterchanai/assets/happydays.mp3",
]
# Cap above the ~13s clip length; -shortest ends the video at the audio end.
_HAPPYDAYS_DURATION = 14.0


def _happydays_audio_path() -> str:
    """First existing happydays mp3 from the candidate list ("" if none)."""
    for p in _HAPPYDAYS_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_happydays(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the Happy Days clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _happydays_audio_path()
    if not audio:
        raise RuntimeError("Happy Days audio (assets/happydays.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_HAPPYDAYS_DURATION)


def happydays_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a happydays MP4. Mirrors
    whoabuddy_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_happydays(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_happydays.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🕺 Happy Days\n\n🕺 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"happydays failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Don't Want to Wait (turn an image into a short MP4 set to the Dawson's Creek
# theme clip — the "dontwanttowait" gag)
# ---------------------------------------------------------------------------

# The shipped dontwanttowait track (repo-relative), overridable with DONTWANTTOWAIT_AUDIO_PATH.
_DONTWANTTOWAIT_AUDIO_CANDIDATES = [
    os.environ.get("DONTWANTTOWAIT_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "dontwanttowait.mp3"),
    "/var/lib/posterchanai/assets/dontwanttowait.mp3",
]
# Cap above the ~10s clip length; -shortest ends the video at the audio end.
_DONTWANTTOWAIT_DURATION = 11.0


def _dontwanttowait_audio_path() -> str:
    """First existing dontwanttowait mp3 from the candidate list ("" if none)."""
    for p in _DONTWANTTOWAIT_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_dontwanttowait(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the Dawson's Creek clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _dontwanttowait_audio_path()
    if not audio:
        raise RuntimeError("Don't Want to Wait audio (assets/dontwanttowait.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_DONTWANTTOWAIT_DURATION)


def dontwanttowait_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a dontwanttowait MP4. Mirrors
    whoabuddy_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_dontwanttowait(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_dontwanttowait.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🌊 Don't Want to Wait\n\n🌊 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"dontwanttowait failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Stranger Things (turn an image into a short MP4 set to the Stranger Things
# theme clip — the "strangerthings" gag)
# ---------------------------------------------------------------------------

# The shipped strangerthings track (repo-relative), overridable with STRANGERTHINGS_AUDIO_PATH.
_STRANGERTHINGS_AUDIO_CANDIDATES = [
    os.environ.get("STRANGERTHINGS_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "strangerthings.mp3"),
    "/var/lib/posterchanai/assets/strangerthings.mp3",
]
# Cap above the ~13s clip length; -shortest ends the video at the audio end.
_STRANGERTHINGS_DURATION = 14.0


def _strangerthings_audio_path() -> str:
    """First existing strangerthings mp3 from the candidate list ("" if none)."""
    for p in _STRANGERTHINGS_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_strangerthings(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the Stranger Things clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _strangerthings_audio_path()
    if not audio:
        raise RuntimeError("Stranger Things audio (assets/strangerthings.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_STRANGERTHINGS_DURATION)


def strangerthings_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a strangerthings MP4. Mirrors
    whoabuddy_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_strangerthings(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_strangerthings.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🔦 Stranger Things\n\n🔦 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"strangerthings failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Addams Family (turn an image into a short MP4 set to the Addams Family theme
# clip — the "adamsfamily" gag)
# ---------------------------------------------------------------------------

# The shipped adamsfamily track (repo-relative), overridable with ADAMSFAMILY_AUDIO_PATH.
_ADAMSFAMILY_AUDIO_CANDIDATES = [
    os.environ.get("ADAMSFAMILY_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "adamsfamily.mp3"),
    "/var/lib/posterchanai/assets/adamsfamily.mp3",
]
# Cap above the ~13s clip length; -shortest ends the video at the audio end.
_ADAMSFAMILY_DURATION = 14.0


def _adamsfamily_audio_path() -> str:
    """First existing adamsfamily mp3 from the candidate list ("" if none)."""
    for p in _ADAMSFAMILY_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_adamsfamily(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the Addams Family clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _adamsfamily_audio_path()
    if not audio:
        raise RuntimeError("Addams Family audio (assets/adamsfamily.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_ADAMSFAMILY_DURATION)


def adamsfamily_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into an adamsfamily MP4. Mirrors
    whoabuddy_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_adamsfamily(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_adamsfamily.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🖤 Addams Family\n\n🖤 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"adamsfamily failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# X-Men (turn an image into a short MP4 set to the X-Men theme clip — the
# "xmen" gag)
# ---------------------------------------------------------------------------

# The shipped xmen track (repo-relative), overridable with XMEN_AUDIO_PATH.
_XMEN_AUDIO_CANDIDATES = [
    os.environ.get("XMEN_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "xmen.mp3"),
    "/var/lib/posterchanai/assets/xmen.mp3",
]
# Cap above the ~15s clip length; -shortest ends the video at the audio end.
_XMEN_DURATION = 16.0


def _xmen_audio_path() -> str:
    """First existing xmen mp3 from the candidate list ("" if none)."""
    for p in _XMEN_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_xmen(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the X-Men clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _xmen_audio_path()
    if not audio:
        raise RuntimeError("X-Men audio (assets/xmen.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_XMEN_DURATION)


def xmen_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into an xmen MP4. Mirrors
    whoabuddy_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_xmen(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_xmen.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## ❌ X-Men\n\n❌ {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"xmen failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Futurama (turn an image into a short MP4 set to the Futurama theme clip — the
# "futurama" gag)
# ---------------------------------------------------------------------------

# The shipped futurama track (repo-relative), overridable with FUTURAMA_AUDIO_PATH.
_FUTURAMA_AUDIO_CANDIDATES = [
    os.environ.get("FUTURAMA_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "futurama.mp3"),
    "/var/lib/posterchanai/assets/futurama.mp3",
]
# Cap above the ~11s clip length; -shortest ends the video at the audio end.
_FUTURAMA_DURATION = 12.0


def _futurama_audio_path() -> str:
    """First existing futurama mp3 from the candidate list ("" if none)."""
    for p in _FUTURAMA_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_futurama(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the Futurama clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _futurama_audio_path()
    if not audio:
        raise RuntimeError("Futurama audio (assets/futurama.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_FUTURAMA_DURATION)


def futurama_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a futurama MP4. Mirrors
    whoabuddy_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_futurama(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_futurama.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🚀 Futurama\n\n🚀 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"futurama failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Charlie's Angels (turn an image into a short MP4 set to the Charlie's Angels
# theme clip — the "charliesangles" gag)
# ---------------------------------------------------------------------------

# The shipped charliesangles track (repo-relative), overridable with CHARLIESANGLES_AUDIO_PATH.
_CHARLIESANGLES_AUDIO_CANDIDATES = [
    os.environ.get("CHARLIESANGLES_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "charliesangles.mp3"),
    "/var/lib/posterchanai/assets/charliesangles.mp3",
]
# Cap above the ~12s clip length; -shortest ends the video at the audio end.
_CHARLIESANGLES_DURATION = 13.0


def _charliesangles_audio_path() -> str:
    """First existing charliesangles mp3 from the candidate list ("" if none)."""
    for p in _CHARLIESANGLES_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_charliesangles(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the Charlie's Angels clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _charliesangles_audio_path()
    if not audio:
        raise RuntimeError("Charlie's Angels audio (assets/charliesangles.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_CHARLIESANGLES_DURATION)


def charliesangles_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a charliesangles MP4. Mirrors
    whoabuddy_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_charliesangles(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_charliesangles.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 👼 Charlie's Angels\n\n👼 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"charliesangles failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Diff'rent Strokes (turn an image into a short MP4 set to the Diff'rent Strokes
# theme clip — the "differentstroke" gag)
# ---------------------------------------------------------------------------

# The shipped differentstroke track (repo-relative), overridable with DIFFERENTSTROKE_AUDIO_PATH.
_DIFFERENTSTROKE_AUDIO_CANDIDATES = [
    os.environ.get("DIFFERENTSTROKE_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "differentstroke.mp3"),
    "/var/lib/posterchanai/assets/differentstroke.mp3",
]
# Cap above the ~8s clip length; -shortest ends the video at the audio end.
_DIFFERENTSTROKE_DURATION = 9.0


def _differentstroke_audio_path() -> str:
    """First existing differentstroke mp3 from the candidate list ("" if none)."""
    for p in _DIFFERENTSTROKE_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_differentstroke(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the Diff'rent Strokes clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _differentstroke_audio_path()
    if not audio:
        raise RuntimeError("Diff'rent Strokes audio (assets/differentstroke.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_DIFFERENTSTROKE_DURATION)


def differentstroke_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a differentstroke MP4. Mirrors
    whoabuddy_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_differentstroke(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_differentstroke.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🌍 Diff'rent Strokes\n\n🌍 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"differentstroke failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Seinfeld (turn an image into a short MP4 set to the Seinfeld theme clip — the
# "seinfeld" gag)
# ---------------------------------------------------------------------------

# The shipped seinfeld track (repo-relative), overridable with SEINFELD_AUDIO_PATH.
_SEINFELD_AUDIO_CANDIDATES = [
    os.environ.get("SEINFELD_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "seinfeld.mp3"),
    "/var/lib/posterchanai/assets/seinfeld.mp3",
]
# Cap above the ~13s clip length; -shortest ends the video at the audio end.
_SEINFELD_DURATION = 14.0


def _seinfeld_audio_path() -> str:
    """First existing seinfeld mp3 from the candidate list ("" if none)."""
    for p in _SEINFELD_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_seinfeld(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the Seinfeld clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _seinfeld_audio_path()
    if not audio:
        raise RuntimeError("Seinfeld audio (assets/seinfeld.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_SEINFELD_DURATION)


def seinfeld_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a seinfeld MP4. Mirrors
    whoabuddy_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_seinfeld(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_seinfeld.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🎤 Seinfeld\n\n🎤 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"seinfeld failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# One Piece (turn an image into a short MP4 set to the One Piece theme clip — the
# "onepiece" gag)
# ---------------------------------------------------------------------------

# The shipped onepiece track (repo-relative), overridable with ONEPIECE_AUDIO_PATH.
_ONEPIECE_AUDIO_CANDIDATES = [
    os.environ.get("ONEPIECE_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "onepiece.mp3"),
    "/var/lib/posterchanai/assets/onepiece.mp3",
]
# Cap above the ~10s clip length; -shortest ends the video at the audio end.
_ONEPIECE_DURATION = 11.0


def _onepiece_audio_path() -> str:
    """First existing onepiece mp3 from the candidate list ("" if none)."""
    for p in _ONEPIECE_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_onepiece(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the One Piece clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _onepiece_audio_path()
    if not audio:
        raise RuntimeError("One Piece audio (assets/onepiece.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_ONEPIECE_DURATION)


def onepiece_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a onepiece MP4. Mirrors
    whoabuddy_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_onepiece(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_onepiece.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🏴‍☠️ One Piece\n\n🏴‍☠️ {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"onepiece failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Overtaken (turn an image into a short MP4 set to the "overtaken" clip — the
# "overtaken" gag)
# ---------------------------------------------------------------------------

# The shipped overtaken track (repo-relative), overridable with OVERTAKEN_AUDIO_PATH.
_OVERTAKEN_AUDIO_CANDIDATES = [
    os.environ.get("OVERTAKEN_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "overtaken.mp3"),
    "/var/lib/posterchanai/assets/overtaken.mp3",
]
# Cap above the ~10s clip length; -shortest ends the video at the audio end.
_OVERTAKEN_DURATION = 11.0


def _overtaken_audio_path() -> str:
    """First existing overtaken mp3 from the candidate list ("" if none)."""
    for p in _OVERTAKEN_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_overtaken(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the overtaken clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _overtaken_audio_path()
    if not audio:
        raise RuntimeError("Overtaken audio (assets/overtaken.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_OVERTAKEN_DURATION)


def overtaken_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into an overtaken MP4. Mirrors
    whoabuddy_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_overtaken(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_overtaken.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🏎️ Overtaken\n\n🏎️ {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"overtaken failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Sopranos (turn an image into a short MP4 set to the Sopranos theme clip — the
# "sopranos" gag)
# ---------------------------------------------------------------------------

# The shipped sopranos track (repo-relative), overridable with SOPRANOS_AUDIO_PATH.
_SOPRANOS_AUDIO_CANDIDATES = [
    os.environ.get("SOPRANOS_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "sopranos.mp3"),
    "/var/lib/posterchanai/assets/sopranos.mp3",
]
# Cap above the ~13s clip length; -shortest ends the video at the audio end.
_SOPRANOS_DURATION = 14.0


def _sopranos_audio_path() -> str:
    """First existing sopranos mp3 from the candidate list ("" if none)."""
    for p in _SOPRANOS_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_sopranos(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the Sopranos clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _sopranos_audio_path()
    if not audio:
        raise RuntimeError("Sopranos audio (assets/sopranos.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_SOPRANOS_DURATION)


def sopranos_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a sopranos MP4. Mirrors
    whoabuddy_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_sopranos(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_sopranos.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🇮🇹 Sopranos\n\n🇮🇹 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"sopranos failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Freebird (turn an image into an MP4 set to the Free Bird solo — the "freebird" gag)
# ---------------------------------------------------------------------------

# The shipped freebird track (repo-relative), overridable with FREEBIRD_AUDIO_PATH.
_FREEBIRD_AUDIO_CANDIDATES = [
    os.environ.get("FREEBIRD_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "freebird.mp3"),
    "/var/lib/posterchanai/assets/freebird.mp3",
]
# Cap above the ~13s clip length; -shortest ends the video at the audio end.
_FREEBIRD_DURATION = 14.0


def _freebird_audio_path() -> str:
    """First existing freebird mp3 from the candidate list ("" if none)."""
    for p in _FREEBIRD_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_freebird(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the Free Bird solo over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _freebird_audio_path()
    if not audio:
        raise RuntimeError("Freebird audio (assets/freebird.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_FREEBIRD_DURATION)


def freebird_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a freebird MP4. Mirrors
    whoabuddy_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_freebird(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_freebird.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🦅 Freebird\n\n🦅 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"freebird failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Kanye (turn an image into an MP4 set to the Kanye clip — the "kanye" gag)
# ---------------------------------------------------------------------------

# The shipped kanye track (repo-relative), overridable with KANYE_AUDIO_PATH.
_KANYE_AUDIO_CANDIDATES = [
    os.environ.get("KANYE_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "kanye.mp3"),
    "/var/lib/posterchanai/assets/kanye.mp3",
]
# Cap above the ~9s clip length; -shortest ends the video at the audio end.
_KANYE_DURATION = 10.0


def _kanye_audio_path() -> str:
    """First existing kanye mp3 from the candidate list ("" if none)."""
    for p in _KANYE_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_kanye(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the Kanye clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _kanye_audio_path()
    if not audio:
        raise RuntimeError("Kanye audio (assets/kanye.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_KANYE_DURATION)


def kanye_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a kanye MP4. Mirrors
    freebird_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_kanye(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_kanye.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🐻 Kanye\n\n🐻 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"kanye failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Darkness (turn an image into an MP4 set to the darkness clip — the "darkness" gag)
# ---------------------------------------------------------------------------

# The shipped darkness track (repo-relative), overridable with DARKNESS_AUDIO_PATH.
_DARKNESS_AUDIO_CANDIDATES = [
    os.environ.get("DARKNESS_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "darkness.mp3"),
    "/var/lib/posterchanai/assets/darkness.mp3",
]
# Cap above the ~16s clip length; -shortest ends the video at the audio end.
_DARKNESS_DURATION = 17.0


def _darkness_audio_path() -> str:
    """First existing darkness mp3 from the candidate list ("" if none)."""
    for p in _DARKNESS_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_darkness(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the darkness clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _darkness_audio_path()
    if not audio:
        raise RuntimeError("Darkness audio (assets/darkness.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_DARKNESS_DURATION)


def darkness_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a darkness MP4. Mirrors
    kanye_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_darkness(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_darkness.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🌑 Darkness\n\n🌑 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"darkness failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Bike (turn an image into an MP4 set to the bike clip — the "bike" gag)
# ---------------------------------------------------------------------------

# The shipped bike track (repo-relative), overridable with BIKE_AUDIO_PATH.
_BIKE_AUDIO_CANDIDATES = [
    os.environ.get("BIKE_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "bike.mp3"),
    "/var/lib/posterchanai/assets/bike.mp3",
]
# Cap above the ~12s clip length; -shortest ends the video at the audio end.
_BIKE_DURATION = 13.0


def _bike_audio_path() -> str:
    """First existing bike mp3 from the candidate list ("" if none)."""
    for p in _BIKE_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_bike(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the bike clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _bike_audio_path()
    if not audio:
        raise RuntimeError("Bike audio (assets/bike.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_BIKE_DURATION)


def bike_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a bike MP4. Mirrors
    darkness_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_bike(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_bike.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🚲 Bike\n\n🚲 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"bike failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Jobs (turn an image into an MP4 set to the "they took our jobs" clip — "jobs" gag)
# ---------------------------------------------------------------------------

# The shipped jobs track (repo-relative), overridable with JOBS_AUDIO_PATH.
_JOBS_AUDIO_CANDIDATES = [
    os.environ.get("JOBS_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "jobs.mp3"),
    "/var/lib/posterchanai/assets/jobs.mp3",
]
# Cap above the ~13s clip length; -shortest ends the video at the audio end.
_JOBS_DURATION = 14.0


def _jobs_audio_path() -> str:
    """First existing jobs mp3 from the candidate list ("" if none)."""
    for p in _JOBS_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_jobs(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the jobs clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _jobs_audio_path()
    if not audio:
        raise RuntimeError("Jobs audio (assets/jobs.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_JOBS_DURATION)


def jobs_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a jobs MP4. Mirrors
    bike_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_jobs(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_jobs.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 💼 Jobs\n\n💼 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"jobs failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Ree (turn an image into an MP4 set to the REEEE clip — the "ree" gag)
# ---------------------------------------------------------------------------

# The shipped ree track (repo-relative), overridable with REE_AUDIO_PATH.
_REE_AUDIO_CANDIDATES = [
    os.environ.get("REE_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "ree.mp3"),
    "/var/lib/posterchanai/assets/ree.mp3",
]
# Cap above the ~6s clip length; -shortest ends the video at the audio end.
_REE_DURATION = 7.0


def _ree_audio_path() -> str:
    """First existing ree mp3 from the candidate list ("" if none)."""
    for p in _REE_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_ree(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the ree clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _ree_audio_path()
    if not audio:
        raise RuntimeError("Ree audio (assets/ree.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_REE_DURATION)


def ree_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a ree MP4. Mirrors
    jobs_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_ree(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_ree.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 😡 Ree\n\n😡 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"ree failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Liberal (turn an image into an MP4 set to the liberal clip — the "liberal" gag)
# ---------------------------------------------------------------------------

# The shipped liberal track (repo-relative), overridable with LIBERAL_AUDIO_PATH.
_LIBERAL_AUDIO_CANDIDATES = [
    os.environ.get("LIBERAL_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "liberal.mp3"),
    "/var/lib/posterchanai/assets/liberal.mp3",
]
# Cap above the ~11s clip length; -shortest ends the video at the audio end.
_LIBERAL_DURATION = 12.0


def _liberal_audio_path() -> str:
    """First existing liberal mp3 from the candidate list ("" if none)."""
    for p in _LIBERAL_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_liberal(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the liberal clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _liberal_audio_path()
    if not audio:
        raise RuntimeError("Liberal audio (assets/liberal.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_LIBERAL_DURATION)


def liberal_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a liberal MP4. Mirrors
    ree_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_liberal(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_liberal.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🗽 Liberal\n\n🗽 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"liberal failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Moving (turn an image into an MP4 set to the moving clip — the "moving" gag)
# ---------------------------------------------------------------------------

# The shipped moving track (repo-relative), overridable with MOVING_AUDIO_PATH.
_MOVING_AUDIO_CANDIDATES = [
    os.environ.get("MOVING_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "moving.mp3"),
    "/var/lib/posterchanai/assets/moving.mp3",
]
# Cap above the ~11s clip length; -shortest ends the video at the audio end.
_MOVING_DURATION = 12.0


def _moving_audio_path() -> str:
    """First existing moving mp3 from the candidate list ("" if none)."""
    for p in _MOVING_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_moving(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the moving clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _moving_audio_path()
    if not audio:
        raise RuntimeError("Moving audio (assets/moving.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_MOVING_DURATION)


def moving_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a moving MP4. Mirrors
    liberal_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_moving(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_moving.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 📦 Moving\n\n📦 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"moving failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Consider (overlay the chroma-keyed "consider the following" cutout — "consider")
# ---------------------------------------------------------------------------

# The shipped consider cutout (transparent PNG), overridable with CONSIDER_PNG_PATH.
_CONSIDER_PNG_CANDIDATES = [
    os.environ.get("CONSIDER_PNG_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "consider.png"),
    "/var/lib/posterchanai/assets/consider.png",
]


def _consider_png_path() -> str:
    """First existing consider png from the candidate list ("" if none)."""
    for p in _CONSIDER_PNG_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_consider(data: bytes) -> bytes:
    """Composite the (transparent) "consider the following" cutout over an image,
    scaled large and anchored to the bottom-right. Returns JPEG bytes."""
    from PIL import Image, ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except Exception:
        pass

    png = _consider_png_path()
    if not png:
        raise RuntimeError("Consider cutout (assets/consider.png) is missing on the server")

    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            background = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            background.paste(rgba, mask=rgba.split()[-1])
            img = background
        elif img.mode != "RGB":
            img = img.convert("RGB")

        W, H = img.size
        img = img.convert("RGBA")
        with Image.open(png) as ov_src:
            ov = ov_src.convert("RGBA")
        ow, oh = ov.size
        # Scale the cutout to ~80% of the image height, but never wider than 95% of
        # the image (so it still fits on portrait images), keeping its aspect ratio.
        scale = min(H * 0.80 / oh, W * 0.95 / ow)
        nw, nh = max(int(ow * scale), 1), max(int(oh * scale), 1)
        ov = ov.resize((nw, nh), Image.LANCZOS)
        # Anchor to the bottom-right corner (small margin).
        margin = max(int(W * 0.01), 2)
        x, y = W - nw - margin, H - nh - margin
        img.alpha_composite(ov, (max(x, 0), max(y, 0)))

        img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue()


def consider_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Overlay the consider cutout on the first image attachment. Mirrors
    meme_attachments (image output, one shared delivery path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_consider(data)
        out = _alive_or_still(result, stem, "consider")
        summary = f"## 🤔 Consider\n\n🤔 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"consider failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Clay (overlay the background-removed Clay Davis "Shiiiit" clip — the "clay" gag)
# ---------------------------------------------------------------------------

# The shipped clay overlay (alpha video) + soundtrack, overridable via env.
_CLAY_OVERLAY_CANDIDATES = [
    os.environ.get("CLAY_OVERLAY_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "clay.mov"),
    "/var/lib/posterchanai/assets/clay.mov",
]
_CLAY_AUDIO_CANDIDATES = [
    os.environ.get("CLAY_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "clay.mp3"),
    "/var/lib/posterchanai/assets/clay.mp3",
]
# Clay's shot only (0:05.5 → the scene-cut at ~0:08.0, before it cuts to the other
# character); ~2.58s, one play.
_CLAY_DURATION = 2.6


def _clay_overlay_path() -> str:
    """First existing clay overlay video from the candidate list ("" if none)."""
    for p in _CLAY_OVERLAY_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def _clay_audio_path() -> str:
    """First existing clay mp3 from the candidate list ("" if none)."""
    for p in _CLAY_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_clay(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Composite the background-removed Clay Davis clip over an image, with its
    "Shiiiit" soundtrack. MP4 bytes."""
    from app.services.media_service import image_gif_overlay_video
    overlay = _clay_overlay_path()
    if not overlay:
        raise RuntimeError("Clay overlay (assets/clay.mov) is missing on the server")
    return image_gif_overlay_video(image_data, source_filename, overlay,
                                   duration=_CLAY_DURATION, audio_path=_clay_audio_path() or None,
                                   height_frac=0.95)


def clay_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Overlay the clay clip on the first image attachment. Mirrors chimp_attachments
    (animated video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_clay(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_clay.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🗣️ Clay\n\n🗣️ {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"clay failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Harlem (turn an image into an MP4 set to the Harlem Shake clip — "harlem" gag)
# ---------------------------------------------------------------------------

# The shipped harlem track (repo-relative), overridable with HARLEM_AUDIO_PATH.
_HARLEM_AUDIO_CANDIDATES = [
    os.environ.get("HARLEM_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "harlem.mp3"),
    "/var/lib/posterchanai/assets/harlem.mp3",
]
# Cap above the ~12s clip length; -shortest ends the video at the audio end.
_HARLEM_DURATION = 13.0


def _harlem_audio_path() -> str:
    """First existing harlem mp3 from the candidate list ("" if none)."""
    for p in _HARLEM_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_harlem(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the Harlem Shake clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _harlem_audio_path()
    if not audio:
        raise RuntimeError("Harlem audio (assets/harlem.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_HARLEM_DURATION)


def harlem_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a harlem MP4. Mirrors
    moving_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_harlem(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_harlem.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🕺 Harlem\n\n🕺 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"harlem failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Chimp (overlay an animated transparent GIF on the lower third — the "chimp" gag)
# ---------------------------------------------------------------------------

# The shipped chimp gif (repo-relative), overridable with CHIMP_GIF_PATH.
_CHIMP_GIF_CANDIDATES = [
    os.environ.get("CHIMP_GIF_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "chimp.gif"),
    "/var/lib/posterchanai/assets/chimp.gif",
]
# Optional chimp soundtrack (repo-relative), overridable with CHIMP_AUDIO_PATH.
_CHIMP_AUDIO_CANDIDATES = [
    os.environ.get("CHIMP_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "chimp.mp3"),
    "/var/lib/posterchanai/assets/chimp.mp3",
]
# Safety cap on the looping overlay video; with audio, `-shortest` ends it with the track.
_CHIMP_DURATION = 6.0


def _chimp_gif_path() -> str:
    """First existing chimp gif from the candidate list ("" if none)."""
    for p in _CHIMP_GIF_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def _chimp_audio_path() -> str:
    """First existing chimp mp3 from the candidate list ("" if none)."""
    for p in _CHIMP_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_chimp(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Composite the animated chimp gif over the lower third of an image, with its
    soundtrack if present. MP4 bytes."""
    from app.services.media_service import image_gif_overlay_video
    gif = _chimp_gif_path()
    if not gif:
        raise RuntimeError("Chimp gif (assets/chimp.gif) is missing on the server")
    return image_gif_overlay_video(image_data, source_filename, gif,
                                   duration=_CHIMP_DURATION, audio_path=_chimp_audio_path() or None)


def chimp_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Overlay the chimp gif on the first image attachment. Mirrors the audio
    effects' shape (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_chimp(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_chimp.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🐵 Chimp\n\n🐵 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"chimp failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Wasteland (turn an image into an MP4 set to the Baba O'Riley intro — "wasteland")
# ---------------------------------------------------------------------------

# The shipped wasteland track (repo-relative), overridable with WASTELAND_AUDIO_PATH.
_WASTELAND_AUDIO_CANDIDATES = [
    os.environ.get("WASTELAND_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "wasteland.mp3"),
    "/var/lib/posterchanai/assets/wasteland.mp3",
]
# Cap above the ~12s clip (0:40–0:52); -shortest ends the video at the audio end.
_WASTELAND_DURATION = 13.0


def _wasteland_audio_path() -> str:
    """First existing wasteland mp3 from the candidate list ("" if none)."""
    for p in _WASTELAND_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_wasteland(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the Teenage Wasteland intro. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _wasteland_audio_path()
    if not audio:
        raise RuntimeError("Wasteland audio (assets/wasteland.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_WASTELAND_DURATION)


def wasteland_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a wasteland MP4. Mirrors
    harlem_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_wasteland(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_wasteland.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🎸 Wasteland\n\n🎸 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"wasteland failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Mixalot (turn an image into an MP4 set to the Baby Got Back clip — "mixalot")
# ---------------------------------------------------------------------------

# The shipped mixalot track (repo-relative), overridable with MIXALOT_AUDIO_PATH.
_MIXALOT_AUDIO_CANDIDATES = [
    os.environ.get("MIXALOT_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "mixalot.mp3"),
    "/var/lib/posterchanai/assets/mixalot.mp3",
]
# Cap above the ~9s clip (0:29–0:38); -shortest ends the video at the audio end.
_MIXALOT_DURATION = 10.0


def _mixalot_audio_path() -> str:
    """First existing mixalot mp3 from the candidate list ("" if none)."""
    for p in _MIXALOT_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_mixalot(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the Baby Got Back clip. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _mixalot_audio_path()
    if not audio:
        raise RuntimeError("Mixalot audio (assets/mixalot.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_MIXALOT_DURATION)


def mixalot_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a mixalot MP4. Mirrors
    wasteland_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_mixalot(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_mixalot.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🍑 Mixalot\n\n🍑 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"mixalot failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Thug (turn an image into an MP4 set to the THUG LIFE clip — the "thug" gag)
# ---------------------------------------------------------------------------

# The shipped thug track (repo-relative), overridable with THUG_AUDIO_PATH.
_THUG_AUDIO_CANDIDATES = [
    os.environ.get("THUG_AUDIO_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "thug.mp3"),
    "/var/lib/posterchanai/assets/thug.mp3",
]
# Cap above the ~17.5s clip length; -shortest ends the video at the audio end.
_THUG_DURATION = 18.0


def _thug_audio_path() -> str:
    """First existing thug mp3 from the candidate list ("" if none)."""
    for p in _THUG_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def _make_deal_with_it_glasses(target_w: int, target_h: int):
    """Pixel-art 'deal with it' black sunglasses, sized to a face's eye band.
    Drawn small then scaled NEAREST for the 8-bit look. Returns RGBA Image."""
    from PIL import Image, ImageDraw
    cw, ch = 80, 30
    g = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    d = ImageDraw.Draw(g)
    black = (12, 12, 12, 255)
    d.rectangle([0, 4, cw, 9], fill=black)          # temple bar across the top
    d.rectangle([6, 8, 34, 26], fill=black)         # left lens
    d.rectangle([46, 8, 74, 26], fill=black)        # right lens
    d.rectangle([34, 11, 46, 15], fill=black)       # bridge
    return g.resize((max(2, target_w), max(2, target_h)), Image.NEAREST)


# Anime-face detector (nagadomi's lbpcascade), so the THUG overlay also lands on
# anime/illustrated faces that the real-face haarcascade misses.
_ANIME_CASCADE_CANDIDATES = [
    os.environ.get("ANIME_CASCADE_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "lbpcascade_animeface.xml"),
    "/var/lib/posterchanai/assets/lbpcascade_animeface.xml",
]


def _anime_cascade_path() -> str:
    """First existing anime-face cascade from the candidate list ("" if none)."""
    for p in _ANIME_CASCADE_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def _detect_thug_faces(gray, anime_gray, im_w: int, im_h: int):
    """Run the real-face + anime-face cascades and return de-duplicated boxes as
    (x, y, w, h, kind) where kind is 'real' or 'anime' (their feature geometry
    differs — anime mouths sit much higher in the box). Anime detection uses a
    histogram-equalized gray (nagadomi's recommendation)."""
    import cv2
    # A meme subject's face is large; a higher floor + neighbour count keeps the
    # anime cascade from stamping spurious tiny "faces" on hands/background.
    min_side = (max(40, im_w // 12), max(40, im_h // 12))
    boxes = []
    real = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if not real.empty():
        boxes += [(b, "real") for b in real.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=min_side)]
    anime_xml = _anime_cascade_path()
    if anime_xml:
        anime = cv2.CascadeClassifier(anime_xml)
        if not anime.empty():
            # minNeighbors=3 is nagadomi's default; 5 missed many real faces. The
            # min_side floor (above) is what suppresses spurious tiny detections.
            boxes += [(b, "anime") for b in anime.detectMultiScale(anime_gray, scaleFactor=1.1, minNeighbors=3, minSize=min_side)]
    # Drop boxes whose centre falls inside an already-accepted box (same face hit
    # by both cascades). Prefer a 'real' hit over an 'anime' one on the same face
    # (real geometry is correct for photos; true anime never trips the real
    # cascade), then larger boxes first.
    accepted = []
    for (x, y, w, h), kind in sorted(boxes, key=lambda bk: (bk[1] != "real", -(bk[0][2] * bk[0][3]))):
        cx, cy = x + w / 2, y + h / 2
        if any(ax <= cx <= ax + aw and ay <= cy <= ay + ah for (ax, ay, aw, ah, _) in accepted):
            continue
        accepted.append((x, y, w, h, kind))
    return accepted


def _draw_joint(overlay_draw, x0: float, y0: float, length: float, th: float, angle_deg: float = 18.0):
    """Draw a lit joint on an RGBA overlay: filter end anchored at (x0, y0)
    (the mouth), drooping down-right, with a glowing ember and smoke."""
    import math
    a = math.radians(angle_deg)
    dx, dy = math.cos(a), math.sin(a)
    x1, y1 = x0 + length * dx, y0 + length * dy            # ember (far) end
    fx, fy = x0 + length * 0.18 * dx, y0 + length * 0.18 * dy
    overlay_draw.line([(x0, y0), (x1, y1)], fill=(245, 240, 230, 255), width=int(th))   # paper
    overlay_draw.line([(x0, y0), (fx, fy)], fill=(120, 80, 40, 255), width=int(th))     # filter (mouth end)
    r = th * 0.7
    overlay_draw.ellipse([x1 - r, y1 - r, x1 + r, y1 + r], fill=(255, 90, 20, 255))     # ember glow
    overlay_draw.ellipse([x1 - r * 0.6, y1 - r * 0.6, x1 + r * 0.6, y1 + r * 0.6], fill=(255, 190, 60, 255))
    for i, (rr, al) in enumerate([(th * 0.6, 150), (th * 0.9, 110), (th * 1.2, 70)]):   # smoke rising
        cx, cy = x1, y1 - (i + 1) * th * 1.2
        overlay_draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=(225, 225, 225, al))


_INSIGHTFACE_APP = None
_INSIGHTFACE_TRIED = False


def _insightface_app():
    """Lazily build a detection-only InsightFace app (SCRFD gives 5 landmarks:
    eyes, nose, mouth corners). Cached; returns None if unavailable."""
    global _INSIGHTFACE_APP, _INSIGHTFACE_TRIED
    if _INSIGHTFACE_TRIED:
        return _INSIGHTFACE_APP
    _INSIGHTFACE_TRIED = True
    try:
        import warnings
        warnings.filterwarnings("ignore")
        from insightface.app import FaceAnalysis
        app = FaceAnalysis(name="buffalo_l", allowed_modules=["detection"],
                           providers=["CPUExecutionProvider"])
        app.prepare(ctx_id=-1, det_size=(640, 640))
        _INSIGHTFACE_APP = app
    except Exception as e:
        logger.warning(f"insightface unavailable for thug overlay: {e}")
        _INSIGHTFACE_APP = None
    return _INSIGHTFACE_APP


def _draw_thug_from_landmarks(im, kps, ImageDraw):
    """Stamp glasses across the two eye landmarks (rotation-aware) and a joint at
    the mouth centre (between the mouth-corner landmarks). `kps` is SCRFD's 5×2:
    [left eye, right eye, nose, left mouth, right mouth]."""
    import math
    from PIL import Image
    le, re, lm, rm = kps[0], kps[1], kps[3], kps[4]
    ipd = math.hypot(re[0] - le[0], re[1] - le[1]) or 1.0
    midx, midy = (le[0] + re[0]) / 2, (le[1] + re[1]) / 2
    ang = math.degrees(math.atan2(re[1] - le[1], re[0] - le[0]))
    gw, gh = max(8, int(ipd * 2.1)), max(8, int(ipd * 0.62))
    glasses = _make_deal_with_it_glasses(gw, gh).rotate(-ang, expand=True, resample=Image.BICUBIC)
    im.paste(glasses, (int(midx - glasses.width / 2), int(midy - glasses.height / 2)), glasses)
    mx, my = (lm[0] + rm[0]) / 2, (lm[1] + rm[1]) / 2
    overlay = Image.new("RGBA", im.size, (0, 0, 0, 0))
    _draw_joint(ImageDraw.Draw(overlay), mx - 0.15 * ipd, my, ipd * 1.7, max(5, int(ipd * 0.17)))
    return Image.alpha_composite(im.convert("RGBA"), overlay).convert("RGB")


def _apply_thug_face(image_data: bytes) -> bytes:
    """Detect a face and stamp pixel sunglasses over the eyes + a lit joint at the
    mouth (the classic THUG LIFE overlay). Tries InsightFace landmarks first
    (exact, handles ¾ views), then falls back to the haar/anime cascades for flat
    2D art. Returns JPEG bytes; on no face or any error returns the original."""
    try:
        import cv2
        import numpy as np
        from PIL import Image, ImageOps, ImageDraw
    except Exception as e:
        logger.warning(f"thug face overlay unavailable ({e}); skipping")
        return image_data

    # Preferred path: InsightFace landmarks (precise eyes + mouth).
    app = _insightface_app()
    if app is not None:
        try:
            with Image.open(io.BytesIO(image_data)) as im0:
                im = ImageOps.exif_transpose(im0).convert("RGB")
            bgr = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
            dets = app.get(bgr)
            if dets:
                f = max(dets, key=lambda d: (d.bbox[2] - d.bbox[0]) * (d.bbox[3] - d.bbox[1]))
                if getattr(f, "kps", None) is not None and len(f.kps) >= 5:
                    out_img = _draw_thug_from_landmarks(im, f.kps, ImageDraw)
                    out = io.BytesIO()
                    out_img.save(out, format="JPEG", quality=92)
                    return out.getvalue()
        except Exception as e:
            logger.warning(f"insightface thug path failed, falling back: {e}")
    try:
        with Image.open(io.BytesIO(image_data)) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            gray = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2GRAY)
            faces = _detect_thug_faces(gray, cv2.equalizeHist(gray), im.width, im.height)
            if not faces:
                return image_data
            # Thug memes target one subject; keep only the largest face so stray
            # cascade hits on hands/collars/background don't get stamped too.
            faces = [max(faces, key=lambda f: f[2] * f[3])]
            eye_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_eye.xml")
            # Joint + smoke go on an RGBA overlay so the smoke can be translucent.
            overlay = Image.new("RGBA", im.size, (0, 0, 0, 0))
            od = ImageDraw.Draw(overlay)
            for (x, y, w, h, kind) in faces:
                # Anchor on the actual eye line — far more robust across art styles
                # than a fixed fraction (boxes vary in how much hat/forehead/neck
                # they include). Fall back to a fraction if no eyes are found.
                eye_cy = None
                if not eye_cascade.empty():
                    eyes = eye_cascade.detectMultiScale(
                        gray[y:y + h, x:x + w], scaleFactor=1.05, minNeighbors=3,
                        minSize=(max(8, int(w * 0.10)), max(6, int(h * 0.06))),
                    )
                    eyes = [e for e in eyes if (e[1] + e[3] / 2) < 0.6 * h]  # ignore nostril/mouth hits
                    if eyes:
                        eye_cy = y + sum(e[1] + e[3] / 2 for e in eyes) / len(eyes)
                if eye_cy is None:
                    eye_cy = y + (0.42 if kind == "anime" else 0.40) * h
                # Sunglasses centred on the eye line.
                gw, gh = int(w * 1.04), max(8, int(h * 0.26))
                glasses = _make_deal_with_it_glasses(gw, gh)
                im.paste(glasses, (x + (w - gw) // 2, int(eye_cy - gh / 2)), glasses)
                # Joint at the mouth, a kind-specific drop below the eyes (anime
                # faces have a much shorter eyes→mouth distance than real ones).
                mouth_y = eye_cy + (0.21 if kind == "anime" else 0.38) * h
                _draw_joint(od, x + 0.40 * w, mouth_y, w * 0.72, max(5, int(h * 0.045)))
            im = Image.alpha_composite(im.convert("RGBA"), overlay).convert("RGB")
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=92)
            return out.getvalue()
    except Exception as e:
        logger.warning(f"thug face overlay failed, using bare image: {e}")
        return image_data


def add_thug(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Stamp the THUG LIFE meme (pixel sunglasses + joint on the detected face),
    burn the "THUG LIFE" caption over the lower third, then turn it into an MP4
    with the THUG LIFE clip over it. MP4 bytes. Each step falls back to the prior
    image so a missing face or font never aborts it."""
    from app.services.media_service import image_audio_to_video
    audio = _thug_audio_path()
    if not audio:
        raise RuntimeError("Thug audio (assets/thug.mp3) is missing on the server")
    faced = _apply_thug_face(image_data)
    try:
        faced = add_meme_text(faced, "THUG LIFE")
    except Exception as e:
        logger.warning(f"thug caption failed, using bare image: {e}")
    return image_audio_to_video(faced, "image.jpg", audio, duration=_THUG_DURATION)


def thug_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a THUG LIFE MP4 (bakes its own
    "THUG LIFE" caption — it does not take a custom `meme` caption)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_thug(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_thug.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 😎 Thug\n\n😎 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"thug failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


# ---------------------------------------------------------------------------
# Motion subcommands (trailing arg, e.g. `dildo zoom` / `dildo shake`) — applied
# to ANY effect's output: an image becomes a short motion video; an audio/video
# effect keeps its audio while its still frame moves.
# ---------------------------------------------------------------------------

# How long the motion runs for an image effect (audio effects use their own
# length so the motion completes over the whole clip).
_MOTION_IMAGE_DURATION = 4.0


def _apply_motion(outputs: List[OutputFile], suffix: str, image_fn, video_fn) -> List[OutputFile]:
    """Replace each effect output with a motion version. Images turn into a short
    motion MP4 (via `image_fn`); existing videos are re-rendered with the same
    motion keeping their audio (via `video_fn`). Original kept on failure."""
    result_files: List[OutputFile] = []
    for out in outputs or []:
        ct = (out.get("content_type") or "").lower()
        fn = out.get("filename") or "image"
        stem = Path(fn).stem or "image"
        try:
            if ct.startswith("image/"):
                video = image_fn(out["data"], fn, duration=_MOTION_IMAGE_DURATION)
            elif ct.startswith("video/"):
                video = video_fn(out["data"], fn)
            else:
                result_files.append(out)
                continue
            result_files.append({
                "filename": f"{stem}_{suffix}.mp4",
                "data": video,
                "content_type": "video/mp4",
            })
        except Exception as e:
            logger.error(f"{suffix} failed for {fn}: {e}", exc_info=True)
            result_files.append(out)
    return result_files


def apply_zoom(outputs: List[OutputFile]) -> List[OutputFile]:
    """Ken Burns zoom-out pan applied to each effect output (the `zoom` arg)."""
    from app.services.media_service import image_zoompan_video, zoom_existing_video
    return _apply_motion(outputs, "zoom", image_zoompan_video, zoom_existing_video)


def apply_shake(outputs: List[OutputFile]) -> List[OutputFile]:
    """Camera-shake motion applied to each effect output (the `shake` arg)."""
    from app.services.media_service import image_shake_video, shake_existing_video
    return _apply_motion(outputs, "shake", image_shake_video, shake_existing_video)


def apply_medshake(outputs: List[OutputFile]) -> List[OutputFile]:
    """Gentler camera-shake motion applied to each effect output (the `medshake` arg)."""
    from app.services.media_service import image_medshake_video, medshake_existing_video
    return _apply_motion(outputs, "medshake", image_medshake_video, medshake_existing_video)


def apply_beginshake(outputs: List[OutputFile]) -> List[OutputFile]:
    """Shake-then-settle motion applied to each effect output (the `beginshake` arg)."""
    from app.services.media_service import image_beginshake_video, beginshake_existing_video
    return _apply_motion(outputs, "beginshake", image_beginshake_video, beginshake_existing_video)


def apply_trippy(outputs: List[OutputFile]) -> List[OutputFile]:
    """Psychedelic hue-cycle applied to each effect output (the `trippy` arg).

    Images become a hue-cycling clip; existing videos are RE-coloured frame-by-frame
    (not frozen), so trippy composes on top of a zoom/shake/pulse motion."""
    from app.services.media_service import image_trippy_video, recolor_existing_video
    return _apply_motion(outputs, "trippy", image_trippy_video, recolor_existing_video)


def apply_pulse(outputs: List[OutputFile]) -> List[OutputFile]:
    """Rhythmic zoom-pulse applied to each effect output (the `pulse` arg)."""
    from app.services.media_service import image_pulse_video, pulse_existing_video
    return _apply_motion(outputs, "pulse", image_pulse_video, pulse_existing_video)


def apply_glow(outputs: List[OutputFile]) -> List[OutputFile]:
    """Glow look applied to each effect output (the `glow` modifier, e.g. `alive glow`).

    Images become the full glow clip (breathing zoom + colour pop + light sweep); existing
    videos get the colour pop + light sweep over their real frames (no breathe), so glow
    composes on top of alive/zoom/etc. without killing the underlying motion."""
    from app.services.media_service import image_glow_video, glow_existing_video
    return _apply_motion(outputs, "glow", image_glow_video, glow_existing_video)


def apply_alive(outputs: List[OutputFile]) -> List[OutputFile]:
    """3D-parallax an effect's IMAGE output (the opt-in `alive` modifier, e.g. `dildo
    alive`). Each image becomes a looping parallax MP4; video outputs (audio gags) are
    left as-is (parallax needs a still). Original kept if the depth model is missing."""
    from pathlib import Path
    from app.services import parallax_service
    result: List[OutputFile] = []
    for out in outputs or []:
        ct = (out.get("content_type") or "").lower()
        stem = Path(out.get("filename") or "image").stem or "image"
        if ct.startswith("image/"):
            try:
                mp4 = parallax_service.add_parallax(out["data"], amplitude=0.035, zoom=1.06, loops=3)
                result.append({"filename": f"{stem}_alive.mp4", "data": mp4, "content_type": "video/mp4"})
                continue
            except Exception as e:
                logger.warning(f"alive modifier failed for {stem}, keeping still: {e}")
        result.append(out)
    return result


def _meme_font_path() -> str:
    """First existing meme font file ("" if none → ffmpeg uses its default)."""
    for p in _MEME_FONT_CANDIDATES:
        if os.path.exists(p):
            return p
    return ""


def apply_meme_text(outputs: List[OutputFile], text: str) -> List[OutputFile]:
    """Burn a meme caption (outlined white, lower third) onto each effect output —
    images via Pillow, videos via ffmpeg drawtext. The trailing `meme <text>`
    subcommand; applied last so it sits over any zoom/shake motion. Original kept
    on failure."""
    text = (text or "").strip()
    if not text:
        return outputs
    from app.services.media_service import caption_video
    result: List[OutputFile] = []
    for out in outputs or []:
        ct = (out.get("content_type") or "").lower()
        fn = out.get("filename") or "file"
        stem = Path(fn).stem or "file"
        try:
            if ct.startswith("image/"):
                data = add_meme_text(out["data"], text)
                result.append({"filename": f"{stem}.jpg", "data": data, "content_type": "image/jpeg"})
            elif ct.startswith("video/"):
                data = caption_video(out["data"], text, _meme_font_path())
                result.append({"filename": fn, "data": data, "content_type": "video/mp4"})
            else:
                result.append(out)
        except Exception as e:
            logger.error(f"meme text overlay failed for {fn}: {e}", exc_info=True)
            result.append(out)
    return result
