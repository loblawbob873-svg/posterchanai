"""Auto-split from the original effects_service.py monolith. No behavior change."""
from ._common import List, OutputFile, Path, _CHARACTERS, _CHARS_DIR_CANDIDATES, _SHRUG_AUDIO_CANDIDATES, _SHRUG_DURATION, _SOYJACK_AUDIO_CANDIDATES, _SOYJACK_DURATION, logger, os

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


def _composite_char_bottom_center(base, char_path: str, height_frac: float = 0.52,
                                  max_width_frac: float = 0.48, want_mouth: bool = True):
    """Place the character bottom-CENTRE (the pointing-up meme anchor) rather than bottom-right like
    apply_character. Returns (image, top_y, left_x, right_x, mouth_y, row_edges) so a caption can be
    placed in whichever gutter beside her is wider, clear of the art, and level with her mouth —
    `row_edges` (see _row_edges) is what lets it hug her actual outline there rather than the box."""
    from PIL import Image as _Img
    W, H = base.size
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
    # The width cap exists to leave a gutter for the caption, so it is derived from what the
    # caption needs, not guessed: the bubble wants `W*0.18` and each gutter is
    # (W - cw)/2 - 2*margin with margin = 0.03W, so cw <= 0.52W still satisfies it. 0.48 keeps a
    # little slack. It was 0.34, which a WIDE pose pays for: the shrugging rabbi (arms out, 0.66
    # aspect) hit the cap on any portrait photo and rendered at 29% of the height.
    # Size by HEIGHT, then cap her actual WIDTH — the two used to be conflated as
    # `height_frac * min(W, H)`, which is a width cap only by accident. On a 1080x1920 phone photo
    # that made her 0.38*1080 tall, i.e. 21% of the frame — the "too small on a high-res photo"
    # complaint — while what the cap was really protecting (a gutter each side for the caption)
    # depends on how WIDE she is, not on the shorter edge. Capping the width directly lets the
    # height frac be honest on every aspect ratio.
    ch = max(2, int(H * height_frac))
    cw = max(1, int(char.width * ch / char.height))
    max_cw = max(2, int(W * max_width_frac))
    if cw > max_cw:
        ch = max(2, int(ch * max_cw / cw))
        cw = max_cw
    char = char.resize((cw, ch), _Img.LANCZOS)
    y = max(0, H - ch)
    x = max(0, (W - cw) // 2)
    base.alpha_composite(char, (x, y))
    # want_mouth=False for the caption-less reaction overlays: locating the mouth runs a face
    # detector (loads insightface, up to ~1.2s on a cold process) purely to place a speech bubble
    # that those effects never draw.
    mf = _char_mouth_frac(char_path) if want_mouth else None
    return (base, y, x, min(W, x + cw), (y + int(ch * mf) if mf is not None else None),
            _row_edges(char, x, y, W, H))


def _row_edges(char, x: int, y: int, W: int, H: int):
    """Per-row (left, right) of the composited figure's SILHOUETTE, in base coordinates; (-1, -1) for
    rows it does not occupy. None if it cannot be measured.

    Exists because `char_right` is a bounding BOX edge, i.e. the figure's widest point — which is
    almost always the bottom (shoulders, a flared coat, a pair of soyjaks). The speech bubble is
    placed level with the MOUTH, where a figure is far narrower, so hugging the box edge left an
    obvious empty channel between the character and their own speech: the bubble read as pinned to
    the frame rather than as theirs. Measured per row, the bubble can close that gap without any risk
    of overlapping the art, because the caller only ever consults the rows the bubble itself covers.
    """
    try:
        import numpy as _np
        a = _np.asarray(char)
        if a.ndim != 3 or a.shape[2] < 4:
            return None
        solid = a[..., 3] > 8
        out = _np.full((H, 2), -1, dtype=_np.int32)
        for r in range(solid.shape[0]):
            by = y + r
            if by < 0 or by >= H:
                continue
            xs = _np.where(solid[r])[0]
            if len(xs):
                out[by] = (max(0, x + int(xs.min())), min(W, x + int(xs.max()) + 1))
        return out
    except Exception as e:
        logger.debug("row-edge measurement failed: %s", e)
        return None


_MOUTH_CACHE = {}


def mouth_frac(img) -> float | None:
    """Where the character's MOUTH sits, as a fraction of `img`'s height — the line a speech bubble
    should be level with. None when no face is found (the caller keeps its own fallback).

    Detected on the TOP 45% of the figure, upscaled: on a full-body pose the head is a few percent
    of the frame and the detectors simply miss it at native scale (`would` returned nothing at all,
    which is how his bubble ended up down by his knees). The art is composited onto white first —
    a bare alpha cutout gives the cascades no contrast to work with.
    """
    try:
        from PIL import Image as _Img
        from .faces import _locate_mouth
        from io import BytesIO as _BIO
        bb = img.getbbox()
        if bb:
            img = img.crop(bb)
        W, H = img.size
        # Search the top 45% FIRST (cheap, and on a full-body pose that is where the head is), then
        # widen. A fixed 45% window silently failed on every BUST-framed character: when the head
        # fills the frame the mouth sits around 75-80% of the figure, outside the crop entirely, so
        # detection returned None and the caller fell back to "38% of the figure" — eye level. That is
        # why the bubble on old Steve Rogers and on the teacher sat too high.
        for frac in (0.45, 0.80, 1.0):
            top = img.crop((0, 0, W, max(2, int(H * frac))))
            scale = max(1.0, 560.0 / max(1, top.width))
            if scale > 1.0:
                top = top.resize((int(top.width * scale), int(top.height * scale)), _Img.LANCZOS)
            flat = _Img.new("RGB", top.size, (255, 255, 255))
            flat.paste(top, mask=top.split()[-1] if top.mode == "RGBA" else None)
            buf = _BIO(); flat.save(buf, "PNG")
            hit = _locate_mouth(buf.getvalue())
            if hit:
                return float(hit[1] / scale / H)
        return None
    except Exception as e:
        logger.debug("mouth detection failed: %s", e)
        return None


def _char_mouth_frac(char_path: str) -> float | None:
    """mouth_frac() for a character ASSET, cached per file — detection costs 0.1-1.2s and the art
    never changes between renders, so it must not run per request."""
    try:
        key = (char_path, os.path.getmtime(char_path))
    except OSError:
        return None
    if key not in _MOUTH_CACHE:
        try:
            _MOUTH_CACHE[key] = mouth_frac(_character_still(char_path))
        except Exception:
            _MOUTH_CACHE[key] = None
    return _MOUTH_CACHE[key]


def _draw_speech_bubble(draw, cx, cy, tw, th, toward_left: bool, scale: int):
    """A rounded dialogue bubble behind the caption with a tail aimed at the character, so the text
    reads as her SAYING it rather than as a caption laid over the picture. Drawn under the text; the
    caller then renders the words in dark ink on the white fill."""
    # Generous padding: with the caption measured at its real anchor the bubble hugs the ink exactly,
    # and a third of `scale` left the words looking wedged against the border on every side.
    pad = max(9, scale // 2)
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


def add_nodontthinkiwill(data: bytes, caption: str = "NO, I DON'T THINK I WILL") -> bytes:
    """`nodontthinkiwill` — old Steve Rogers declining (see _add_pointing_meme).

    He does not point at anything; the bubble reads as what he is SAYING, which is the meme. Caption
    defaults to the line and an argument replaces it.
    """
    return _add_pointing_meme(data, "nodontthinkiwill", caption)


def add_nothingeverhappens(data: bytes, caption: str = "NOTHING EVER HAPPENS") -> bytes:
    """`nothingeverhappens` — the angry teacher pointing at his board (see _add_pointing_meme).

    The caption defaults to the meme's own line, so the effect needs no argument; passing one
    replaces it, which is the whole joke for anything else you want him to be lecturing about.
    """
    return _add_pointing_meme(data, "nothingeverhappens", caption)


def add_theraped(data: bytes, caption: str = "The Raped") -> bytes:
    """`theraped` — the imageboard pointing-up format (see _add_pointing_meme)."""
    return _add_pointing_meme(data, "theraped", caption)


def add_would(data: bytes, caption: str = "WOULD") -> bytes:
    """`would` — the same pointing-up format with the old man. Same renderer, different art + caption,
    so the two can never drift apart in layout."""
    return _add_pointing_meme(data, "would", caption, fallback="theraped")


def add_shrug(data: bytes, caption: str = "Whaddya gonna do?") -> bytes:
    """`shrug` — resigned rabbi, palms up, saying "Whaddya gonna do?". Same character+dialogue renderer;
    the shrug.png pose gestures on its own. Returns the STILL JPEG frame; the
    video (with the shrug audio clip) is built in shrug_attachments — mirrors add_diarrhea's split."""
    return _add_pointing_meme(data, "shrug", caption, fallback="would")


def _shrug_audio_path() -> str:
    """First existing shrug mp3 from the candidate list ("" if none). Exposing this makes `shrug`
    auto-discovered by meme_builder_service.sound_names()."""
    for p in _SHRUG_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def _soyjack_audio_path() -> str:
    """First existing soyjack mp3 from the candidate list ("" if none). The `_<name>_audio_path`
    NAME is load-bearing: meme_builder_service.sound_names() discovers sounds by that regex, so
    defining it is what puts `soyjack` in the Meme Builder's sound list."""
    for p in _SOYJACK_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def add_soyjack_video(data: bytes, source_filename: str = "image.jpg") -> bytes:
    """The soyjack still, looped under the crying-soyjak sound → MP4. Mirrors add_shrug_video; the
    branded end-card is appended later by CommandService._brand_effect_videos like every other
    MOTION_EFFECTS video."""
    from app.services.media_service import image_audio_to_video
    still = add_soyjack(data)
    audio = _soyjack_audio_path()
    if not audio:
        raise RuntimeError("Soyjack audio (assets/soyjack.mp3) is missing on the server")
    return image_audio_to_video(still, "soyjack.jpg", audio, duration=_SOYJACK_DURATION)


def add_shrug_video(data: bytes, caption: str = "Whaddya gonna do?",
                    source_filename: str = "image.jpg") -> bytes:
    """Render the shrug meme still, then loop it under the shrug audio clip → MP4 bytes.
    Mirrors add_diarrhea (image → image_audio_to_video); the branding end-card is appended
    later by CommandService._brand_effect_videos, same as the other MOTION_EFFECTS videos."""
    from app.services.media_service import image_audio_to_video
    still = add_shrug(data, caption)
    audio = _shrug_audio_path()
    if not audio:
        raise RuntimeError("Shrug audio (assets/shrug.mp3) is missing on the server")
    return image_audio_to_video(still, "shrug.jpg", audio, duration=_SHRUG_DURATION)


def _char_alpha_canvas(char_path: str, long_edge: int = 720):
    """A transparent RGBA canvas sized to the character's OWN silhouette (cropped to its opaque bbox),
    scaled so its long edge is `long_edge`. Returns the character alone on transparency — the caller
    encodes it as an alpha layer the Meme Builder composites over whatever is beneath. Sizing the canvas
    to the figure (rather than padding it into a fixed 9:16 frame) means the exported layer has no dead
    transparent margin, so it scales/positions cleanly on the timeline."""
    from PIL import Image as _Img
    char = _character_still(char_path)
    try:
        bb = char.getbbox()
        if bb:
            char = char.crop(bb)     # drop the empty canvas padding the assets ship with
    except Exception:
        pass
    w, h = char.size
    if max(w, h) > long_edge:
        r = long_edge / float(max(w, h))
        char = char.resize((max(2, int(w * r)), max(2, int(h * r))), _Img.LANCZOS)
    return char


def render_shrug_alpha(dur: float = None) -> bytes:
    """The shrug pose (shrug.png) on a transparent canvas → alpha .mov with the shrug audio muxed in.
    The Meme Builder LAYER variant of `shrug`: unlike add_shrug_video it has NO background image and NO
    caption/speech bubble (there is nothing beneath it to caption yet — the user composites it over
    their own footage) and NO branded outro. Carries its sound so the layer plays the shrug clip.
    """
    from app.services.media_service import still_to_alpha_video
    cp = _character_path("shrug")
    if not cp:
        raise RuntimeError("shrug character art (assets/characters/shrug.png) is missing on the server")
    still = _char_alpha_canvas(cp)
    d = float(dur) if dur else _SHRUG_DURATION
    # Silent — the shrug sound rides the meme layer's `sound` field (client sets it to "shrug").
    return still_to_alpha_video(still, dur=d)


def render_character_alpha(name: str, dur: float = 6.0) -> bytes:
    """A named character (carl/soyjack/would/…) on a transparent canvas → alpha .mov, no audio, no outro.
    The Meme Builder LAYER variant of `char <name>`: the character art alone, composited over whatever
    is beneath it on the timeline. A .mov character would contribute its FIRST frame only (same as
    _composite_char_on_image); every character shipped today is a still PNG.
    """
    from app.services.media_service import still_to_alpha_video
    cp = _character_path(name)
    if not cp:
        raise RuntimeError(f"unknown character '{name}'")
    still = _char_alpha_canvas(cp)
    return still_to_alpha_video(still, dur=float(dur or 6.0))


# The two-panel beat, shared by the catalogue so the Meme Builder gives the layer a timeline slot the
# clip actually fills (1.6s away + 1.9s looking — see add_lookingaway_video; the punchline holds longer).
LOOKINGAWAY_ALPHA_DUR = 3.5


def render_lookingaway_alpha(dur: float = None) -> bytes:
    """The two-panel "looking away" meme as a transparent LAYER: the puppet looks AWAY, then turns to
    the camera. The Meme Builder variant of add_lookingaway_video — same two shots and the same beats,
    but on transparency with no source image, so it composites over whatever is on the timeline.

    `anyways` used to come through here as render_character_alpha, which resolves the name to the
    ORIGINAL one-panel anyways.png — so the layer was a single still of the puppet. The turn IS the
    joke, so that could only ever be half the meme (the command path got the two panels in 404aeb52;
    this path never did).

    Both panels are cropped against a SHARED bbox and scaled by ONE ratio. Per-panel cropping moves the
    figure between the frames — the shipped art's bboxes already differ by a pixel (y0 1 vs 0) — and on
    a hard cut a shifted figure reads as a twitch instead of a turn.
    """
    from PIL import Image as _Img
    from app.services.media_service import frames_to_alpha_video
    away, look = _lookingaway_panels()
    if not (away and look):
        # Degrade to the original single still rather than failing the pick — the same policy the
        # command path takes when the two-panel art isn't installed.
        return render_character_alpha("anyways", dur=dur or 6.0)
    panels = [_character_still(away), _character_still(look)]
    boxes = [p.getbbox() for p in panels]
    if all(boxes):
        shared = (min(b[0] for b in boxes), min(b[1] for b in boxes),
                  max(b[2] for b in boxes), max(b[3] for b in boxes))
        panels = [p.crop(shared) for p in panels]
    w, h = panels[0].size
    if max(w, h) > 720:
        r = 720 / float(max(w, h))
        size = (max(2, int(w * r)), max(2, int(h * r)))
        panels = [p.resize(size, _Img.LANCZOS) for p in panels]
    total = max(1.0, min(float(dur or LOOKINGAWAY_ALPHA_DUR), 30.0))
    # Hold each panel as a run of identical frames: it's a CUT, not an animation, so the rate only has
    # to be fine enough to land the cut where the beats say (1/8s) and to scrub smoothly.
    fps = 8
    n_away = max(1, int(round(total * (1.6 / LOOKINGAWAY_ALPHA_DUR) * fps)))
    n_look = max(1, int(round(total * (1.9 / LOOKINGAWAY_ALPHA_DUR) * fps)))
    return frames_to_alpha_video([panels[0]] * n_away + [panels[1]] * n_look, fps=fps)


def draw_dialogue_caption(base, text: str, char_top: int, char_left: int, char_right: int,
                          band_cap: int = 0, mouth_y: int = None, row_edges=None,
                          prefer_side: str = ""):
    """Draw `text` on `base` (RGBA, modified in place) as the character's DIALOGUE: a rounded speech
    bubble in the wider gutter beside them with the tail on them, or — when no gutter can hold
    readable text — a plain white meme banner above their head.

    Shared by every character effect (`shrug`/`would`/`theraped`/`consider`) so the dialogue style
    can never drift between them. `char_top`/`char_left`/`char_right` are the character's bounds.
    `band_cap` (px, 0 = off) narrows the gutter the bubble may use — a character composited LARGE
    leaves a gutter far wider than they are, and filling it makes the bubble dwarf them; capping it
    at their own width keeps the compact block look. `mouth_y` is the character's mouth line: the
    bubble is centred on it so the words come out of their MOUTH. Returns (beside, caption_bottom);
    caption_bottom is where the pointing arrow may start.
    """
    from PIL import ImageDraw as _Draw
    from ._common import _load_meme_font, _wrap_text_to_width

    text = (text or "").strip().upper()
    W, H = base.size
    draw = _Draw.Draw(base)
    margin = max(int(W * 0.03), 6)

    # Caption goes in the WIDER gutter beside her. Ties go right (she reads as pointing up-left of it
    # in the stock pose). If neither gutter can hold readable text — a very wide character, a narrow
    # image — fall back to the band above her head rather than cramming it into a 3-character column.
    left_w, right_w = char_left - 2 * margin, W - char_right - 2 * margin
    side = "right" if right_w >= left_w else "left"
    # A character can ask for a side (see _BUBBLE_SIDE): which way they FACE decides where their
    # speech belongs, and that is a property of the art, not of which gutter happens to be wider.
    # Honoured only if that gutter can hold readable text — otherwise the wider one still wins.
    if prefer_side in ("left", "right"):
        if (left_w if prefer_side == "left" else right_w) >= max(int(W * 0.18), 60):
            side = prefer_side
    # Size against the gutter actually being used, not the widest one — they differ once a side is
    # forced, and sizing against the other gutter would overflow the one the bubble sits in.
    band_w = left_w if side == "left" else right_w
    beside = band_w >= max(int(W * 0.18), 60)
    if band_cap > 0:
        band_w = min(band_w, band_cap)

    def _layout(in_gutter: bool):
        """(max_width, top_size, band_h) for the beside-her or above-her-head placement."""
        # Use only part of the gutter: at full width the bubble filled it end to end and clamped
        # against the frame margin, which reads as "pinned to the right" rather than sitting beside
        # him. The tail and the bubble padding also live outside `tw`, so budgeting for them here is
        # what keeps the tail on him.
        mw = int(band_w * 0.74) if in_gutter else (W - 2 * margin)
        # "Smaller" applies to BOTH placements: this is a label on her, not a meme banner. The
        # above-head fallback used H/6 and rendered enormous on portrait images.
        ts = max(int(H / (11 if in_gutter else 10)), 13)
        bh = (H - char_top) if in_gutter else min(max(int(H * 0.10), char_top - margin), int(H * 0.30))
        return mw, ts, bh

    max_width, top_size, band_h = _layout(beside)

    # A caption must never be broken MID-WORD. _wrap_text_to_width hard-breaks a word that cannot fit,
    # so in a narrow gutter "WOULD" came out as "WOUL"/"D" — and the size loop accepted it, because the
    # broken result does technically fit. Require every WORD to fit on its own line and keep shrinking
    # until one does; wrapping between words is still fine for longer captions.
    words = text.split() or [text]

    def _fit(one_line: bool):
        """Largest size whose caption fits the box. one_line=True refuses to wrap at all."""
        for size in range(top_size, 10, -1):
            font = _load_meme_font(size)
            if max(draw.textlength(w, font=font) for w in words) > max_width:
                continue                               # a word would be chopped mid-word — go smaller
            lines = [text] if one_line else _wrap_text_to_width(draw, text, font, max_width)
            stroke = max(1, size // 14)
            bbox = draw.multiline_textbbox((0, 0), "\n".join(lines), font=font,
                                           stroke_width=stroke, align="center")
            if (bbox[2] - bbox[0]) <= max_width and (bbox[3] - bbox[1]) <= band_h:
                return (font, lines, stroke, bbox)
        return None

    # ONE LINE is the meme's look — "WOULD" read right while "THE RAPED" wrapped to two lines and the
    # two effects stopped matching. Prefer a single line even though it means a smaller font; only wrap
    # when the caption cannot fit on one line at any readable size (a long custom caption).
    chosen = _fit(True) or _fit(False)
    # A gutter can be wide enough to pass the `beside` test and still only hold unreadable text —
    # on a 1080x1920 phone photo the shrugging rabbi leaves ~217px of gutter, which fits "WHADDYA"
    # at about 2% of the image height. When that happens use the band ABOVE his head instead: the
    # pointing format leaves most of a portrait frame empty up there, so the caption can be several
    # times bigger, which matters more than sitting beside him.
    #
    # "Readable" is measured against the SHORT side: on a tall portrait frame H/24 demanded a font
    # bigger than any gutter could hold, so a perfectly readable bubble was thrown away for the
    # banner. The rabbi case above still falls back (38px in a 1080-wide frame is under 45).
    floor = min(W, H) / 24
    if beside and chosen and chosen[0].size < floor:
        # A multi-word caption ("WHADDYA GONNA DO?") is what usually shrinks: one line of it is
        # long, so the size loop keeps going down until the whole thing fits the gutter. Two lines
        # in the gutter at a readable size beat one tiny line, so try wrapping BEFORE giving up the
        # bubble — the speech bubble is the look, and only a genuinely narrow gutter should lose it.
        wrapped = _fit(False)
        if wrapped and wrapped[0].size >= floor:
            chosen = wrapped
        else:
            max_width, top_size, band_h = _layout(False)
            alt = _fit(True) or _fit(False)
            if alt and alt[0].size > chosen[0].size:
                beside, chosen = False, alt
            else:
                max_width, top_size, band_h = _layout(True)
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
        # Level with the MOUTH — that is what makes it read as speech. `mouth_y` is detected from the
        # art (see mouth_frac); the 38% fallback is for art with no findable face and is only ever
        # approximately the head: char_top is the top of the RAISED ARM on a pointing pose, so a
        # fraction of the figure lands wherever the pose happens to put the chest.
        # Solved BEFORE x, because how close the bubble can sit depends on how wide the figure is at
        # these particular rows.
        char_h = H - char_top
        anchor = mouth_y if mouth_y is not None else (char_top + int(char_h * 0.38))
        y = max(margin, min(anchor - th // 2, H - th - margin))
        # Hug the SILHOUETTE over the bubble's own rows, not the bounding box. char_left/char_right are
        # the figure's widest point, which is nearly always its bottom (shoulders, a flared coat), while
        # the bubble sits up at the mouth — so the box edge left a visible empty channel between a
        # character and their own speech. Taking the extent of exactly the rows the bubble covers cannot
        # overlap the art, and falls back to the box when the silhouette could not be measured.
        l_edge, r_edge = char_left, char_right
        if row_edges is not None:
            try:
                band = row_edges[max(0, y):min(H, y + th)]
                band = band[band[:, 1] >= 0]
                if len(band):
                    l_edge, r_edge = int(band[:, 0].min()), int(band[:, 1].max())
            except Exception:
                pass
        cx = (l_edge - gap - tw // 2) if side == "left" else (r_edge + gap + tw // 2)
        cx = max(margin + tw // 2, min(cx, W - margin - tw // 2))
    else:
        cx, y = W // 2, max(margin, min(char_top - th - margin, H - th - margin))
    if beside:
        # Dialogue: bubble first, then dark ink on it (a white stroke-outlined caption would vanish).
        #
        # Size the bubble from where the words ACTUALLY land, not from `tw`/`th`. Those come from a
        # bbox measured at the origin with the default anchor, but the caption is drawn with
        # anchor="ma" — the two differ by the ascender-to-cap-top gap, which grows with the font
        # size, so the bubble sat too high and the text rested on (or crossed) its bottom border.
        # Measuring at the real draw position with the real anchor pads the caption evenly instead.
        _txt = "\n".join(lines)
        _ink = draw.multiline_textbbox((cx, y), _txt, font=font, anchor="ma", align="center")
        _draw_speech_bubble(draw, (_ink[0] + _ink[2]) // 2, _ink[1], _ink[2] - _ink[0], _ink[3] - _ink[1],
                            toward_left=(side == "right"), scale=max(6, font.size // 3))
        draw.multiline_text((cx, y), _txt, font=font, fill=(20, 18, 26),
                            anchor="ma", align="center")
    else:
        draw.multiline_text((cx, y), "\n".join(lines), font=font, fill=(255, 255, 255),
                            stroke_width=stroke, stroke_fill=(0, 0, 0), anchor="ma", align="center")

    return beside, (margin if beside else y + th)


# Which side a character's speech belongs on, when it isn't just "whichever gutter is wider".
_BUBBLE_SIDE = {
    # Steve is turned toward frame-left, so his words read as his coming out that way; on the right
    # they sat behind his head. The tail flips on its own (`toward_left=(side == "right")`).
    "nodontthinkiwill": "left",
}


def _add_pointing_meme(data: bytes, char_key: str, caption: str, fallback: str = "") -> bytes:
    """The pointing-up meme format: the character stands bottom-centre pointing at the image above,
    with the caption BESIDE them (whichever side has more room) in a speech bubble, so the character
    stays the focal point and the text reads as dialogue rather than a meme banner.

    Reuses the proven meme font/wrap/stroke helpers rather than reimplementing text layout, and takes
    the art from the _CHARACTERS registry — drop a pointing pose at assets/characters/<key>.png and it
    is used automatically.
    """
    from PIL import Image as _Img, ImageOps as _Ops
    from io import BytesIO as _BIO

    cp = _character_path(char_key) or (_character_path(fallback) if fallback else "")
    if not cp:
        raise ValueError(f"{char_key}: character art (assets/characters/{char_key}.png) is missing on the server")

    with _Img.open(_BIO(data)) as im:
        im = _Ops.exif_transpose(im)
        base = im.convert("RGBA")

    base, char_top, char_left, char_right, mouth_y, edges = _composite_char_bottom_center(base, cp)
    draw_dialogue_caption(base, caption or char_key, char_top, char_left, char_right,
                          mouth_y=mouth_y, row_edges=edges,
                          prefer_side=_BUBBLE_SIDE.get(char_key, ""))

    buf = _BIO()
    base.convert("RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue()


# Reaction overlays — the same bottom-CENTRE anchor as the pointing memes, but with NO caption, no
# speech bubble and no drawn arrow: these poses already say it. Each entry is how big the figure
# should be, because "40% of the height" means something different for a wide pair of soyjaks than
# for one tall puppet: (height_frac, max_width_frac).
_REACTION_SIZES = {
    "carl":    (0.46, 0.62),   # landscape, points off to his left — needs room sideways
    "soyjack": (0.44, 0.98),   # a WIDE pair that must span the frame, or they point at nothing
    "anyways": (0.52, 0.42),   # tall and narrow; the side-eye reads at half the frame height
    # Jerry is chest-up and roughly square (882x674), mic out to one side — a hair over half the
    # frame height reads without his outstretched hand running off a portrait photo.
    "jerry":   (0.55, 0.70),
}


def _add_reaction_overlay(data: bytes, char_key: str) -> bytes:
    """Composite a background-less character over the image, bottom-centre.

    The difference from _add_pointing_meme is everything it DOESN'T do — no caption, no bubble, no
    arrow — so the result is just the picture with someone reacting to it at the bottom of the frame.
    """
    from PIL import Image as _Img, ImageOps as _Ops
    from io import BytesIO as _BIO

    cp = _character_path(char_key)
    if not cp:
        raise ValueError(f"{char_key}: character art (assets/characters/{char_key}.png) is missing on the server")
    hf, wf = _REACTION_SIZES.get(char_key, (0.46, 0.6))

    with _Img.open(_BIO(data)) as im:
        im = _Ops.exif_transpose(im)
        base = im.convert("RGBA")

    base, _top, _l, _r, _mouth, _edges = _composite_char_bottom_center(base, cp, height_frac=hf,
                                                               max_width_frac=wf, want_mouth=False)
    buf = _BIO()
    base.convert("RGB").save(buf, "JPEG", quality=90)
    return buf.getvalue()


def add_carl(data: bytes) -> bytes:
    """`carl` — Carl Brutananadilewski points at whatever you attached."""
    return _add_reaction_overlay(data, "carl")


def add_soyjack(data: bytes) -> bytes:
    """`soyjack` — the two soyjaks, pointing and yelling at your image."""
    return _add_reaction_overlay(data, "soyjack")


def add_anyways(data: bytes) -> bytes:
    """`anyways` — the puppet side-eyes your image and moves on."""
    return _add_reaction_overlay(data, "anyways")


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


def nodontthinkiwill_attachments(attachments):
    return _pointing_attachments(attachments, "nodontthinkiwill", "No, I Don't Think I Will",
                                 add_nodontthinkiwill)


def nothingeverhappens_attachments(attachments):
    return _pointing_attachments(attachments, "nothingeverhappens", "Nothing Ever Happens",
                                 add_nothingeverhappens)


def would_attachments(attachments):
    return _pointing_attachments(attachments, "would", "Would", add_would)


def carl_attachments(attachments):
    return _pointing_attachments(attachments, "carl", "Carl", add_carl)


def soyjack_attachments(attachments):
    """Soyjack now carries audio, so its output is video/mp4 rather than a still — same shape as
    shrug/diarrhea. Falls back to the silent still if the sound asset is missing, so a server
    without assets/soyjack.mp3 degrades instead of erroring."""
    from ._common import _human_size, is_image
    if not _soyjack_audio_path():
        return _pointing_attachments(attachments, "soyjack", "Soyjak", add_soyjack)
    images = [(f, d, ct) for f, d, ct in (attachments or []) if is_image(f, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_soyjack_video(data, source_filename=filename)
        out: OutputFile = {"filename": f"{stem}_soyjack.mp4", "data": result,
                           "content_type": "video/mp4"}
        return [out], f"## 😮 Soyjaks pointing\n\n😮 {filename}: {_human_size(len(result))}"
    except Exception as e:
        logger.error(f"soyjack failed for {filename}: {e}", exc_info=True)
        return [], f"Could not apply soyjack to {filename}: {e}"


def _lookingaway_panels():
    """(away, look) panel paths, or ("","") when the two-panel art isn't installed."""
    out = []
    for suffix in ("a", "b"):
        p = ""
        for base in _CHARS_DIR_CANDIDATES:
            cand = os.path.join(base, f"lookingaway_{suffix}.png")
            if os.path.exists(cand):
                p = cand
                break
        out.append(p)
    return (out[0], out[1]) if all(out) else ("", "")


def add_lookingaway_video(data: bytes) -> bytes:
    """The two-panel "looking away" meme as a 2-shot MP4: the puppet looks AWAY, then turns to the
    camera. That turn IS the joke, so a single still can only ever be half the meme.

    Both shots composite onto the SAME source image, so only the puppet's eyes change between them.
    """
    import subprocess, tempfile
    from app.services.media_service import resolve_ffmpeg
    away, look = _lookingaway_panels()
    if not (away and look):
        raise RuntimeError("looking-away art (assets/characters/lookingaway_[ab].png) is missing")
    shots = [(_composite_char_on_image(data, away), 1.6),    # beat on the turn: the second shot
             (_composite_char_on_image(data, look), 1.9)]    # holds longer than the first
    with tempfile.TemporaryDirectory() as tmp:
        listing = []
        for i, (jpg, dur) in enumerate(shots):
            fp = os.path.join(tmp, f"shot{i}.jpg")
            with open(fp, "wb") as fh:
                fh.write(jpg)
            listing.append(f"file '{fp}'\nduration {dur}")
        # Repeat the last file with a TINY duration. The usual idiom repeats it with none at all, but
        # ffmpeg then holds it for the previous entry's duration — which silently doubled the final
        # shot (measured 5.46s for a 1.6+1.9 pair) and left the meme sitting on its punchline.
        listing.append(f"file '{os.path.join(tmp, f'shot{len(shots)-1}.jpg')}'\nduration 0.04")
        lp = os.path.join(tmp, "list.txt")
        with open(lp, "w") as fh:
            fh.write("\n".join(listing) + "\n")
        out = os.path.join(tmp, "out.mp4")
        p = subprocess.run([resolve_ffmpeg(), "-y", "-hide_banner", "-loglevel", "error",
                            "-f", "concat", "-safe", "0", "-i", lp,
                            "-vf", "scale=trunc(iw/2)*2:trunc(ih/2)*2,fps=24",
                            "-c:v", "libx264", "-pix_fmt", "yuv420p", out],
                           capture_output=True, timeout=180)
        if p.returncode != 0 or not os.path.exists(out):
            raise RuntimeError(f"ffmpeg failed: {p.stderr[-200:]}")
        with open(out, "rb") as fh:
            return fh.read()


def anyways_attachments(attachments):
    """Two-panel when the art is installed (video/mp4), else the original single still."""
    from ._common import _human_size, is_image
    away, look = _lookingaway_panels()
    if not (away and look):
        return _pointing_attachments(attachments, "anyways", "Anyways", add_anyways)
    images = [(f, d, ct) for f, d, ct in (attachments or []) if is_image(f, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_lookingaway_video(data)
        out: OutputFile = {"filename": f"{stem}_lookingaway.mp4", "data": result,
                           "content_type": "video/mp4"}
        return [out], f"## 🙄 Looking away\n\n🙄 {filename}: {_human_size(len(result))}"
    except Exception as e:
        logger.error(f"lookingaway failed for {filename}: {e}", exc_info=True)
        return _pointing_attachments(attachments, "anyways", "Anyways", add_anyways)


lookingaway_attachments = anyways_attachments   # the meme's real name; `anyways` stays an alias


def shrug_attachments(attachments):
    """Render the shrug meme on the first image and set it to the shrug audio clip → MP4.
    Unlike theraped/would (still images), shrug carries audio, so its output is video/mp4 —
    mirrors diarrhea_attachments (whoabuddy-style video output)."""
    from ._common import _human_size, is_image
    images = [(f, d, ct) for f, d, ct in (attachments or []) if is_image(f, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_shrug_video(data, source_filename=filename)
        out: OutputFile = {
            "filename": f"{stem}_shrug.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🤷 Whaddya gonna do?\n\n🤷 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"shrug failed for {filename}: {e}", exc_info=True)
        return [], f"Could not apply shrug to {filename}: {e}"
