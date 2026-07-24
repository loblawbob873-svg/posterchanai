"""Auto-split from the original effects_service.py monolith. No behavior change."""
from ._common import List, OutputFile, Path, _CHARACTERS, _CHARS_DIR_CANDIDATES, logger, os

def _character_path(name: str) -> str:
    """Resolve a character name (or alias) to an existing asset path ("" if unknown/missing)."""
    fn = _CHARACTERS.get((name or "").lower().strip())
    if not fn:
        return ""
    for base in _CHARS_DIR_CANDIDATES:
        p = os.path.join(base, fn)
        if os.path.exists(p):
            return p
    return ""


def _character_still(char_path: str):
    """A transparent PIL image of the character (PNG/GIF directly, or the first frame of a .mov)."""
    from PIL import Image as _Img
    if char_path.lower().endswith((".png", ".gif", ".webp")):
        return _Img.open(char_path).convert("RGBA")
    import tempfile as _tf, subprocess as _sp
    from app.services.media_service import resolve_ffmpeg
    _fd, _fp = _tf.mkstemp(suffix=".png"); os.close(_fd)
    try:
        _sp.run([resolve_ffmpeg(), "-y", "-i", char_path, "-frames:v", "1", _fp],
                capture_output=True, timeout=30)
        return _Img.open(_fp).convert("RGBA")
    finally:
        try:
            os.unlink(_fp)
        except Exception:
            pass


def _composite_char_on_image(image_bytes: bytes, char_path: str,
                             height_frac: float = 0.34, margin_frac: float = 0.03) -> bytes:
    from PIL import Image as _Img
    from io import BytesIO as _BIO
    base = _Img.open(_BIO(image_bytes)).convert("RGBA")
    W, H = base.size
    ch = max(2, int(H * height_frac))
    char = _character_still(char_path)
    cw = max(1, int(char.width * ch / char.height))
    char = char.resize((cw, ch))
    mw, mh = int(W * margin_frac), int(H * margin_frac)
    base.alpha_composite(char, (max(0, W - cw - mw), max(0, H - ch - mh)))
    buf = _BIO()
    base.convert("RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue()


def apply_character(outputs: List[OutputFile], name: str) -> List[OutputFile]:
    """Overlay the named character bottom-right on each effect output (video → animated overlay,
    image → static composite). Unknown name or any failure leaves the output untouched."""
    cp = _character_path(name)
    if not cp:
        return outputs
    from app.services.media_service import overlay_corner_character
    result: List[OutputFile] = []
    for out in outputs or []:
        ct = (out.get("content_type") or "").lower()
        fn = out.get("filename") or "file"
        try:
            if ct.startswith("video/"):
                out = {**out, "data": overlay_corner_character(out["data"], fn, cp)}
            elif ct.startswith("image/"):
                stem = Path(fn).stem or "file"
                out = {"filename": f"{stem}.jpg",
                       "data": _composite_char_on_image(out["data"], cp),
                       "content_type": "image/jpeg"}
        except Exception as e:
            logger.error(f"apply_character ({name}) failed for {fn}: {e}", exc_info=True)
        result.append(out)
    return result


def _composite_char_bottom_center(base, char_path: str, height_frac: float = 0.38):
    """Place the character bottom-CENTRE (the pointing-up meme anchor) rather than bottom-right like
    apply_character. Returns (image, char_top_y) so a caption can be placed clear of the character."""
    from PIL import Image as _Img
    W, H = base.size
    ch = max(2, int(H * height_frac))
    char = _character_still(char_path)
    cw = max(1, int(char.width * ch / char.height))
    char = char.resize((cw, ch), _Img.LANCZOS)
    y = max(0, H - ch)
    base.alpha_composite(char, (max(0, (W - cw) // 2), y))
    return base, y


def add_theraped(data: bytes, caption: str = "The Raped") -> bytes:
    """`theraped` — the imageboard pointing-up format: a character stands bottom-centre pointing at the
    image above, captioned. The caption sits ABOVE the character so the two never overlap, and shrinks
    to fit rather than clipping.

    Reuses the proven meme font/wrap/stroke helpers rather than reimplementing text layout. The
    character comes from the existing _CHARACTERS registry (`animegirl`), so swapping the art is a
    matter of replacing the asset file — nothing here hardcodes a picture.
    """
    from PIL import Image as _Img, ImageDraw as _Draw, ImageOps as _Ops
    from io import BytesIO as _BIO
    from ._common import _load_meme_font, _wrap_text_to_width

    cp = _character_path("animegirl")
    if not cp:
        raise ValueError("theraped: the 'animegirl' character asset is missing")

    text = (caption or "The Raped").strip().upper()
    with _Img.open(_BIO(data)) as im:
        im = _Ops.exif_transpose(im)
        base = im.convert("RGBA")

    base, char_top = _composite_char_bottom_center(base, cp)
    W, H = base.size
    draw = _Draw.Draw(base)
    margin = max(int(W * 0.04), 8)
    max_width = W - 2 * margin
    # The caption band is everything above the character's head, capped so it can't dominate the image.
    band = max(int(H * 0.10), char_top - margin)
    band = min(band, int(H * 0.30))

    chosen = None
    for size in range(max(int(H / 6), 14), 11, -2):
        font = _load_meme_font(size)
        lines = _wrap_text_to_width(draw, text, font, max_width)
        stroke = max(1, size // 14)
        bbox = draw.multiline_textbbox((0, 0), "\n".join(lines), font=font,
                                       stroke_width=stroke, align="center")
        if (bbox[2] - bbox[0]) <= max_width and (bbox[3] - bbox[1]) <= band:
            chosen = (font, lines, stroke, bbox)
            break
    if chosen is None:
        font = _load_meme_font(12)
        lines = _wrap_text_to_width(draw, text, font, max_width)
        stroke = 1
        bbox = draw.multiline_textbbox((0, 0), "\n".join(lines), font=font,
                                       stroke_width=stroke, align="center")
        chosen = (font, lines, stroke, bbox)

    font, lines, stroke, bbox = chosen
    th = bbox[3] - bbox[1]
    # Sit the block just above the character, clamped into the image if the character is very tall.
    y = max(margin, min(char_top - th - margin, H - th - margin))
    draw.multiline_text((W // 2, y), "\n".join(lines), font=font, fill=(255, 255, 255),
                        stroke_width=stroke, stroke_fill=(0, 0, 0), anchor="ma", align="center")

    buf = _BIO()
    base.convert("RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue()


def theraped_attachments(attachments):
    """Apply `theraped` to the first image attachment. Mirrors gay_attachments/blood_attachments."""
    from ._common import _alive_or_still, _human_size, is_image
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_theraped(data)
        out = _alive_or_still(result, stem, "theraped")
        summary = f"## 👉 The Raped\n\n👉 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"theraped failed for {filename}: {e}", exc_info=True)
        return [], f"Could not apply theraped to {filename}: {e}"
