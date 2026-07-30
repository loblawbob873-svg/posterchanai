"""Auto-split from the original effects_service.py monolith. No behavior change."""
from ._common import List, OutputFile, Path, Tuple, _AKBAR_AUDIO_CANDIDATES, _AKBAR_DURATION, _BEAVIS_AUDIO_CANDIDATES, _BEAVIS_DURATION, _BEAVIS_OVERLAY_CANDIDATES, _CHEERS_AUDIO_CANDIDATES, _CHEERS_DURATION, _CURB_AUDIO_CANDIDATES, _CURB_DURATION, _DEPRESSING_AUDIO_CANDIDATES, _DEPRESSING_DURATION, _FAHH_AUDIO_CANDIDATES, _FAHH_AUDIO_START, _FAHH_DURATION, _FBI_AUDIO_CANDIDATES, _FBI_DURATION, _FELIZ_AUDIO_CANDIDATES, _FELIZ_DURATION, _FELTEDTABLES_AUDIO_CANDIDATES, _FELTEDTABLES_DURATION, _GIGITY_AUDIO_CANDIDATES, _GIGITY_DURATION, _GONG_AUDIO_CANDIDATES, _GONG_DURATION, _HAVA_AUDIO_CANDIDATES, _HAVA_DURATION, _HELPME_AUDIO_CANDIDATES, _HELPME_DURATION, _HOOD_AUDIO_CANDIDATES, _HOOD_DURATION, _HORSE_AUDIO_CANDIDATES, _HORSE_DURATION, _KNIGHTRIDER_AUDIO_CANDIDATES, _KNIGHTRIDER_DURATION, _HUGEBITCH_AUDIO_CANDIDATES, _HUGEBITCH_DURATION, _INDIAN_AUDIO_CANDIDATES, _INDIAN_DURATION, _PRAYER_AUDIO_CANDIDATES, _PRAYER_DURATION, _REDEEM_AUDIO_CANDIDATES, _REDEEM_DURATION, _RETARD_AUDIO_CANDIDATES, _RETARD_DURATION, _REZE_AUDIO_CANDIDATES, _REZE_DANCE_CANDIDATES, _REZE_DURATION, _VIBE_AUDIO_CANDIDATES, _VIBE_DANCE_CANDIDATES, _VIBE_DURATION, _REBECCA_AUDIO_CANDIDATES, _REBECCA_DANCE_CANDIDATES, _REBECCA_DURATION, _MAKIMA_AUDIO_CANDIDATES, _MAKIMA_SHOOT_CANDIDATES, _MAKIMA_DURATION, _GURA_AUDIO_CANDIDATES, _GURA_POG_CANDIDATES, _GURA_DURATION, _ROBOCOP_AUDIO_CANDIDATES, _ROBOCOP_DURATION, _SETH_AUDIO_CANDIDATES, _SETH_DURATION, _SLEEPWELL_AUDIO_CANDIDATES, _SLEEPWELL_DURATION, _SMELL_AUDIO_CANDIDATES, _SMELL_DURATION, _TERMINATOR_AUDIO_CANDIDATES, _TERMINATOR_DURATION, _TITAN_AUDIO_CANDIDATES, _TITAN_DURATION, _WHOABUDDY_AUDIO_CANDIDATES, _WHOABUDDY_DURATION, _HEAT_AUDIO_CANDIDATES, _HEAT_DURATION, _DIARRHEA_AUDIO_CANDIDATES, _DIARRHEA_DURATION, _YAKETY_AUDIO_CANDIDATES, _YAKETY_DURATION, _YAMETE_AUDIO_CANDIDATES, _YAMETE_DURATION, _human_size, _pad_audio_to_duration, is_image, logger, os

def _hava_audio_path() -> str:
    """First existing Hava Nagila mp3 from the candidate list ("" if none)."""
    for p in _HAVA_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


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


def _fahh_audio_path() -> str:
    """First existing fahh mp3 from the candidate list ("" if none)."""
    for p in _FAHH_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_fahh(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a 5s MP4 playing the fahh clip (padded with trailing silence so the
    moving photo holds for the full 5s before the outro watermark) over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _fahh_audio_path()
    if not audio:
        raise RuntimeError("Fahh audio (assets/fahh.mp3) is missing on the server")
    padded = _pad_audio_to_duration(audio, _FAHH_DURATION, start=_FAHH_AUDIO_START)
    try:
        return image_audio_to_video(image_data, source_filename, padded, duration=_FAHH_DURATION)
    finally:
        if padded != audio and os.path.exists(padded):
            os.unlink(padded)


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


def _beavis_audio_path() -> str:
    """First existing beavis mp3 from the candidate list ("" if none)."""
    for p in _BEAVIS_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def _beavis_overlay_path() -> str:
    """First existing beavis overlay clip from the candidate list ("" if none)."""
    for p in _BEAVIS_OVERLAY_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_beavis(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Composite the cackling Beavis + Butt-Head cutout over an image, set to the laugh. MP4 bytes.

    Falls back to the plain audio-over-still render when the overlay asset is missing, so a node that
    hasn't pulled it yet still answers `beavis` with the laugh instead of an error."""
    from app.services.media_service import image_audio_to_video, image_gif_overlay_video
    audio = _beavis_audio_path()
    if not audio:
        raise RuntimeError("Beavis audio (assets/beavis.mp3) is missing on the server")
    overlay = _beavis_overlay_path()
    if not overlay:
        return image_audio_to_video(image_data, source_filename, audio, duration=_BEAVIS_DURATION)
    # The pair is WIDER than tall (382x323), and the overlay is scaled by HEIGHT with no width
    # clamp — at the 0.55 default a 9:16 photo got a 1.15x-too-wide overlay and lost their outer
    # arms off the sides. 0.45 is the largest fraction that still fits a 9:16 frame end to end.
    return image_gif_overlay_video(image_data, source_filename, overlay,
                                   duration=_BEAVIS_DURATION, audio_path=audio,
                                   height_frac=0.45)


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


def _heat_audio_path() -> str:
    """First existing heat mp3 from the candidate list ("" if none)."""
    for p in _HEAT_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_heat(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the Heat of the Moment clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _heat_audio_path()
    if not audio:
        raise RuntimeError("Heat audio (assets/heat.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_HEAT_DURATION)


def heat_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a heat MP4. Mirrors whoabuddy_attachments
    (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_heat(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_heat.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## \U0001F525 Heat of the Moment\n\n\U0001F525 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"heat failed for {filename}: {e}", exc_info=True)
        return [], f"\u274c {filename}: {e}"


def _diarrhea_audio_path() -> str:
    """First existing diarrhea mp3 from the candidate list ("" if none)."""
    for p in _DIARRHEA_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_diarrhea(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the diarrhea clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _diarrhea_audio_path()
    if not audio:
        raise RuntimeError("Diarrhea audio (assets/diarrhea.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_DIARRHEA_DURATION)


def diarrhea_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a diarrhea MP4. Mirrors
    whoabuddy_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_diarrhea(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_diarrhea.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 💩 Diarrhea\n\n💩 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"diarrhea failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


def _seth_audio_path() -> str:
    """First existing seth mp3 from the candidate list ("" if none)."""
    for p in _SETH_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_seth(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the seth clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _seth_audio_path()
    if not audio:
        raise RuntimeError("Seth audio (assets/seth.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_SETH_DURATION)


def seth_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a seth MP4. Mirrors
    whoabuddy_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_seth(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_seth.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🎬 Seth\n\n🎬 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"seth failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


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
    # 0.62 rather than the old 0.5: that was tuned for the two-chibi 700x520 canvas, where each
    # figure was only half the width. The keyed asset is ONE dancer filling her frame.
    return image_gif_overlay_video(image_data, source_filename, overlay,
                                   duration=_REZE_DURATION, audio_path=audio, height_frac=0.62)


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


def _makima_audio_path() -> str:
    """First existing makima mp3 (the gunshots) from the candidate list ("" if none)."""
    for p in _MAKIMA_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def _makima_shoot_path() -> str:
    """First existing makima shooting overlay (.mov) from the candidate list ("" if none)."""
    for p in _MAKIMA_SHOOT_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_makima(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Composite Makima finger-gunning the viewer onto the image. MP4 bytes.
    An ANIMATED overlay like rebecca: a generated sprite animated here (recoil + muzzle flashes)
    rather than keyed footage — see scripts/gen_makima_shoot.py. The audio is bare gunshots, timed
    to the exact frames the overlay fires on."""
    from app.services.media_service import image_gif_overlay_video
    if isinstance(image_data, list):  # makima is single-image (overlay), not a slideshow
        image_data = image_data[0][1]
    audio = _makima_audio_path()
    if not audio:
        raise RuntimeError("Makima audio (assets/makima.mp3) is missing on the server")
    overlay = _makima_shoot_path()
    if not overlay:
        raise RuntimeError("Makima overlay (assets/makima_shoot.mov) is missing on the server")
    # Same reasoning as rebecca: her canvas carries recoil/flash headroom, so she only fills ~88%
    # of it and needs a slightly larger frac to land at the same on-screen size.
    return image_gif_overlay_video(image_data, source_filename, overlay,
                                   duration=_MAKIMA_DURATION, audio_path=audio, height_frac=0.68)


def makima_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a makima MP4. Mirrors rebecca_attachments
    (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_makima(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_makima.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🔫 Makima\n\n🔫 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"makima failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


def _gura_audio_path() -> str:
    """First existing gura mp3 (the "a") from the candidate list ("" if none)."""
    for p in _GURA_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def _gura_pog_path() -> str:
    """First existing Shark Pog overlay (.mov) from the candidate list ("" if none)."""
    for p in _GURA_POG_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_gura(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Composite Shark Pog over the image, popping on Gura's "a". MP4 bytes.

    An ANIMATED overlay like makima: the cutout is Know Your Meme's Shark Pog photo (which already
    has real alpha) and the motion is added in scripts/gen_gura.py, because the Shark Pog video
    puts white hair on a white background — there is no key that separates them.
    """
    from app.services.media_service import image_gif_overlay_video
    if isinstance(image_data, list):  # gura is single-image (overlay), not a slideshow
        image_data = image_data[0][1]
    audio = _gura_audio_path()
    if not audio:
        raise RuntimeError("Gura audio (assets/gura.mp3) is missing on the server")
    overlay = _gura_pog_path()
    if not overlay:
        raise RuntimeError("Gura overlay (assets/gura_pog.mov) is missing on the server")
    # Her canvas is 0.8 sprite / 0.2 pop headroom, so the frac is raised to land her on screen at
    # the size the number suggests — same correction as makima and rebecca.
    return image_gif_overlay_video(image_data, source_filename, overlay,
                                   duration=_GURA_DURATION, audio_path=audio, height_frac=0.62)


def gura_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a gura MP4. Mirrors makima_attachments."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_gura(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_gura.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🦈 Gura\n\n🦈 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"gura failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


def _rebecca_audio_path() -> str:
    """First existing rebecca mp3 from the candidate list ("" if none)."""
    for p in _REBECCA_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def _rebecca_dance_path() -> str:
    """First existing rebecca dance overlay (.mov) from the candidate list ("" if none)."""
    for p in _REBECCA_DANCE_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_rebecca(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Composite the dancing, thumbs-up Rebecca onto the image, set to the rebecca clip. MP4 bytes.
    Another ANIMATED overlay (chimp/clay/reze/vibe). Unlike those, the overlay is not keyed footage
    but a sprite this node generated and animated — see scripts/gen_rebecca_dance.py. The asset is
    ONE beat-cycle and the renderer loops it, so it's a fraction of the size of a full-length clip."""
    from app.services.media_service import image_gif_overlay_video
    if isinstance(image_data, list):  # rebecca is single-image (overlay), not a slideshow
        image_data = image_data[0][1]
    audio = _rebecca_audio_path()
    if not audio:
        raise RuntimeError("Rebecca audio (assets/rebecca.mp3) is missing on the server")
    overlay = _rebecca_dance_path()
    if not overlay:
        raise RuntimeError("Rebecca dance overlay (assets/rebecca_dance.mov) is missing on the server")
    # 0.70, not vibe's 0.60: her sprite sits on a canvas with headroom for the hop and the tilt,
    # so she only fills ~86% of it — the extra frac buys back that margin and lands her at the
    # same on-screen size as the keyed-footage overlays.
    return image_gif_overlay_video(image_data, source_filename, overlay,
                                   duration=_REBECCA_DURATION, audio_path=audio, height_frac=0.70)


def rebecca_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a rebecca MP4. Mirrors vibe_attachments
    (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_rebecca(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_rebecca.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 👍 Rebecca\n\n👍 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"rebecca failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


def _vibe_audio_path() -> str:
    """First existing vibe mp3 from the candidate list ("" if none)."""
    for p in _VIBE_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def _vibe_dance_path() -> str:
    """First existing vibe dance overlay (.mov) from the candidate list ("" if none)."""
    for p in _VIBE_DANCE_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_vibe(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Composite the cel-anime dancing girl onto the image, set to the vibe clip. MP4 bytes.
    An ANIMATED overlay effect like reze, but the overlay is real anime footage keyed off a
    green screen (not drawn shapes), so she reads as anime instead of chibi doodles."""
    from app.services.media_service import image_gif_overlay_video
    if isinstance(image_data, list):  # vibe is single-image (overlay), not a slideshow
        image_data = image_data[0][1]
    audio = _vibe_audio_path()
    if not audio:
        raise RuntimeError("Vibe audio (assets/vibe.mp3) is missing on the server")
    overlay = _vibe_dance_path()
    if not overlay:
        raise RuntimeError("Vibe dance overlay (assets/vibe_dance.mov) is missing on the server")
    return image_gif_overlay_video(image_data, source_filename, overlay,
                                   duration=_VIBE_DURATION, audio_path=audio, height_frac=0.6)


def vibe_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a vibe MP4. Mirrors reze_attachments
    (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_vibe(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_vibe.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 💖 Vibe\n\n💖 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"vibe failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


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


def _horse_audio_path() -> str:
    """First existing horse mp3 from the candidate list ("" if none)."""
    for p in _HORSE_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_horse(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the horse clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _horse_audio_path()
    if not audio:
        raise RuntimeError("Horse audio (assets/horse.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_HORSE_DURATION)


def horse_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a horse MP4 (mirrors sleepwell_attachments)."""
    outputs: List[OutputFile] = []
    for filename, data, content_type in attachments or []:
        if not is_image(filename, content_type):
            continue
        stem = Path(filename).stem or "image"
        try:
            result = add_horse(data, filename)
            outputs.append({
                "filename": f"{stem}_horse.mp4",
                "data": result,
                "content_type": "video/mp4",
            })
        except Exception as e:
            logger.error(f"horse failed for {filename}: {e}", exc_info=True)
    if not outputs:
        return [], "No image to add the horse clip to."
    return outputs, f"🐴 Horse ({_human_size(sum(len(o['data']) for o in outputs))})"


def _knightrider_audio_path() -> str:
    """First existing knightrider mp3 from the candidate list ("" if none)."""
    for p in _KNIGHTRIDER_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_knightrider(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the Knight Rider theme over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _knightrider_audio_path()
    if not audio:
        raise RuntimeError("Knight Rider audio (assets/knightrider.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_KNIGHTRIDER_DURATION)


def knightrider_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a Knight Rider MP4 (mirrors horse_attachments)."""
    outputs: List[OutputFile] = []
    for filename, data, content_type in attachments or []:
        if not is_image(filename, content_type):
            continue
        stem = Path(filename).stem or "image"
        try:
            result = add_knightrider(data, filename)
            outputs.append({
                "filename": f"{stem}_knightrider.mp4",
                "data": result,
                "content_type": "video/mp4",
            })
        except Exception as e:
            logger.error(f"knightrider failed for {filename}: {e}", exc_info=True)
    if not outputs:
        return [], "No image to add the Knight Rider clip to."
    return outputs, f"🚗 Knight Rider ({_human_size(sum(len(o['data']) for o in outputs))})"


def _hugebitch_audio_path() -> str:
    """First existing hugebitch mp3 from the candidate list ("" if none)."""
    for p in _HUGEBITCH_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_hugebitch(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the hugebitch clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _hugebitch_audio_path()
    if not audio:
        raise RuntimeError("Huge Bitch audio (assets/hugebitch.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_HUGEBITCH_DURATION)


def hugebitch_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a Huge Bitch MP4 (mirrors knightrider_attachments)."""
    outputs: List[OutputFile] = []
    for filename, data, content_type in attachments or []:
        if not is_image(filename, content_type):
            continue
        stem = Path(filename).stem or "image"
        try:
            result = add_hugebitch(data, filename)
            outputs.append({
                "filename": f"{stem}_hugebitch.mp4",
                "data": result,
                "content_type": "video/mp4",
            })
        except Exception as e:
            logger.error(f"hugebitch failed for {filename}: {e}", exc_info=True)
    if not outputs:
        return [], "No image to add the Huge Bitch clip to."
    return outputs, f"🗣️ Huge Bitch ({_human_size(sum(len(o['data']) for o in outputs))})"


def _sleepwell_audio_path() -> str:
    """First existing sleepwell mp3 from the candidate list ("" if none)."""
    for p in _SLEEPWELL_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_sleepwell(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing the sleepwell clip over it. MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _sleepwell_audio_path()
    if not audio:
        raise RuntimeError("Sleepwell audio (assets/sleepwell.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_SLEEPWELL_DURATION)


def sleepwell_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a sleepwell MP4 (mirrors whoabuddy_attachments)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_sleepwell(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_sleepwell.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 😴 Sleep Well\n\n😴 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"sleepwell failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


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
