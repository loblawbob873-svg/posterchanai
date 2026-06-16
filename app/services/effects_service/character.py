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
