"""Auto-split from the original effects_service.py monolith. No behavior change."""
from ._common import List, OutputFile, Path, Tuple, _ANIME_CASCADE_CANDIDATES, _THUG_AUDIO_CANDIDATES, _THUG_DURATION, _alive_or_still, _human_size, io, is_image, logger, os, re
from .text import add_meme_text
from .stamps import add_kosher

_INSIGHTFACE_APP = None
_INSIGHTFACE_TRIED = False


def _thug_audio_path() -> str:
    """First existing thug mp3 from the candidate list ("" if none)."""
    for p in _THUG_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def _make_deal_with_it_glasses(target_w: int, target_h: int):
    """Pixel-art 'deal with it' black sunglasses, sized to a face's eye band.
    Drawn small then scaled NEAREST for the 8-bit look. Returns RGBA Image."""
    from PIL import Image, ImageDraw
    cw, ch = 80, 30
    g = Image.new("RGBA", (cw, ch), (0, 0, 0, 0))
    d = ImageDraw.Draw(g)
    black = (12, 12, 12, 255)
    d.rectangle([0, 4, cw, 9], fill=black)          # temple bar across the top
    d.rectangle([6, 8, 34, 26], fill=black)         # left lens
    d.rectangle([46, 8, 74, 26], fill=black)        # right lens
    d.rectangle([34, 11, 46, 15], fill=black)       # bridge
    return g.resize((max(2, target_w), max(2, target_h)), Image.NEAREST)


def _anime_cascade_path() -> str:
    """First existing anime-face cascade from the candidate list ("" if none)."""
    for p in _ANIME_CASCADE_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def _detect_thug_faces(gray, anime_gray, im_w: int, im_h: int):
    """Run the real-face + anime-face cascades and return de-duplicated boxes as
    (x, y, w, h, kind) where kind is 'real' or 'anime' (their feature geometry
    differs — anime mouths sit much higher in the box). Anime detection uses a
    histogram-equalized gray (nagadomi's recommendation)."""
    import cv2
    # A meme subject's face is large; a higher floor + neighbour count keeps the
    # anime cascade from stamping spurious tiny "faces" on hands/background.
    min_side = (max(40, im_w // 12), max(40, im_h // 12))
    boxes = []
    real = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_frontalface_default.xml")
    if not real.empty():
        boxes += [(b, "real") for b in real.detectMultiScale(gray, scaleFactor=1.1, minNeighbors=5, minSize=min_side)]
    anime_xml = _anime_cascade_path()
    if anime_xml:
        anime = cv2.CascadeClassifier(anime_xml)
        if not anime.empty():
            # minNeighbors=3 is nagadomi's default; 5 missed many real faces. The
            # min_side floor (above) is what suppresses spurious tiny detections.
            boxes += [(b, "anime") for b in anime.detectMultiScale(anime_gray, scaleFactor=1.1, minNeighbors=3, minSize=min_side)]
    # Drop boxes whose centre falls inside an already-accepted box (same face hit
    # by both cascades). Prefer a 'real' hit over an 'anime' one on the same face
    # (real geometry is correct for photos; true anime never trips the real
    # cascade), then larger boxes first.
    accepted = []
    for (x, y, w, h), kind in sorted(boxes, key=lambda bk: (bk[1] != "real", -(bk[0][2] * bk[0][3]))):
        cx, cy = x + w / 2, y + h / 2
        if any(ax <= cx <= ax + aw and ay <= cy <= ay + ah for (ax, ay, aw, ah, _) in accepted):
            continue
        accepted.append((x, y, w, h, kind))
    return accepted


def _draw_joint(overlay_draw, x0: float, y0: float, length: float, th: float, angle_deg: float = 18.0):
    """Draw a lit joint on an RGBA overlay: filter end anchored at (x0, y0)
    (the mouth), drooping down-right, with a glowing ember and smoke."""
    import math
    a = math.radians(angle_deg)
    dx, dy = math.cos(a), math.sin(a)
    x1, y1 = x0 + length * dx, y0 + length * dy            # ember (far) end
    fx, fy = x0 + length * 0.18 * dx, y0 + length * 0.18 * dy
    overlay_draw.line([(x0, y0), (x1, y1)], fill=(245, 240, 230, 255), width=int(th))   # paper
    overlay_draw.line([(x0, y0), (fx, fy)], fill=(120, 80, 40, 255), width=int(th))     # filter (mouth end)
    r = th * 0.7
    overlay_draw.ellipse([x1 - r, y1 - r, x1 + r, y1 + r], fill=(255, 90, 20, 255))     # ember glow
    overlay_draw.ellipse([x1 - r * 0.6, y1 - r * 0.6, x1 + r * 0.6, y1 + r * 0.6], fill=(255, 190, 60, 255))
    for i, (rr, al) in enumerate([(th * 0.6, 150), (th * 0.9, 110), (th * 1.2, 70)]):   # smoke rising
        cx, cy = x1, y1 - (i + 1) * th * 1.2
        overlay_draw.ellipse([cx - rr, cy - rr, cx + rr, cy + rr], fill=(225, 225, 225, al))


def _insightface_app():
    """Lazily build a detection-only InsightFace app (SCRFD gives 5 landmarks:
    eyes, nose, mouth corners). Cached; returns None if unavailable."""
    global _INSIGHTFACE_APP, _INSIGHTFACE_TRIED
    if _INSIGHTFACE_TRIED:
        return _INSIGHTFACE_APP
    _INSIGHTFACE_TRIED = True
    try:
        import warnings
        warnings.filterwarnings("ignore")
        from insightface.app import FaceAnalysis
        app = FaceAnalysis(name="buffalo_l", allowed_modules=["detection"],
                           providers=["CPUExecutionProvider"])
        app.prepare(ctx_id=-1, det_size=(640, 640))
        _INSIGHTFACE_APP = app
    except Exception as e:
        logger.warning(f"insightface unavailable for thug overlay: {e}")
        _INSIGHTFACE_APP = None
    return _INSIGHTFACE_APP


def _draw_thug_from_landmarks(im, kps, ImageDraw):
    """Stamp glasses across the two eye landmarks (rotation-aware) and a joint at
    the mouth centre (between the mouth-corner landmarks). `kps` is SCRFD's 5×2:
    [left eye, right eye, nose, left mouth, right mouth]."""
    import math
    from PIL import Image
    le, re, lm, rm = kps[0], kps[1], kps[3], kps[4]
    ipd = math.hypot(re[0] - le[0], re[1] - le[1]) or 1.0
    midx, midy = (le[0] + re[0]) / 2, (le[1] + re[1]) / 2
    ang = math.degrees(math.atan2(re[1] - le[1], re[0] - le[0]))
    gw, gh = max(8, int(ipd * 2.1)), max(8, int(ipd * 0.62))
    glasses = _make_deal_with_it_glasses(gw, gh).rotate(-ang, expand=True, resample=Image.BICUBIC)
    im.paste(glasses, (int(midx - glasses.width / 2), int(midy - glasses.height / 2)), glasses)
    mx, my = (lm[0] + rm[0]) / 2, (lm[1] + rm[1]) / 2
    overlay = Image.new("RGBA", im.size, (0, 0, 0, 0))
    _draw_joint(ImageDraw.Draw(overlay), mx - 0.15 * ipd, my, ipd * 1.7, max(5, int(ipd * 0.17)))
    return Image.alpha_composite(im.convert("RGBA"), overlay).convert("RGB")


def _apply_thug_face(image_data: bytes) -> bytes:
    """Detect a face and stamp pixel sunglasses over the eyes + a lit joint at the
    mouth (the classic THUG LIFE overlay). Tries InsightFace landmarks first
    (exact, handles ¾ views), then falls back to the haar/anime cascades for flat
    2D art. Returns JPEG bytes; on no face or any error returns the original."""
    try:
        import cv2
        import numpy as np
        from PIL import Image, ImageOps, ImageDraw
    except Exception as e:
        logger.warning(f"thug face overlay unavailable ({e}); skipping")
        return image_data

    # Preferred path: InsightFace landmarks (precise eyes + mouth).
    app = _insightface_app()
    if app is not None:
        try:
            with Image.open(io.BytesIO(image_data)) as im0:
                im = ImageOps.exif_transpose(im0).convert("RGB")
            bgr = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
            dets = app.get(bgr)
            if dets:
                f = max(dets, key=lambda d: (d.bbox[2] - d.bbox[0]) * (d.bbox[3] - d.bbox[1]))
                if getattr(f, "kps", None) is not None and len(f.kps) >= 5:
                    out_img = _draw_thug_from_landmarks(im, f.kps, ImageDraw)
                    out = io.BytesIO()
                    out_img.save(out, format="JPEG", quality=92)
                    return out.getvalue()
        except Exception as e:
            logger.warning(f"insightface thug path failed, falling back: {e}")
    try:
        with Image.open(io.BytesIO(image_data)) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            gray = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2GRAY)
            faces = _detect_thug_faces(gray, cv2.equalizeHist(gray), im.width, im.height)
            if not faces:
                return image_data
            # Thug memes target one subject; keep only the largest face so stray
            # cascade hits on hands/collars/background don't get stamped too.
            faces = [max(faces, key=lambda f: f[2] * f[3])]
            eye_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_eye.xml")
            # Joint + smoke go on an RGBA overlay so the smoke can be translucent.
            overlay = Image.new("RGBA", im.size, (0, 0, 0, 0))
            od = ImageDraw.Draw(overlay)
            for (x, y, w, h, kind) in faces:
                # Anchor on the actual eye line — far more robust across art styles
                # than a fixed fraction (boxes vary in how much hat/forehead/neck
                # they include). Fall back to a fraction if no eyes are found.
                eye_cy = None
                if not eye_cascade.empty():
                    eyes = eye_cascade.detectMultiScale(
                        gray[y:y + h, x:x + w], scaleFactor=1.05, minNeighbors=3,
                        minSize=(max(8, int(w * 0.10)), max(6, int(h * 0.06))),
                    )
                    eyes = [e for e in eyes if (e[1] + e[3] / 2) < 0.6 * h]  # ignore nostril/mouth hits
                    if eyes:
                        eye_cy = y + sum(e[1] + e[3] / 2 for e in eyes) / len(eyes)
                if eye_cy is None:
                    eye_cy = y + (0.42 if kind == "anime" else 0.40) * h
                # Sunglasses centred on the eye line.
                gw, gh = int(w * 1.04), max(8, int(h * 0.26))
                glasses = _make_deal_with_it_glasses(gw, gh)
                im.paste(glasses, (x + (w - gw) // 2, int(eye_cy - gh / 2)), glasses)
                # Joint at the mouth, a kind-specific drop below the eyes (anime
                # faces have a much shorter eyes→mouth distance than real ones).
                mouth_y = eye_cy + (0.21 if kind == "anime" else 0.38) * h
                _draw_joint(od, x + 0.40 * w, mouth_y, w * 0.72, max(5, int(h * 0.045)))
            im = Image.alpha_composite(im.convert("RGBA"), overlay).convert("RGB")
            out = io.BytesIO()
            im.save(out, format="JPEG", quality=92)
            return out.getvalue()
    except Exception as e:
        logger.warning(f"thug face overlay failed, using bare image: {e}")
        return image_data


def add_thug(image_data: bytes, source_filename: str = "image.jpg") -> bytes:
    """Stamp the THUG LIFE meme (pixel sunglasses + joint on the detected face),
    burn the "THUG LIFE" caption over the lower third, then turn it into an MP4
    with the THUG LIFE clip over it. MP4 bytes. Each step falls back to the prior
    image so a missing face or font never aborts it."""
    from app.services.media_service import image_audio_to_video
    audio = _thug_audio_path()
    if not audio:
        raise RuntimeError("Thug audio (assets/thug.mp3) is missing on the server")
    faced = _apply_thug_face(image_data)
    try:
        faced = add_meme_text(faced, "THUG LIFE")
    except Exception as e:
        logger.warning(f"thug caption failed, using bare image: {e}")
    return image_audio_to_video(faced, "image.jpg", audio, duration=_THUG_DURATION)


def thug_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Turn the first image attachment into a THUG LIFE MP4 (bakes its own
    "THUG LIFE" caption — it does not take a custom `meme` caption)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_thug(data, filename)
        out: OutputFile = {
            "filename": f"{stem}_thug.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 😎 Thug\n\n😎 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"thug failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"


def _locate_mouth(image_data: bytes):
    """Return ``(cx, cy, mouth_w)`` for the largest face's mouth, or ``None``.
    InsightFace gives the exact mouth corners; the haar(real)+lbp(anime) cascade
    fallback estimates the mouth from the face box + eye line (anime mouths sit
    much higher), mirroring the THUG overlay so it works on photos and anime."""
    try:
        import cv2
        import numpy as np
        import math
        from PIL import Image, ImageOps
    except Exception as e:
        logger.warning(f"blue: cv2/PIL unavailable ({e}); skipping")
        return None

    # Preferred: InsightFace 5-pt landmarks (le, re, nose, left-mouth, right-mouth).
    app = _insightface_app()
    if app is not None:
        try:
            with Image.open(io.BytesIO(image_data)) as im0:
                im = ImageOps.exif_transpose(im0).convert("RGB")
            bgr = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
            dets = app.get(bgr)
            if dets:
                f = max(dets, key=lambda d: (d.bbox[2] - d.bbox[0]) * (d.bbox[3] - d.bbox[1]))
                if getattr(f, "kps", None) is not None and len(f.kps) >= 5:
                    lm, rm = f.kps[3], f.kps[4]
                    cx, cy = (lm[0] + rm[0]) / 2, (lm[1] + rm[1]) / 2
                    mw = max(8.0, math.hypot(rm[0] - lm[0], rm[1] - lm[1]))
                    return (cx, cy, mw)
        except Exception as e:
            logger.warning(f"blue: insightface path failed, falling back: {e}")

    # Fallback: cascade face box + eye line (same anchoring as the thug overlay).
    try:
        with Image.open(io.BytesIO(image_data)) as im:
            im = ImageOps.exif_transpose(im).convert("RGB")
            gray = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2GRAY)
            faces = _detect_thug_faces(gray, cv2.equalizeHist(gray), im.width, im.height)
            if not faces:
                return None
            x, y, w, h, kind = max(faces, key=lambda f: f[2] * f[3])
            eye_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + "haarcascade_eye.xml")
            eye_cy = None
            if not eye_cascade.empty():
                eyes = eye_cascade.detectMultiScale(
                    gray[y:y + h, x:x + w], scaleFactor=1.05, minNeighbors=3,
                    minSize=(max(8, int(w * 0.10)), max(6, int(h * 0.06))),
                )
                eyes = [e for e in eyes if (e[1] + e[3] / 2) < 0.6 * h]  # drop nostril/mouth hits
                if len(eyes):
                    eye_cy = y + sum(e[1] + e[3] / 2 for e in eyes) / len(eyes)
            if eye_cy is None:
                eye_cy = y + (0.42 if kind == "anime" else 0.40) * h
            mouth_y = eye_cy + (0.21 if kind == "anime" else 0.38) * h
            return (x + 0.5 * w, mouth_y, max(8.0, w * 0.42))
    except Exception as e:
        logger.warning(f"blue: cascade path failed: {e}")
    return None


def _draw_blue_paint(im, cx, cy, mw):
    """Smear wet blue paint AROUND the mouth at ``(cx, cy)`` (sized to mouth width
    ``mw``) with drips running down — then punch the mouth opening clear so the lips
    and teeth stay visible. Draws on an RGBA overlay, then composites. Returns RGB."""
    import random
    from PIL import Image, ImageDraw, ImageFilter
    rng = random.Random(int(cx * 131 + cy * 17 + mw))   # stable per-face, varied look
    W, H = im.size
    BLUE = (24, 92, 226, 235)
    DARK = (12, 48, 150, 235)
    # Mouth opening to KEEP visible (lips + teeth).
    mhw = mw * 0.55
    mhh = mw * 0.26
    # Paint stays ON the lips: a tight horizontal band along the lip line, no taller
    # than the mouth and biased slightly downward — so it never climbs onto the nose.
    band_hw = mw * 0.72
    band_hh = mw * 0.30
    pcy = cy + mhh * 0.3

    overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    d = ImageDraw.Draw(overlay)
    # Dark undertone, then blobby blue smudged along the lip line for an irregular edge.
    d.ellipse([cx - band_hw, pcy - band_hh, cx + band_hw, pcy + band_hh], fill=DARK)
    n = 11
    for i in range(n):
        t = i / (n - 1) - 0.5
        ex = cx + t * band_hw * 1.9 + rng.uniform(-mw * 0.05, mw * 0.05)
        ey = pcy + rng.uniform(-band_hh * 0.4, band_hh * 0.5)
        rw = mw * rng.uniform(0.12, 0.20)
        rh = band_hh * rng.uniform(0.7, 1.05)
        d.ellipse([ex - rw, ey - rh, ex + rw, ey + rh], fill=BLUE)

    # Drips running DOWN FROM the mouth — the main wet-paint feature. They originate
    # at the lower lip and stream onto the chin.
    for _ in range(rng.randint(6, 9)):
        dx = cx + rng.uniform(-band_hw, band_hw)
        top = cy + mhh * rng.uniform(0.5, 1.0)          # start at the lower lip
        length = mw * rng.uniform(1.2, 3.2)
        top_r = mw * rng.uniform(0.06, 0.13)
        bot_r = top_r * rng.uniform(0.4, 0.7)
        d.polygon([(dx - top_r, top), (dx + top_r, top),
                   (dx + bot_r, top + length), (dx - bot_r, top + length)], fill=BLUE)
        dr = bot_r * rng.uniform(1.3, 2.0)
        d.ellipse([dx - dr, top + length - dr * 0.6, dx + dr, top + length + dr * 1.4], fill=BLUE)
        if rng.random() < 0.5:
            dd = dr * rng.uniform(0.3, 0.5)
            dy2 = top + length + dr * 1.4 + mw * rng.uniform(0.2, 0.8)
            d.ellipse([dx - dd, dy2 - dd, dx + dd, dy2 + dd], fill=BLUE)

    overlay = overlay.filter(ImageFilter.GaussianBlur(max(1, int(mw * 0.04))))  # wet softening

    # Wet sheen along the lip band.
    hl = Image.new("RGBA", (W, H), (0, 0, 0, 0))
    ImageDraw.Draw(hl).arc(
        [cx - band_hw, pcy - band_hh, cx + band_hw, pcy + band_hh],
        20, 160, fill=(170, 210, 255, 170), width=max(2, int(mw * 0.05)))
    hl = hl.filter(ImageFilter.GaussianBlur(max(1, int(mw * 0.03))))

    base = Image.alpha_composite(im.convert("RGBA"), overlay)
    base = Image.alpha_composite(base, hl)

    # Punch the mouth opening back through everything so the lips + teeth stay
    # visible (paste with a soft elliptical mask of the ORIGINAL pixels).
    mask = Image.new("L", (W, H), 0)
    ImageDraw.Draw(mask).ellipse([cx - mhw, cy - mhh, cx + mhw, cy + mhh], fill=255)
    mask = mask.filter(ImageFilter.GaussianBlur(max(1, int(mw * 0.06))))
    base.paste(im.convert("RGBA"), (0, 0), mask)
    return base.convert("RGB")


def add_blue(image_data: bytes) -> bytes:
    """Detect the (largest) face's mouth and smear drippy blue paint around it.
    Works on photos (InsightFace/haar) and anime art (lbp cascade). Returns JPEG;
    on no detectable face or any error, returns the original image unchanged."""
    from PIL import Image, ImageOps
    loc = _locate_mouth(image_data)
    if loc is None:
        logger.info("blue: no face/mouth detected; returning original")
        return image_data
    try:
        with Image.open(io.BytesIO(image_data)) as im0:
            im = ImageOps.exif_transpose(im0).convert("RGB")
        cx, cy, mw = loc
        out_img = _draw_blue_paint(im, cx, cy, mw)
        out = io.BytesIO()
        out_img.save(out, format="JPEG", quality=92)
        return out.getvalue()
    except Exception as e:
        logger.warning(f"blue paint failed, using bare image: {e}")
        return image_data


def blue_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Smear drippy blue paint around the mouth of the first image attachment.
    Mirrors consider_attachments (image output, shared delivery path)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_blue(data)
        # Then stamp the KOSHER seal over the painted image (both are image->image).
        try:
            result = add_kosher(result)
        except Exception as e:
            logger.warning(f"blue: kosher stamp failed, keeping painted image: {e}")
        out = _alive_or_still(result, stem, "blue")
        summary = f"## 🔵 Blue\n\n🔵 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"blue failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"
