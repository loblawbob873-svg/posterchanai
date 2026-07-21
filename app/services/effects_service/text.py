"""Auto-split from the original effects_service.py monolith. No behavior change."""
from ._common import List, OutputFile, Path, Tuple, _EMOJI_RE, _alive_or_still, _human_size, _load_meme_font, _meme_font_path, _wrap_text_to_width, io, is_image, logger, re

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
    web UI and Telegram share one delivery path.
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
