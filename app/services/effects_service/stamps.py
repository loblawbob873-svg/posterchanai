"""Auto-split from the original effects_service.py monolith. No behavior change."""
from ._common import List, OutputFile, Path, Tuple, _alive_or_still, _draw_tracked, _human_size, _load_blacked_font, _load_meme_font, _shade, _tracked_width, io, is_image, logger

def _stamp_centred(img, stamp, frac: float = 0.66):
    """Scale `stamp` to `frac` of the frame and composite it CENTRED, fitting BOTH dimensions.

    The old spelling scaled to a fraction of the WIDTH only and let the height follow the aspect
    ratio, then rotated with expand=True (which grows the bounding box another ~28% at 20 degrees)
    and composited at ((W-w)//2, (H-h)//2). On anything wider than it is tall that y goes NEGATIVE and
    PIL's alpha_composite silently CLIPS — no error, no warning, just a stamp with its top and bottom
    sliced off. A plain 1920x1080 photo lost a quarter of the stamp; a wide banner lost far more.

    Fitting both axes AFTER the rotation is what makes it correct: scaling first and rotating second
    means the number you fitted is not the number you draw.
    """
    from PIL import Image        # imported per-function in this module, not at module scope
    W, H = img.size
    sw, sh = stamp.size
    sc = min(W * frac / max(sw, 1), H * frac / max(sh, 1))
    if sc < 1.0:                       # only ever shrink — never upscale a stamp that already fits
        stamp = stamp.resize((max(int(sw * sc), 1), max(int(sh * sc), 1)), Image.BICUBIC)
    img.alpha_composite(stamp, ((W - stamp.width) // 2, (H - stamp.height) // 2))
    return img


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
        # Rotate FIRST, then fit — expand=True grows the bounding box, so fitting before the rotation
        # measures a stamp that is not the one being drawn.
        stamp = stamp.rotate(random.uniform(15, 22), expand=True, resample=Image.BICUBIC)
        img = _stamp_centred(img, stamp)

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


def _make_word_stamp(text: str, text_h: int):
    """Render a distressed red rubber stamp of `text` (the shared GAY/HAG/GOON stamp look: double
    border, stamp-red ink, grungy speckle). Pure Pillow; the caller rotates + scales it."""
    import random
    from PIL import Image, ImageDraw

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
    d.rectangle([bw, bw, W - bw, H - bw], outline=red, width=bw)
    off = int(bw * 2.2)
    d.rectangle([off, off, W - off, H - off], outline=red, width=max(bw // 2, 2))
    d.text(((W - tw) / 2 - bbox[0], (H - th) / 2 - bbox[1]), text, font=font,
           fill=red, stroke_width=stroke, stroke_fill=red)

    px = tile.load()
    for _ in range(int(W * H * 0.05)):
        x, y = random.randint(0, W - 1), random.randint(0, H - 1)
        r, g, b, a = px[x, y]
        if a > 0:
            px[x, y] = (r, g, b, int(a * random.uniform(0.0, 0.6)))
    return tile


def add_goon(data: bytes, count: int = 0) -> bytes:
    """Stamp a big rotated red "GOON" across the image (same treatment as GAY). Returns JPEG bytes."""
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
        stamp = _make_word_stamp("GOON", max(int(min(W, H) * 0.17), 24))
        # Rotate FIRST, then fit — expand=True grows the bounding box, so fitting before the rotation
        # measures a stamp that is not the one being drawn.
        stamp = stamp.rotate(random.uniform(15, 22), expand=True, resample=Image.BICUBIC)
        img = _stamp_centred(img, stamp)

        img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue()


def goon_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Stamp GOON on the first image attachment. Mirrors gay_attachments."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_goon(data)
        out = _alive_or_still(result, stem, "goon")
        summary = f"## 🥴 Goon\n\n🥴 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"goon failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


def _make_hag_stamp(text_h: int):
    """Render a distressed red rubber stamp reading "HAG" — identical treatment to the GAY stamp
    (double border, stamp-red ink, grungy speckle). Pure Pillow; caller rotates + scales it."""
    import random
    from PIL import Image, ImageDraw

    text = "HAG"
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
    d.rectangle([bw, bw, W - bw, H - bw], outline=red, width=bw)
    off = int(bw * 2.2)
    d.rectangle([off, off, W - off, H - off], outline=red, width=max(bw // 2, 2))
    d.text(((W - tw) / 2 - bbox[0], (H - th) / 2 - bbox[1]), text, font=font,
           fill=red, stroke_width=stroke, stroke_fill=red)

    px = tile.load()
    for _ in range(int(W * H * 0.05)):
        x, y = random.randint(0, W - 1), random.randint(0, H - 1)
        r, g, b, a = px[x, y]
        if a > 0:
            px[x, y] = (r, g, b, int(a * random.uniform(0.0, 0.6)))
    return tile


def _draw_old_lady(h: int):
    """Render a cute little old lady's face on a transparent square tile (pure Pillow): silver hair
    with a top bun + side curls, round spectacles, rosy cheeks and a warm smile. Ships no asset;
    drawn in the same style as _make_barked_dog so `hag` matches the drawn-figure look of `barked`.
    `h` = tile size px."""
    from PIL import Image, ImageDraw

    W = H = max(int(h), 48)
    cx = W / 2.0
    skin = (255, 222, 194)
    hair = (210, 210, 216)
    hair_d = _shade(hair, 0.82)[:3]
    dark = (70, 60, 62)
    pink = (243, 158, 168)
    lw = max(int(W * 0.014), 2)

    tile = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(tile)

    # silver hair bun on top
    d.ellipse([W * 0.37, H * 0.02, W * 0.63, H * 0.27], fill=hair, outline=hair_d, width=lw)
    # hair mass framing the face
    d.ellipse([W * 0.12, H * 0.12, W * 0.88, H * 0.94], fill=hair, outline=hair_d, width=lw)
    # side curls
    d.ellipse([W * 0.09, H * 0.40, W * 0.30, H * 0.68], fill=hair, outline=hair_d, width=max(lw - 1, 1))
    d.ellipse([W * 0.70, H * 0.40, W * 0.91, H * 0.68], fill=hair, outline=hair_d, width=max(lw - 1, 1))
    # face
    d.ellipse([W * 0.20, H * 0.20, W * 0.80, H * 0.90], fill=skin, outline=_shade(skin, 0.8)[:3], width=max(lw - 1, 1))
    # rosy cheeks
    cr = W * 0.075
    d.ellipse([W * 0.32 - cr, H * 0.63 - cr, W * 0.32 + cr, H * 0.63 + cr], fill=pink)
    d.ellipse([W * 0.68 - cr, H * 0.63 - cr, W * 0.68 + cr, H * 0.63 + cr], fill=pink)
    # round spectacles + eyes
    gr = W * 0.11
    gy = H * 0.51
    for ex in (W * 0.38, W * 0.62):
        d.ellipse([ex - gr, gy - gr, ex + gr, gy + gr], outline=dark, width=lw)
        er = W * 0.028
        d.ellipse([ex - er, gy - er, ex + er, gy + er], fill=dark)   # eye
    d.line([(W * 0.38 + gr, gy), (W * 0.62 - gr, gy)], fill=dark, width=lw)   # bridge
    # tiny nose
    d.line([(cx, gy + gr * 0.5), (cx, H * 0.69)], fill=_shade(skin, 0.7)[:3], width=lw)
    # warm smile
    d.arc([W * 0.37, H * 0.64, W * 0.63, H * 0.84], start=15, end=165, fill=dark, width=lw)
    return tile


def add_hag(data: bytes, count: int = 0) -> bytes:
    """Stamp a big rotated red "HAG" (centred, exactly like GAY) and draw a cute little old lady small
    at the bottom centre. Returns JPEG bytes."""
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

        # HAG stamp — centred (mirrors add_gay)
        stamp = _make_hag_stamp(max(int(min(W, H) * 0.17), 24))
        # Rotate FIRST, then fit — expand=True grows the bounding box, so fitting before the rotation
        # measures a stamp that is not the one being drawn.
        stamp = stamp.rotate(random.uniform(15, 22), expand=True, resample=Image.BICUBIC)
        img = _stamp_centred(img, stamp)

        # cute little old lady — small, bottom centre
        lady = _draw_old_lady(max(int(min(W, H) * 0.26), 48))
        img.alpha_composite(lady, ((W - lady.width) // 2, H - lady.height - max(int(H * 0.02), 4)))

        img = img.convert("RGB")
        out = io.BytesIO()
        img.save(out, format="JPEG", quality=90, optimize=True)
        return out.getvalue()


def hag_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Stamp HAG + draw a little old lady on the first image attachment. Mirrors gay_attachments."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_hag(data)
        out = _alive_or_still(result, stem, "hag")
        summary = f"## 👵 Hag\n\n👵 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"hag failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


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
