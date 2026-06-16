"""Collage effect — the ONE effect that combines ALL attached images into a single image
(every other effect uses only the first). The combined image then flows through the normal
effect pipeline (caption / character / motion), same as any other effect output."""
from ._common import OutputFile, _alive_or_still, _human_size, is_image, logger


def make_collage(images: list, cell: int = 512, gap: int = 8, bg=(18, 18, 18)) -> bytes:
    """Tile N images into a near-square grid, each fit into a `cell`-sized box (aspect kept,
    centered) on a dark background. Returns JPEG bytes."""
    import io, math
    from PIL import Image

    pics = []
    for data in images:
        try:
            pics.append(Image.open(io.BytesIO(data)).convert("RGB"))
        except Exception as e:
            logger.warning(f"collage: skipping unreadable image: {e}")
    if not pics:
        raise ValueError("no readable images")

    n = len(pics)
    cols = math.ceil(math.sqrt(n))
    rows = math.ceil(n / cols)
    W = gap + cols * (cell + gap)
    H = gap + rows * (cell + gap)
    canvas = Image.new("RGB", (W, H), bg)
    for idx, im in enumerate(pics):
        r, c = divmod(idx, cols)
        thumb = im.copy()
        thumb.thumbnail((cell, cell), Image.LANCZOS)
        x = gap + c * (cell + gap) + (cell - thumb.width) // 2
        y = gap + r * (cell + gap) + (cell - thumb.height) // 2
        canvas.paste(thumb, (x, y))
    out = io.BytesIO()
    canvas.save(out, "JPEG", quality=90)
    return out.getvalue()


def collage_attachments(attachments):
    """Combine all attached images into one collage image. Mirrors the other *_attachments
    shape (returns (output_files, summary_text)) so the shared delivery path is unchanged."""
    _all = attachments or []
    images = [(fn, d, ct) for fn, d, ct in _all if is_image(fn, ct)]
    logger.info(f"[collage] received {len(_all)} attachments {[(fn, ct) for fn, _, ct in _all]}; "
                f"{len(images)} passed is_image")
    if not images:
        return [], "No images — attach at least one image to make a collage."
    try:
        result = make_collage([d for _, d, _ in images])
        out = _alive_or_still(result, "collage", "collage")
        summary = f"## 🧩 Collage\n\n🖼️ {len(images)} images → {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"collage failed: {e}", exc_info=True)
        return [], f"❌ collage: {e}"
