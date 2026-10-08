"""Auto-split from the original effects_service.py monolith. No behavior change."""
from . import _sound_on_still
from ._common import List, OutputFile, Path, Tuple, _ADAMSFAMILY_AUDIO_CANDIDATES, _ADAMSFAMILY_DURATION, _BIKE_AUDIO_CANDIDATES, _BIKE_DURATION, _CHARLIESANGLES_AUDIO_CANDIDATES, _CHARLIESANGLES_DURATION, _CHIMP_AUDIO_CANDIDATES, _CHIMP_DURATION, _CHIMP_GIF_CANDIDATES, _CLAY_AUDIO_CANDIDATES, _CLAY_DURATION, _CLAY_OVERLAY_CANDIDATES, _CONSIDER_PNG_CANDIDATES, _DARKNESS_AUDIO_CANDIDATES, _DARKNESS_DURATION, _DIFFERENTSTROKE_AUDIO_CANDIDATES, _DIFFERENTSTROKE_DURATION, _DONTWANTTOWAIT_AUDIO_CANDIDATES, _DONTWANTTOWAIT_DURATION, _FREEBIRD_AUDIO_CANDIDATES, _FREEBIRD_DURATION, _FUTURAMA_AUDIO_CANDIDATES, _FUTURAMA_DURATION, _HAPPYDAYS_AUDIO_CANDIDATES, _HAPPYDAYS_DURATION, _HARLEM_AUDIO_CANDIDATES, _HARLEM_DURATION, _JOBS_AUDIO_CANDIDATES, _JOBS_DURATION, _KANYE_AUDIO_CANDIDATES, _KANYE_DURATION, _LIBERAL_AUDIO_CANDIDATES, _LIBERAL_DURATION, _MIXALOT_AUDIO_CANDIDATES, _MIXALOT_DURATION, _MOVING_AUDIO_CANDIDATES, _MOVING_DURATION, _NONEMATTERS_AUDIO_CANDIDATES, _NONEMATTERS_DURATION, _MUNSTERS_AUDIO_CANDIDATES, _MUNSTERS_DURATION, _ONEPIECE_AUDIO_CANDIDATES, _ONEPIECE_DURATION, _OVERTAKEN_AUDIO_CANDIDATES, _OVERTAKEN_DURATION, _REE_AUDIO_CANDIDATES, _REE_DURATION, _SEINFELD_AUDIO_CANDIDATES, _SEINFELD_DURATION, _SOPRANOS_AUDIO_CANDIDATES, _SOPRANOS_DURATION, _STRANGERTHINGS_AUDIO_CANDIDATES, _STRANGERTHINGS_DURATION, _UWU_AUDIO_CANDIDATES, _UWU_DURATION, _NAMI_OVERLAY_CANDIDATES, _NAMI_AUDIO_CANDIDATES, _NAMI_DURATION, _MENTIONED_OVERLAY_CANDIDATES, _MENTIONED_AUDIO_CANDIDATES, _MENTIONED_DURATION, _meme_font_path, _UWU_OVERLAY_CANDIDATES, _WASTELAND_AUDIO_CANDIDATES, _WASTELAND_DURATION, _XMEN_AUDIO_CANDIDATES, _XMEN_DURATION, _alive_or_still, _human_size, io, is_image, logger, os


def add_jerry(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Composite the Jerry stand-up cutout over an image, set to the Seinfeld theme. MP4 bytes.

    `seinfeld` is the same theme over your UNTOUCHED image; `jerry` puts Jerry in the frame doing the
    bit. Deliberately reuses _seinfeld_audio_path — one mp3 on disk, two effects — and composites with
    the shared reaction-overlay helper (carl/soyjack), so the cutout is placed by the same rules.
    """
    from app.services.media_service import image_audio_to_video
    from .character import _add_reaction_overlay
    audio = globals()["_seinfeld_audio_path"]()   # built from the _SOUND_ON_STILL table below
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


def _nami_overlay_path() -> str:
    for p in _NAMI_OVERLAY_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def _nami_audio_path() -> str:
    for p in _NAMI_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_nami(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Nami, money bags for pupils, rubbing her hands over the image -- with a ka-ching. MP4 bytes."""
    from app.services.media_service import image_gif_overlay_video
    overlay = _nami_overlay_path()
    if not overlay:
        raise RuntimeError("nami overlay (assets/nami_money.mov) is missing on the server")
    return image_gif_overlay_video(image_data, source_filename, overlay,
                                   duration=_NAMI_DURATION, audio_path=_nami_audio_path() or None,
                                   height_frac=0.6)


def _first_existing(paths) -> str:
    for p in paths:
        if p and os.path.exists(p):
            return p
    return ""


MENTIONED_ASK = "Say what got mentioned, e.g. `mentioned pizza`."


def mentioned_caption(word: str) -> str:
    """'michigan' -> 'MICHIGAN MENTIONED'. No word -> "": the thing that got mentioned is the person's to say
    ("Users should add it") -- there is no default to fall back on."""
    w = " ".join(str(word or "").split())[:60].strip()
    return (w + " MENTIONED").upper() if w else ""


# THE CAPTION AND THE GIRL SHARE THE FRAME, NEVER A PIXEL. She stood at full height and the caption was drawn
# over her, sized from the frame; on a small image the caption grew to a third of it and she disappeared behind it
# ("character went behind the text and the text was too big"). The caption now owns a band at the top, she stands
# in the rest, and the two fractions add up to less than the whole -- tests/test_mentioned_layout.py checks it.
MENTIONED_CAPTION_BAND = 0.24        # the caption block never reaches below this fraction of the height
MENTIONED_GIRL_HEIGHT = 0.74         # she is this fraction of the height, anchored to the bottom


def add_mentioned(image_data: bytes, source_filename: str = "image.jpg", word: str = "") -> bytes:
    """The "<THING> MENTIONED" meme: a cheering anime girl hops over the image, the caption above her. MP4 bytes."""
    from app.services.media_service import caption_video, image_gif_overlay_video
    overlay = _first_existing(_MENTIONED_OVERLAY_CANDIDATES)
    if not overlay:
        raise RuntimeError("mentioned overlay (assets/mentioned_cheer.mov) is missing on the server")
    clip = image_gif_overlay_video(image_data, source_filename, overlay,
                                   duration=_MENTIONED_DURATION,
                                   audio_path=_first_existing(_MENTIONED_AUDIO_CANDIDATES) or None,
                                   height_frac=MENTIONED_GIRL_HEIGHT)
    return caption_video(clip, mentioned_caption(word), _meme_font_path(),
                         position="top", max_height_frac=MENTIONED_CAPTION_BAND)


def mentioned_attachments(
    attachments: List[Tuple[str, bytes, str]], word: str = "",
) -> Tuple[List[OutputFile], str]:
    """`mentioned <thing>` on the first image attachment (animated video output)."""
    if not mentioned_caption(word):
        return [], MENTIONED_ASK
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_mentioned(data, filename, word)
        out: OutputFile = {"filename": f"{stem}_mentioned.mp4", "data": result, "content_type": "video/mp4"}
        return [out], f"## 🎉 {mentioned_caption(word).title()}\n\n🎉 {filename}: {_human_size(len(result))}"
    except Exception as e:
        logger.error(f"mentioned failed for {filename}: {e}", exc_info=True)
        return [], f"\u274c {filename}: {e}"


def nami_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Nami on the first image attachment (animated video output), like uwu_attachments."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_nami(data, filename)
        out: OutputFile = {"filename": f"{stem}_nami.mp4", "data": result, "content_type": "video/mp4"}
        return [out], f"## 💰 Nami\n\n💰 {filename}: {_human_size(len(result))}"
    except Exception as e:
        logger.error(f"nami failed for {filename}: {e}", exc_info=True)
        return [], f"\u274c {filename}: {e}"


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

# A sound over your picture: (name, heading, emoji, mp3 candidates, seconds, message when the mp3 is missing).
# Each row becomes `_<name>_audio_path`, `add_<name>` and `<name>_attachments` -- see _sound_on_still.py.
_SOUND_ON_STILL = [
    ('munsters', '🧛 Munsters', '🧛', _MUNSTERS_AUDIO_CANDIDATES, _MUNSTERS_DURATION,
     'Munsters audio (assets/munsters.mp3) is missing on the server'),
    ('happydays', '🕺 Happy Days', '🕺', _HAPPYDAYS_AUDIO_CANDIDATES, _HAPPYDAYS_DURATION,
     'Happy Days audio (assets/happydays.mp3) is missing on the server'),
    ('dontwanttowait', "🌊 Don't Want to Wait", '🌊', _DONTWANTTOWAIT_AUDIO_CANDIDATES, _DONTWANTTOWAIT_DURATION,
     "Don't Want to Wait audio (assets/dontwanttowait.mp3) is missing on the server"),
    ('strangerthings', '🔦 Stranger Things', '🔦', _STRANGERTHINGS_AUDIO_CANDIDATES, _STRANGERTHINGS_DURATION,
     'Stranger Things audio (assets/strangerthings.mp3) is missing on the server'),
    ('adamsfamily', '🖤 Addams Family', '🖤', _ADAMSFAMILY_AUDIO_CANDIDATES, _ADAMSFAMILY_DURATION,
     'Addams Family audio (assets/adamsfamily.mp3) is missing on the server'),
    ('xmen', '❌ X-Men', '❌', _XMEN_AUDIO_CANDIDATES, _XMEN_DURATION,
     'X-Men audio (assets/xmen.mp3) is missing on the server'),
    ('futurama', '🚀 Futurama', '🚀', _FUTURAMA_AUDIO_CANDIDATES, _FUTURAMA_DURATION,
     'Futurama audio (assets/futurama.mp3) is missing on the server'),
    ('charliesangles', "👼 Charlie's Angels", '👼', _CHARLIESANGLES_AUDIO_CANDIDATES, _CHARLIESANGLES_DURATION,
     "Charlie's Angels audio (assets/charliesangles.mp3) is missing on the server"),
    ('differentstroke', "🌍 Diff'rent Strokes", '🌍', _DIFFERENTSTROKE_AUDIO_CANDIDATES, _DIFFERENTSTROKE_DURATION,
     "Diff'rent Strokes audio (assets/differentstroke.mp3) is missing on the server"),
    ('seinfeld', '🎤 Seinfeld', '🎤', _SEINFELD_AUDIO_CANDIDATES, _SEINFELD_DURATION,
     'Seinfeld audio (assets/seinfeld.mp3) is missing on the server'),
    ('onepiece', '🏴\u200d☠️ One Piece', '🏴\u200d☠️', _ONEPIECE_AUDIO_CANDIDATES, _ONEPIECE_DURATION,
     'One Piece audio (assets/onepiece.mp3) is missing on the server'),
    ('overtaken', '🏎️ Overtaken', '🏎️', _OVERTAKEN_AUDIO_CANDIDATES, _OVERTAKEN_DURATION,
     'Overtaken audio (assets/overtaken.mp3) is missing on the server'),
    ('sopranos', '🇮🇹 Sopranos', '🇮🇹', _SOPRANOS_AUDIO_CANDIDATES, _SOPRANOS_DURATION,
     'Sopranos audio (assets/sopranos.mp3) is missing on the server'),
    ('freebird', '🦅 Freebird', '🦅', _FREEBIRD_AUDIO_CANDIDATES, _FREEBIRD_DURATION,
     'Freebird audio (assets/freebird.mp3) is missing on the server'),
    ('kanye', '🐻 Kanye', '🐻', _KANYE_AUDIO_CANDIDATES, _KANYE_DURATION,
     'Kanye audio (assets/kanye.mp3) is missing on the server'),
    ('darkness', '🌑 Darkness', '🌑', _DARKNESS_AUDIO_CANDIDATES, _DARKNESS_DURATION,
     'Darkness audio (assets/darkness.mp3) is missing on the server'),
    ('bike', '🚲 Bike', '🚲', _BIKE_AUDIO_CANDIDATES, _BIKE_DURATION,
     'Bike audio (assets/bike.mp3) is missing on the server'),
    ('jobs', '💼 Jobs', '💼', _JOBS_AUDIO_CANDIDATES, _JOBS_DURATION,
     'Jobs audio (assets/jobs.mp3) is missing on the server'),
    ('ree', '😡 Ree', '😡', _REE_AUDIO_CANDIDATES, _REE_DURATION,
     'Ree audio (assets/ree.mp3) is missing on the server'),
    ('liberal', '🗽 Liberal', '🗽', _LIBERAL_AUDIO_CANDIDATES, _LIBERAL_DURATION,
     'Liberal audio (assets/liberal.mp3) is missing on the server'),
    ('moving', '📦 Moving', '📦', _MOVING_AUDIO_CANDIDATES, _MOVING_DURATION,
     'Moving audio (assets/moving.mp3) is missing on the server'),
    ('harlem', '🕺 Harlem', '🕺', _HARLEM_AUDIO_CANDIDATES, _HARLEM_DURATION,
     'Harlem audio (assets/harlem.mp3) is missing on the server'),
    ('wasteland', '🎸 Wasteland', '🎸', _WASTELAND_AUDIO_CANDIDATES, _WASTELAND_DURATION,
     'Wasteland audio (assets/wasteland.mp3) is missing on the server'),
    ('nonematters', '🤷 None of this matters', '🤷', _NONEMATTERS_AUDIO_CANDIDATES, _NONEMATTERS_DURATION,
     'Nonematters audio (assets/nonematters.mp3) is missing on the server'),
    ('mixalot', '🍑 Mixalot', '🍑', _MIXALOT_AUDIO_CANDIDATES, _MIXALOT_DURATION,
     'Mixalot audio (assets/mixalot.mp3) is missing on the server'),
]
for _row in _SOUND_ON_STILL:
    _sound_on_still.register(globals(), *_row)
del _row
