"""Auto-split from the original effects_service.py monolith. No behavior change."""
from ._common import List, OutputFile, Path, Tuple, _apply_motion, _human_size, is_image, logger

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
