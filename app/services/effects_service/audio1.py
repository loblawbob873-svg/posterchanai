"""Auto-split from the original effects_service.py monolith. No behavior change."""
from . import _sound_on_still
from ._common import List, OutputFile, Path, Tuple, _AKBAR_AUDIO_CANDIDATES, _AKBAR_DURATION, _BEAVIS_AUDIO_CANDIDATES, _BEAVIS_DURATION, _BEAVIS_OVERLAY_CANDIDATES, _CHEERS_AUDIO_CANDIDATES, _CHEERS_DURATION, _CURB_AUDIO_CANDIDATES, _CURB_DURATION, _DEPRESSING_AUDIO_CANDIDATES, _DEPRESSING_DURATION, _FAHH_AUDIO_CANDIDATES, _FAHH_AUDIO_START, _FAHH_DURATION, _FBI_AUDIO_CANDIDATES, _FBI_DURATION, _FELIZ_AUDIO_CANDIDATES, _FELIZ_DURATION, _FELTEDTABLES_AUDIO_CANDIDATES, _FELTEDTABLES_DURATION, _GIGITY_AUDIO_CANDIDATES, _GIGITY_DURATION, _GONG_AUDIO_CANDIDATES, _GONG_DURATION, _HAVA_AUDIO_CANDIDATES, _HAVA_DURATION, _HELPME_AUDIO_CANDIDATES, _HELPME_DURATION, _HOOD_AUDIO_CANDIDATES, _HOOD_DURATION, _HORSE_AUDIO_CANDIDATES, _HORSE_DURATION, _KNIGHTRIDER_AUDIO_CANDIDATES, _KNIGHTRIDER_DURATION, _HUGEBITCH_AUDIO_CANDIDATES, _HUGEBITCH_DURATION, _INDIAN_AUDIO_CANDIDATES, _INDIAN_DURATION, _PRAYER_AUDIO_CANDIDATES, _PRAYER_DURATION, _REDEEM_AUDIO_CANDIDATES, _REDEEM_DURATION, _RETARD_AUDIO_CANDIDATES, _RETARD_DURATION, _REZE_AUDIO_CANDIDATES, _REZE_DANCE_CANDIDATES, _REZE_DURATION, _VIBE_AUDIO_CANDIDATES, _VIBE_DANCE_CANDIDATES, _VIBE_DURATION, _REBECCA_AUDIO_CANDIDATES, _REBECCA_DANCE_CANDIDATES, _REBECCA_DURATION, _MAKIMA_AUDIO_CANDIDATES, _MAKIMA_SHOOT_CANDIDATES, _MAKIMA_DURATION, _GURA_AUDIO_CANDIDATES, _GURA_POG_CANDIDATES, _GURA_DURATION, _ROBOCOP_AUDIO_CANDIDATES, _ROBOCOP_DURATION, _SETH_AUDIO_CANDIDATES, _SETH_DURATION, _SLEEPWELL_AUDIO_CANDIDATES, _SLEEPWELL_DURATION, _SMELL_AUDIO_CANDIDATES, _SMELL_DURATION, _TERMINATOR_AUDIO_CANDIDATES, _TERMINATOR_DURATION, _TITAN_AUDIO_CANDIDATES, _TITAN_DURATION, _WHOABUDDY_AUDIO_CANDIDATES, _WHOABUDDY_DURATION, _HEAT_AUDIO_CANDIDATES, _HEAT_DURATION, _DIARRHEA_AUDIO_CANDIDATES, _DIARRHEA_DURATION, _YAKETY_AUDIO_CANDIDATES, _YAKETY_DURATION, _YAMETE_AUDIO_CANDIDATES, _YAMETE_DURATION, _human_size, _pad_audio_to_duration, is_image, logger, os


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

# A sound over your picture: (name, heading, emoji, mp3 candidates, seconds, message when the mp3 is missing).
# Each row becomes `_<name>_audio_path`, `add_<name>` and `<name>_attachments` -- see _sound_on_still.py.
_SOUND_ON_STILL = [
    ('hava', '🎻 Hava', '🎻', _HAVA_AUDIO_CANDIDATES, _HAVA_DURATION,
     'Hava Nagila audio (assets/hava.mp3) is missing on the server'),
    ('indian', '🇮🇳 Indian', '🇮🇳', _INDIAN_AUDIO_CANDIDATES, _INDIAN_DURATION,
     'Indian audio (assets/indian.mp3) is missing on the server'),
    ('yakety', '🎷 Yakety Sax', '🎷', _YAKETY_AUDIO_CANDIDATES, _YAKETY_DURATION,
     'Yakety Sax audio (assets/yakety.mp3) is missing on the server'),
    ('yamete', '🛑 Yamete', '🛑', _YAMETE_AUDIO_CANDIDATES, _YAMETE_DURATION,
     'Yamete audio (assets/yamete.mp3) is missing on the server'),
    ('curb', '😬 Curb', '😬', _CURB_AUDIO_CANDIDATES, _CURB_DURATION,
     'Curb theme audio (assets/curb.mp3) is missing on the server'),
    ('depressing', '😢 Depressing', '😢', _DEPRESSING_AUDIO_CANDIDATES, _DEPRESSING_DURATION,
     'Depressing audio (assets/depressing.mp3) is missing on the server'),
    ('helpme', '🆘 Helpme', '🆘', _HELPME_AUDIO_CANDIDATES, _HELPME_DURATION,
     'Helpme audio (assets/helpme.mp3) is missing on the server'),
    ('gong', '🔔 Gong', '🔔', _GONG_AUDIO_CANDIDATES, _GONG_DURATION,
     'Gong audio (assets/gong.mp3) is missing on the server'),
    ('fbi', '🚨 FBI', '🚨', _FBI_AUDIO_CANDIDATES, _FBI_DURATION,
     'FBI audio (assets/fbi.mp3) is missing on the server'),
    ('redeem', '💳 Redeem', '💳', _REDEEM_AUDIO_CANDIDATES, _REDEEM_DURATION,
     'Redeem audio (assets/redeem.mp3) is missing on the server'),
    ('gigity', '😏 Gigity', '😏', _GIGITY_AUDIO_CANDIDATES, _GIGITY_DURATION,
     'Gigity audio (assets/gigity.mp3) is missing on the server'),
    ('smell', '👃 Smell', '👃', _SMELL_AUDIO_CANDIDATES, _SMELL_DURATION,
     'Smell audio (assets/smell.mp3) is missing on the server'),
    ('hood', '🏚️ Hood', '🏚️', _HOOD_AUDIO_CANDIDATES, _HOOD_DURATION,
     'Hood audio (assets/hood.mp3) is missing on the server'),
    ('akbar', '🕌 Akbar', '🕌', _AKBAR_AUDIO_CANDIDATES, _AKBAR_DURATION,
     'Akbar audio (assets/akbar.mp3) is missing on the server'),
    ('retard', '⚠️ Retard', '⚠️', _RETARD_AUDIO_CANDIDATES, _RETARD_DURATION,
     'Retard audio (assets/retard.mp3) is missing on the server'),
    ('whoabuddy', '🤠 Whoabuddy', '🤠', _WHOABUDDY_AUDIO_CANDIDATES, _WHOABUDDY_DURATION,
     'Whoabuddy audio (assets/whoabuddy.mp3) is missing on the server'),
    ('heat', '🔥 Heat of the Moment', '🔥', _HEAT_AUDIO_CANDIDATES, _HEAT_DURATION,
     'Heat audio (assets/heat.mp3) is missing on the server'),
    ('diarrhea', '💩 Diarrhea', '💩', _DIARRHEA_AUDIO_CANDIDATES, _DIARRHEA_DURATION,
     'Diarrhea audio (assets/diarrhea.mp3) is missing on the server'),
    ('seth', '🎬 Seth', '🎬', _SETH_AUDIO_CANDIDATES, _SETH_DURATION,
     'Seth audio (assets/seth.mp3) is missing on the server'),
    ('robocop', '🤖 Robocop', '🤖', _ROBOCOP_AUDIO_CANDIDATES, _ROBOCOP_DURATION,
     'Robocop audio (assets/robocop.mp3) is missing on the server'),
    ('titan', '🗿 Titan', '🗿', _TITAN_AUDIO_CANDIDATES, _TITAN_DURATION,
     'Titan audio (assets/titan.mp3) is missing on the server'),
    ('terminator', '🦾 Terminator', '🦾', _TERMINATOR_AUDIO_CANDIDATES, _TERMINATOR_DURATION,
     'Terminator audio (assets/terminator.mp3) is missing on the server'),
    ('feliz', '🎉 Feliz', '🎉', _FELIZ_AUDIO_CANDIDATES, _FELIZ_DURATION,
     'Feliz audio (assets/feliz.mp3) is missing on the server'),
    ('sleepwell', '😴 Sleep Well', '😴', _SLEEPWELL_AUDIO_CANDIDATES, _SLEEPWELL_DURATION,
     'Sleepwell audio (assets/sleepwell.mp3) is missing on the server'),
    ('prayer', '🙏 Prayer', '🙏', _PRAYER_AUDIO_CANDIDATES, _PRAYER_DURATION,
     'Prayer audio (assets/prayer.mp3) is missing on the server'),
    ('feltedtables', '🎱 Felted Tables', '🎱', _FELTEDTABLES_AUDIO_CANDIDATES, _FELTEDTABLES_DURATION,
     'Felted-tables audio (assets/feltedtables.mp3) is missing on the server'),
    ('cheers', '🍻 Cheers', '🍻', _CHEERS_AUDIO_CANDIDATES, _CHEERS_DURATION,
     'Cheers audio (assets/cheers.mp3) is missing on the server'),
]
for _row in _SOUND_ON_STILL:
    _sound_on_still.register(globals(), *_row)
del _row
