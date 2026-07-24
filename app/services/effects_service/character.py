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
    apply_character. Returns (image, top_y, left_x, right_x) so a caption can be placed in whichever
    gutter beside her is wider, clear of the art."""
    from PIL import Image as _Img
    W, H = base.size
    # Size by height BUT cap against width. Height-only 38% made her 729px wide on a 1080x1920 phone
    # photo: she swallowed BOTH gutters (111px each vs the 194px minimum), so the caption had nowhere to
    # sit beside her and fell back to a banner above her head — which reads as "the text is far from the
    # character". Landscape test images never hit it, which is why it survived several rounds of review.
    ch = max(2, min(int(H * height_frac), int(W * height_frac)))
    char = _character_still(char_path)
    # CROP to the opaque silhouette first. The assets are 460x460 canvases with the figure inset —
    # would.png carries 114px of empty pixels to the RIGHT of the old man. Without this the returned
    # right_x is the canvas edge, ~100px clear of his actual body, so the speech bubble hugged nothing
    # and looked shoved off to the right. Cropping also means height_frac sizes the FIGURE rather than
    # the padding, so he renders at the intended size instead of slightly small.
    try:
        _bb = char.getbbox()
        if _bb:
            char = char.crop(_bb)
    except Exception:
        pass
    cw = max(1, int(char.width * ch / char.height))
    char = char.resize((cw, ch), _Img.LANCZOS)
    y = max(0, H - ch)
    x = max(0, (W - cw) // 2)
    base.alpha_composite(char, (x, y))
    return base, y, x, min(W, x + cw)


def _draw_speech_bubble(draw, cx, cy, tw, th, toward_left: bool, scale: int):
    """A rounded dialogue bubble behind the caption with a tail aimed at the character, so the text
    reads as her SAYING it rather than as a caption laid over the picture. Drawn under the text; the
    caller then renders the words in dark ink on the white fill."""
    pad = max(6, scale // 3)
    x0, y0 = cx - tw // 2 - pad, cy - pad
    x1, y1 = cx + tw // 2 + pad, cy + th + pad
    r = max(6, min(pad * 2, (y1 - y0) // 3))
    outline = max(2, scale // 8)
    draw.rounded_rectangle([x0, y0, x1, y1], radius=r, fill=(255, 255, 255),
                           outline=(20, 18, 26), width=outline)
    # Tail: a small triangle off the side facing the character, overlapping the border so it reads as
    # one shape rather than a detached wedge.
    ty = min(y1 - r, max(y0 + r, (y0 + y1) // 2))
    tl = max(8, scale)
    if toward_left:
        pts = [(x0 + outline // 2, ty - tl // 2), (x0 + outline // 2, ty + tl // 2), (x0 - tl, ty)]
    else:
        pts = [(x1 - outline // 2, ty - tl // 2), (x1 - outline // 2, ty + tl // 2), (x1 + tl, ty)]
    draw.polygon(pts, fill=(255, 255, 255), outline=(20, 18, 26))
    # Re-cover the seam the tail's outline draws across the bubble edge.
    if toward_left:
        draw.rectangle([x0 + outline // 2, ty - tl // 2 + outline, x0 + outline, ty + tl // 2 - outline], fill=(255, 255, 255))
    else:
        draw.rectangle([x1 - outline, ty - tl // 2 + outline, x1 - outline // 2, ty + tl // 2 - outline], fill=(255, 255, 255))


def _draw_point_arrow(draw, W, char_top, caption_bottom, H):
    """A white, black-outlined arrow in the gap between the caption and the character's head, aimed UP
    at the image being pointed at. Drawn with polygons rather than an emoji glyph so it renders
    identically everywhere — an emoji font is not guaranteed on a headless box."""
    gap_top, gap_bottom = caption_bottom + int(H * 0.01), char_top - int(H * 0.01)
    span = gap_bottom - gap_top
    if span < max(12, int(H * 0.04)):
        return                                  # no room between caption and character; skip rather than overlap
    span = min(span, int(H * 0.13))
    cx = W // 2
    bottom = gap_bottom
    top = bottom - span
    head = int(span * 0.45)
    hw = max(3, int(span * 0.30))               # half-width of the arrowhead
    sw = max(2, int(span * 0.10))               # half-width of the shaft
    outline = max(2, span // 14)
    head_pts = [(cx, top), (cx - hw, top + head), (cx + hw, top + head)]
    shaft = [cx - sw, top + head, cx + sw, bottom]
    for w, col in ((outline, (0, 0, 0)), (0, (255, 255, 255))):
        if w:
            draw.polygon([(x, y) for x, y in head_pts], fill=col, outline=col, width=w)
            draw.rectangle(shaft, fill=col, outline=col, width=w)
        else:
            draw.polygon([(cx, top + outline), (cx - hw + outline, top + head), (cx + hw - outline, top + head)], fill=col)
            draw.rectangle([shaft[0] + outline, shaft[1], shaft[2] - outline, shaft[3] - outline], fill=col)


def add_theraped(data: bytes, caption: str = "The Raped") -> bytes:
    """`theraped` — the imageboard pointing-up format (see _add_pointing_meme)."""
    return _add_pointing_meme(data, "theraped", caption, fallback="animegirl")


def add_would(data: bytes, caption: str = "WOULD") -> bytes:
    """`would` — the same pointing-up format with the old man. Same renderer, different art + caption,
    so the two can never drift apart in layout."""
    return _add_pointing_meme(data, "would", caption, fallback="theraped")


def _add_pointing_meme(data: bytes, char_key: str, caption: str, fallback: str = "animegirl") -> bytes:
    """The pointing-up meme format: the character stands bottom-centre pointing at the image above,
    with the caption BESIDE them (whichever side has more room) in a speech bubble, so the character
    stays the focal point and the text reads as dialogue rather than a meme banner.

    Reuses the proven meme font/wrap/stroke helpers rather than reimplementing text layout, and takes
    the art from the _CHARACTERS registry — drop a pointing pose at assets/characters/<key>.png and it
    is used automatically.
    """
    from PIL import Image as _Img, ImageDraw as _Draw, ImageOps as _Ops
    from io import BytesIO as _BIO
    from ._common import _load_meme_font, _wrap_text_to_width

    cp = _character_path(char_key) or _character_path(fallback)
    if not cp:
        raise ValueError(f"{char_key}: no character asset found ({char_key}.png or {fallback})")
    has_pose = bool(_character_path(char_key))   # dedicated art already points; don't draw a 2nd arrow

    text = (caption or char_key).strip().upper()
    with _Img.open(_BIO(data)) as im:
        im = _Ops.exif_transpose(im)
        base = im.convert("RGBA")

    base, char_top, char_left, char_right = _composite_char_bottom_center(base, cp)
    W, H = base.size
    draw = _Draw.Draw(base)
    margin = max(int(W * 0.03), 6)

    # Caption goes in the WIDER gutter beside her. Ties go right (she reads as pointing up-left of it
    # in the stock pose). If neither gutter can hold readable text — a very wide character, a narrow
    # image — fall back to the band above her head rather than cramming it into a 3-character column.
    left_w, right_w = char_left - 2 * margin, W - char_right - 2 * margin
    side = "right" if right_w >= left_w else "left"
    band_w = max(left_w, right_w)
    beside = band_w >= max(int(W * 0.18), 60)
    # Use only part of the gutter: at full width the bubble filled it end to end and clamped against the
    # frame margin, which reads as "pinned to the right" rather than sitting beside him. The tail and the
    # bubble padding also live outside `tw`, so budgeting for them here is what keeps the tail on him.
    max_width = int(band_w * 0.74) if beside else (W - 2 * margin)
    # "Smaller" applies to BOTH placements: this is a label on her, not a meme banner. The above-head
    # fallback used H/6 and rendered enormous on portrait images where the gutters are too narrow.
    top_size = max(int(H / (11 if beside else 10)), 13)
    band_h = (H - char_top) if beside else min(max(int(H * 0.10), char_top - margin), int(H * 0.30))

    # A caption must never be broken MID-WORD. _wrap_text_to_width hard-breaks a word that cannot fit,
    # so in a narrow gutter "WOULD" came out as "WOUL"/"D" — and the size loop accepted it, because the
    # broken result does technically fit. Require every WORD to fit on its own line and keep shrinking
    # until one does; wrapping between words is still fine for longer captions.
    words = text.split() or [text]
    chosen = None
    for size in range(top_size, 10, -1):
        font = _load_meme_font(size)
        if max(draw.textlength(w, font=font) for w in words) > max_width:
            continue                                   # some word would be hyphen-less chopped — go smaller
        lines = _wrap_text_to_width(draw, text, font, max_width)
        stroke = max(1, size // 14)
        bbox = draw.multiline_textbbox((0, 0), "\n".join(lines), font=font,
                                       stroke_width=stroke, align="center")
        if (bbox[2] - bbox[0]) <= max_width and (bbox[3] - bbox[1]) <= band_h:
            chosen = (font, lines, stroke, bbox)
            break
    if chosen is None:
        font = _load_meme_font(11)
        lines = _wrap_text_to_width(draw, text, font, max_width)
        stroke = 1
        bbox = draw.multiline_textbbox((0, 0), "\n".join(lines), font=font,
                                       stroke_width=stroke, align="center")
        chosen = (font, lines, stroke, bbox)

    font, lines, stroke, bbox = chosen
    tw, th = bbox[2] - bbox[0], bbox[3] - bbox[1]
    if beside:
        # HUG her: the bubble sits a small fixed gap from her silhouette, not centred in the gutter.
        # Centring put it out at the far edge of a wide image — the tail stretched across empty space
        # and it stopped reading as her speech. The gap scales with the image so it holds at any size.
        gap = max(int(W * 0.015), 6)
        cx = (char_left - gap - tw // 2) if side == "left" else (char_right + gap + tw // 2)
        cx = max(margin + tw // 2, min(cx, W - margin - tw // 2))
        # char_top is the top of the RAISED ARM, so anchoring near it put the bubble up level with his
        # finger. Centre it on the head/upper torso instead (~38% down the character) so it reads as
        # speech coming from him.
        char_h = H - char_top
        y = max(margin, min(char_top + int(char_h * 0.38) - th // 2, H - th - margin))
    else:
        cx, y = W // 2, max(margin, min(char_top - th - margin, H - th - margin))
    if beside:
        # Dialogue: bubble first, then dark ink on it (a white stroke-outlined caption would vanish).
        _draw_speech_bubble(draw, cx, y, tw, th, toward_left=(side == "right"), scale=max(6, font.size // 3))
        draw.multiline_text((cx, y), "\n".join(lines), font=font, fill=(20, 18, 26),
                            anchor="ma", align="center")
    else:
        draw.multiline_text((cx, y), "\n".join(lines), font=font, fill=(255, 255, 255),
                            stroke_width=stroke, stroke_fill=(0, 0, 0), anchor="ma", align="center")

    # The format IS the point: she indicates the image above. The stock animegirl still has no arm-up
    # pose, so draw an explicit arrow above her. Skipped once real pointing art is installed.
    if not has_pose:
        _draw_point_arrow(draw, W, char_top, (y + th) if not beside else margin, H)

    buf = _BIO()
    base.convert("RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue()


def _pointing_attachments(attachments, key: str, title: str, fn):
    """Apply a pointing-up meme to the first image attachment. Mirrors gay_attachments/blood_attachments."""
    from ._common import _alive_or_still, _human_size, is_image
    images = [(f, d, ct) for f, d, ct in (attachments or []) if is_image(f, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = fn(data)
        out = _alive_or_still(result, stem, key)
        summary = f"## 👉 {title}\n\n👉 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"{key} failed for {filename}: {e}", exc_info=True)
        return [], f"Could not apply {key} to {filename}: {e}"


def theraped_attachments(attachments):
    return _pointing_attachments(attachments, "theraped", "The Raped", add_theraped)


def would_attachments(attachments):
    return _pointing_attachments(attachments, "would", "Would", add_would)
