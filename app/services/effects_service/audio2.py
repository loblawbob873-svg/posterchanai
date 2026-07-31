"""Auto-split from the original effects_service.py monolith. No behavior change."""
from ._common import List, OutputFile, Path, Tuple, _ADAMSFAMILY_AUDIO_CANDIDATES, _ADAMSFAMILY_DURATION, _BIKE_AUDIO_CANDIDATES, _BIKE_DURATION, _CHARLIESANGLES_AUDIO_CANDIDATES, _CHARLIESANGLES_DURATION, _CHIMP_AUDIO_CANDIDATES, _CHIMP_DURATION, _CHIMP_GIF_CANDIDATES, _CLAY_AUDIO_CANDIDATES, _CLAY_DURATION, _CLAY_OVERLAY_CANDIDATES, _CONSIDER_PNG_CANDIDATES, _DARKNESS_AUDIO_CANDIDATES, _DARKNESS_DURATION, _DIFFERENTSTROKE_AUDIO_CANDIDATES, _DIFFERENTSTROKE_DURATION, _DONTWANTTOWAIT_AUDIO_CANDIDATES, _DONTWANTTOWAIT_DURATION, _FREEBIRD_AUDIO_CANDIDATES, _FREEBIRD_DURATION, _FUTURAMA_AUDIO_CANDIDATES, _FUTURAMA_DURATION, _HAPPYDAYS_AUDIO_CANDIDATES, _HAPPYDAYS_DURATION, _HARLEM_AUDIO_CANDIDATES, _HARLEM_DURATION, _JOBS_AUDIO_CANDIDATES, _JOBS_DURATION, _KANYE_AUDIO_CANDIDATES, _KANYE_DURATION, _LIBERAL_AUDIO_CANDIDATES, _LIBERAL_DURATION, _MIXALOT_AUDIO_CANDIDATES, _MIXALOT_DURATION, _MOVING_AUDIO_CANDIDATES, _MOVING_DURATION, _NONEMATTERS_AUDIO_CANDIDATES, _NONEMATTERS_DURATION, _MUNSTERS_AUDIO_CANDIDATES, _MUNSTERS_DURATION, _ONEPIECE_AUDIO_CANDIDATES, _ONEPIECE_DURATION, _OVERTAKEN_AUDIO_CANDIDATES, _OVERTAKEN_DURATION, _REE_AUDIO_CANDIDATES, _REE_DURATION, _SEINFELD_AUDIO_CANDIDATES, _SEINFELD_DURATION, _SOPRANOS_AUDIO_CANDIDATES, _SOPRANOS_DURATION, _STRANGERTHINGS_AUDIO_CANDIDATES, _STRANGERTHINGS_DURATION, _UWU_AUDIO_CANDIDATES, _UWU_DURATION, _UWU_OVERLAY_CANDIDATES, _WASTELAND_AUDIO_CANDIDATES, _WASTELAND_DURATION, _XMEN_AUDIO_CANDIDATES, _XMEN_DURATION, _alive_or_still, _human_size, io, is_image, logger, os

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


def add_jerry(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Composite the Jerry stand-up cutout over an image, set to the Seinfeld theme. MP4 bytes.

    `seinfeld` is the same theme over your UNTOUCHED image; `jerry` puts Jerry in the frame doing the
    bit. Deliberately reuses _seinfeld_audio_path — one mp3 on disk, two effects — and composites with
    the shared reaction-overlay helper (carl/soyjack), so the cutout is placed by the same rules.
    """
    from app.services.media_service import image_audio_to_video
    from .character import _add_reaction_overlay
    audio = _seinfeld_audio_path()
    if not audio:
        raise RuntimeError("Seinfeld audio (assets/seinfeld.mp3) is missing on the server")
    still = _add_reaction_overlay(image_data, "jerry")   # raises if the cutout art is missing
    return image_audio_to_video(still, source_filename, audio, duration=_SEINFELD_DURATION)


def jerry_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a jerry MP4. Mirrors seinfeld_attachments."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_jerry(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_jerry.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🎤 Jerry\n\n🎤 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"jerry failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


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


def _consider_png_path() -> str:
    """First existing consider png from the candidate list ("" if none)."""
    for p in _CONSIDER_PNG_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def _strip_baked_caption(ov):
    """Drop the baked-in "Consider the Following" plate off the top of the cutout so the caption can be
    drawn as a speech bubble instead. The plate is a solid grey/white banner: from the top, drop every
    row whose visible pixels are almost all achromatic, stopping at the (colourful) art. Leaves the
    image untouched if that would eat a third of it — i.e. the asset has no baked banner."""
    W, H = ov.size
    px = ov.load()
    cut = 0
    for y in range(int(H * 0.34)):
        vis = [px[x, y] for x in range(W) if px[x, y][3] > 8]
        if len(vis) < W * 0.25:
            break                                   # past the plate (or a gap above it)
        if sum(1 for p in vis if max(p[:3]) - min(p[:3]) <= 24) < len(vis) * 0.9:
            break                                   # colour — this is the art, not the plate
        cut = y + 1
    if cut:
        ov = ov.crop((0, cut, W, H))
    return ov.crop(ov.getbbox() or (0, 0, ov.size[0], ov.size[1]))


_CONSIDER_MOUTH: dict = {}


def _consider_mouth_frac(png_path: str, ov):
    """Her mouth line as a fraction of the (banner-stripped) cutout's height, so the bubble sits
    level with it like the other character effects. Cached per asset — the detection behind it costs
    up to a second and the art never changes."""
    from .character import mouth_frac
    try:
        key = (png_path, os.path.getmtime(png_path))
    except OSError:
        return None
    if key not in _CONSIDER_MOUTH:
        _CONSIDER_MOUTH[key] = mouth_frac(ov)
    return _CONSIDER_MOUTH[key]


def _figure_columns(ov) -> Tuple[int, int]:
    """(left, right) of the FIGURE inside the cutout, ignoring the thin noose hanging beside her.
    Her body is a solid mass — columns that are opaque down most of the frame — while the rope is a
    few pixels wide, so take the run of dense columns containing the densest one. Without this the
    speech bubble hugs the ROPE (the cutout's true left edge) and sits marooned out in the gutter."""
    W, H = ov.size
    px = ov.split()[-1].load()
    cov = [sum(1 for y in range(H) if px[x, y] > 8) / H for x in range(W)]
    peak = max(range(W), key=lambda x: cov[x])
    if cov[peak] < 0.5:
        return 0, W                                  # no solid mass — treat the whole cutout as the figure
    left = peak
    while left > 0 and cov[left - 1] >= 0.5:
        left -= 1
    right = peak
    while right < W - 1 and cov[right + 1] >= 0.5:
        right += 1
    return left, right


def add_consider(data: bytes, caption: str = "Consider the following") -> bytes:
    """Composite the (transparent) "consider the following" cutout over an image, scaled large and
    anchored to the bottom-right, with the line said in a SPEECH BUBBLE — the same dialogue renderer
    `shrug`/`would`/`theraped` use (character.draw_dialogue_caption), so the two styles match. The
    cutout ships with the line baked into a grey plate; that plate is stripped first. Returns JPEG."""
    from PIL import Image, ImageOps
    from .character import draw_dialogue_caption
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
            ov = _strip_baked_caption(ov_src.convert("RGBA"))
        ow, oh = ov.size
        # Scale the cutout to ~80% of the image height, but never wider than 95% of
        # the image (so it still fits on portrait images), keeping its aspect ratio.
        scale = min(H * 0.80 / oh, W * 0.95 / ow)
        nw, nh = max(int(ow * scale), 1), max(int(oh * scale), 1)
        ov = ov.resize((nw, nh), Image.LANCZOS)
        # Anchor to the bottom-right corner (small margin).
        margin = max(int(W * 0.01), 2)
        x, y = W - nw - margin, H - nh - margin
        x, y = max(x, 0), max(y, 0)
        img.alpha_composite(ov, (x, y))
        # She says it: same bubble/font/fit rules as the shrug rabbi. Anchor on HER, not on the
        # cutout box (the noose hangs well to her left), and cap the bubble at her own width so it
        # stays the compact block the rabbi gets instead of filling the whole gutter.
        fl, fr = _figure_columns(ov)
        mf = _consider_mouth_frac(png, ov)
        draw_dialogue_caption(img, caption, y, x + fl, min(W, x + fr), band_cap=max(fr - fl, 1),
                              mouth_y=(y + int(nh * mf) if mf is not None else None))

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


def _uwu_overlay_path() -> str:
    """First existing uwu overlay video from the candidate list ("" if none)."""
    for p in _UWU_OVERLAY_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def _uwu_audio_path() -> str:
    """First existing uwu mp3 from the candidate list ("" if none)."""
    for p in _UWU_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_uwu(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Composite the dancing anime girl over an image, with the "uwu" voice clip. MP4 bytes."""
    from app.services.media_service import image_gif_overlay_video
    overlay = _uwu_overlay_path()
    if not overlay:
        raise RuntimeError("uwu overlay (assets/uwu_dance.mov) is missing on the server")
    # 0.55: she is TALLER than wide, so unlike the beavis pair this never runs out of frame width —
    # image_gif_overlay_video bounds the width anyway.
    return image_gif_overlay_video(image_data, source_filename, overlay,
                                   duration=_UWU_DURATION, audio_path=_uwu_audio_path() or None,
                                   height_frac=0.55)


def uwu_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Overlay the dancing anime girl on the first image attachment. Mirrors clay_attachments
    (animated video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_uwu(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_uwu.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## \U0001F97A UwU\n\n\U0001F97A {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"uwu failed for {filename}: {e}", exc_info=True)
        return [], f"\u274c {filename}: {e}"


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


def _nonematters_audio_path() -> str:
    """First existing nonematters mp3 from the candidate list ("" if none)."""
    for p in _NONEMATTERS_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_nonematters(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Turn a still image into a short MP4 playing Carl's "none of this matters". MP4 bytes."""
    from app.services.media_service import image_audio_to_video
    audio = _nonematters_audio_path()
    if not audio:
        raise RuntimeError("Nonematters audio (assets/nonematters.mp3) is missing on the server")
    return image_audio_to_video(image_data, source_filename, audio, duration=_NONEMATTERS_DURATION)


def nonematters_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a nonematters MP4. Mirrors
    mixalot_attachments (video output, routed through the bots' video path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    data = [(_f, _d) for _f, _d, _c in images] if len(images) > 1 else data
    stem = Path(filename).stem or "image"
    try:
        result = add_nonematters(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_nonematters.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🤷 None of this matters\n\n🤷 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"nonematters failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


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
