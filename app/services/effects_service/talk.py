"""Make a still picture TALK — a puppet lip-sync for the meme tools.

This module is the MOUTH only. It takes a picture and a path to already-generated
speech, and knows nothing about where that speech came from — the callers use the
app's CLONED-VOICE model (`voice_factory`, which owns the GPU lock, the VRAM swap
and the node round-robin), so the two halves queue on entirely different things:
speech on the GPU, mouth on the meme render queue. Keeping the split at "hand me
an audio file" is what lets the GPU discipline live in exactly one place.

The mouth is animated by WARPING the picture, not by a neural lip-sync model
(Wav2Lip/SadTalker/LatentSync). That is a deliberate choice:

  * portability — this runs on CPU with numpy + Pillow, so it behaves identically
    on the CUDA box, the Arc/XPU box and a Docker image with no GPU at all. Every
    diffusion-shaped feature in this repo has an Arc or ROCm gotcha; this has none,
    and it never takes ``GPUResourceLock`` (see video_service/music_local for why
    that lock is precious).
  * it works on DRAWINGS. Memes are half cartoon, and the neural models are trained
    on video of real faces — they smear on flat art. The mouth locator here already
    falls back to the anime cascade (see faces._locate_mouth), so a hand-drawn face
    gets the same treatment as a photo.
  * the crude look is the point. This is the Clutch Cargo / cheap-Saturday-cartoon
    jaw flap, which is funnier for a meme than an uncanny half-real mouth.

How a frame is built (see _render_frames for the code):
  1. Locate the mouth once — centre, width and the face's tilt.
  2. Turn the audio into a per-frame "how open" envelope (_audio_envelope).
  3. Per frame: paint a mouth INTERIOR (dark, with a tooth strip and a tongue) at
     the lip line, then paste the jaw — the picture's own pixels, through a
     feathered ellipse — shifted DOWN the face's own axis. The jaw hides the top of
     the interior, so opening the jaw reveals a mouth. No pixels are resampled: the
     jaw is an integer-offset paste of the original, so it stays as sharp as the
     source.

A closed frame (envelope ≈ 0) is the untouched original, so silence is a still.
"""
import io
import logging
import math
import os
import subprocess
from pathlib import Path
from typing import List, Optional, Tuple

from app.services.media_service import OutputFile, _human_size, is_image

logger = logging.getLogger(__name__)

# Working long edge. The clip is composited into a meme (or posted as a reaction), so a
# 4000px phone photo is wasted work — every frame is a full-image copy. Mirrors
# _SCATTER_ANIM_MAXDIM, and keeps the frame list (below) inside a sane RAM budget.
TALK_MAXDIM = 960
TALK_FPS = 20
# A talking meme is a punchline, not a monologue: the renderer is O(frames) full-image
# copies and the caller pays for TTS too. `talk` refuses longer input up front rather
# than rendering for a minute and timing out in the router.
TALK_MAX_DURATION = 30.0
TALK_MAX_CHARS = 400

# The animation's proportions. Jaw travel and the mask are scaled off the MOUTH-TO-CHIN
# distance, not the mouth width: how far a jaw can drop is a property of the jaw. The
# mouth's own width scales the cavity. Both come from the landmarks (see _face_geometry).
# Jaw travel at full openness, as a fraction of mouth→chin. Deliberately modest: the first cuts
# (0.55, then 0.45) gaped so wide on a loud syllable that the face read as a nutcracker rather than
# someone talking. The cavity is sized FROM this (see _mouth_interior), so lowering it narrows the
# whole mouth, not just the travel.
_JAW_DROP = 0.30
_JAW_ELL_HW = 1.00        # jaw mask ellipse half-width, × mouth width
_JAW_ELL_HH = 0.85        # jaw mask ellipse half-height, × mouth→chin
_JAW_ELL_DY = 0.55        # jaw mask ellipse centre below the lip line, × mouth→chin
_MOUTH_HALF_W = 0.42      # mouth-cavity half-width, × mouth width
_LIP_LINE = -0.06         # top of the jaw mask / cavity, × mouth width, above the lip line
_OPEN_EPS = 0.05          # below this the frame is left as the original still

# InsightFace's 106-point model, indices verified by plotting them (see docs/TALK.md):
# 0-32 is the face contour (chin at the bottom of it) and 52-71 is the lip outline.
# InsightFace ships no semantic table for these, so they are measured, not assumed.
_LMK_LIPS = slice(52, 72)
_LMK_CONTOUR = slice(0, 33)

_TALK_APP = None
_TALK_APP_TRIED = False


def _talk_app():
    """Lazily build an InsightFace app with the 106-point landmark model, cached.

    Deliberately NOT ``faces._insightface_app()``: that one is detection-only and is
    shared by the thug/blue overlays, so widening it would change their cost and their
    behaviour. The 5 detection keypoints are not good enough here — SCRFD's "mouth
    corners" sit at NOSTRIL height on a lot of faces (measured: 6px high on a 36px mouth,
    which puts the whole animation on the philtrum), while the 106-point lip outline lands
    on the actual lip seam. The weights are already in the buffalo_l pack that detection
    downloads, so this costs a model load, not a download.
    """
    global _TALK_APP, _TALK_APP_TRIED
    if _TALK_APP_TRIED:
        return _TALK_APP
    _TALK_APP_TRIED = True
    try:
        import warnings
        warnings.filterwarnings("ignore")
        from insightface.app import FaceAnalysis
        app = FaceAnalysis(name="buffalo_l", allowed_modules=["detection", "landmark_2d_106"],
                           providers=["CPUExecutionProvider"])
        app.prepare(ctx_id=-1, det_size=(640, 640))
        _TALK_APP = app
    except Exception as e:
        logger.warning(f"talk: 106-point landmarks unavailable ({e}); using the cascade fallback")
        _TALK_APP = None
    return _TALK_APP


def _face_geometry(image_data: bytes) -> Optional[Tuple[float, float, float, float, float]]:
    """``(cx, cy, mouth_width, angle_deg, mouth_to_chin)`` for the best face, or None.

    Everything is measured in the FACE's own frame, not the screen's: the tilt comes from
    the eye keypoints (same ``atan2(dy, dx)`` convention as the THUG overlay, so PIL
    rotates by ``-angle``), and the mouth width / chin distance are the landmark extents
    along that rotated frame. A tilted face whose jaw dropped straight down the SCREEN
    would slide sideways off the chin.

    Fallback chain, widest-to-narrowest capability:
      1. 106-point landmarks — the lip seam and the real chin.
      2. the 5 detection keypoints, with the measured downward correction to get off the
         nostrils, and a chin estimated from the box.
      3. ``faces._locate_mouth`` — the haar/anime cascade path the `blue` effect uses, so
         flat and hand-drawn art still animates. No eye line, so it assumes upright.
    """
    import numpy as np
    from . import faces

    def _frame(kps):
        """Face tilt in degrees, clamped. A wildly tilted 'face' is a bad detection far more
        often than a head lying on its side, and the animation would land somewhere absurd."""
        ang = math.degrees(math.atan2(float(kps[1][1] - kps[0][1]), float(kps[1][0] - kps[0][0])))
        return 0.0 if abs(ang) > 45 else ang

    app = _talk_app()
    if app is not None:
        try:
            import cv2
            from PIL import Image, ImageOps
            with Image.open(io.BytesIO(image_data)) as im0:
                im = ImageOps.exif_transpose(im0).convert("RGB")
            bgr = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
            dets = [d for d in (app.get(bgr) or [])
                    if getattr(d, "kps", None) is not None and len(d.kps) >= 5]
            if dets:
                # Ranked by MOUTH width, not bounding-box area — the other overlays pick the
                # largest box, but here the mouth is what gets animated, and on a group shot
                # the boxes come out within a percent of each other while the mouths do not
                # (a turned face has a full-size box and a mouth half the width). Measured on
                # a 6-face poster: areas 8910 vs 8881 — a JPEG re-encode flipped that winner —
                # against mouth widths of 48.6 vs 22.0. So this is also the STABLE key.
                f = max(dets, key=lambda d: math.hypot(float(d.kps[4][0] - d.kps[3][0]),
                                                       float(d.kps[4][1] - d.kps[3][1])))
                angle = _frame(f.kps)
                rad = math.radians(angle)
                right = np.array([math.cos(rad), math.sin(rad)], dtype=np.float64)
                down = np.array([-math.sin(rad), math.cos(rad)], dtype=np.float64)
                lmk = getattr(f, "landmark_2d_106", None)
                if lmk is not None and len(lmk) >= 106:
                    lips = np.asarray(lmk[_LMK_LIPS], dtype=np.float64)
                    centre = lips.mean(axis=0)              # the lip seam, not the upper lip
                    along = lips @ right
                    mw = max(8.0, float(along.max() - along.min()))
                    contour = np.asarray(lmk[_LMK_CONTOUR], dtype=np.float64)
                    # Chin = the contour point furthest DOWN the face's own axis.
                    chin = float((contour @ down).max() - float(centre @ down))
                    if chin < 0.25 * mw:                    # nonsense geometry — use the norm
                        chin = 0.70 * mw
                    return (float(centre[0]), float(centre[1]), mw, angle, chin)
                # No landmarks on this face: the keypoint mouth, nudged down off the nostrils.
                lm, rm = f.kps[3], f.kps[4]
                mw = max(8.0, math.hypot(float(rm[0] - lm[0]), float(rm[1] - lm[1])))
                c = np.array([(lm[0] + rm[0]) / 2.0, (lm[1] + rm[1]) / 2.0]) + down * (0.17 * mw)
                return (float(c[0]), float(c[1]), mw, angle, 0.70 * mw)
        except Exception as e:
            logger.warning(f"talk: insightface path failed, falling back: {e}")

    loc = faces._locate_mouth(image_data)
    if loc is None:
        return None
    cx, cy, mw = loc
    return (float(cx), float(cy), float(mw), 0.0, 0.70 * float(mw))


def _decode_pcm(audio_path: str, rate: int = 16000):
    """Decode any audio file to a mono float32 numpy array at `rate` Hz (empty on failure)."""
    import numpy as np
    from app.services.media_service import resolve_ffmpeg

    ffmpeg = resolve_ffmpeg()
    if not (ffmpeg and audio_path and os.path.exists(audio_path)):
        return np.zeros(0, dtype=np.float32)
    try:
        p = subprocess.run(
            [ffmpeg, "-v", "error", "-i", audio_path, "-f", "s16le", "-acodec", "pcm_s16le",
             "-ac", "1", "-ar", str(rate), "-"],
            capture_output=True, timeout=300)
        if p.returncode != 0 or not p.stdout:
            logger.warning(f"talk: could not decode audio: {(p.stderr or b'')[-200:]!r}")
            return np.zeros(0, dtype=np.float32)
        # An odd trailing byte would make frombuffer raise on a truncated decode.
        raw = p.stdout[: len(p.stdout) // 2 * 2]
        return np.frombuffer(raw, dtype="<i2").astype(np.float32) / 32768.0
    except Exception as e:
        logger.warning(f"talk: audio decode failed ({e})")
        return np.zeros(0, dtype=np.float32)


def _audio_envelope(audio_path: str, fps: int = TALK_FPS,
                    max_duration: float = TALK_MAX_DURATION):
    """Per-frame ``(openness, width)`` in 0..1, driving the jaw and the mouth's shape.

    The clip's LENGTH comes out of this too — it is one sample per frame, so the caller
    gets the frame count from ``len()`` rather than asking ffprobe how long the audio is.
    One decode answers both questions; a separate probe would be a second subprocess over
    the same file, and the only `ffprobe`-by-bare-name call in the module.

    `openness` is a loudness envelope: per-frame RMS, normalised against the clip's own
    92nd percentile (NOT its peak — one plosive would otherwise mumble the whole line),
    gamma'd, noise-gated so silence closes the mouth, then one-poled with a fast attack
    and a slow release. That asymmetry is what makes it read as speech: a mouth snaps
    open on a consonant and closes lazily.

    `width` is the spectral centroid, normalised over the clip. Bright frames (ee/ss)
    spread the mouth wide, dark ones (oo/mm) round it — a cheap stand-in for visemes that
    keeps the flap from looking like a metronome. It is only worth a small modulation.

    Raises RuntimeError when nothing decodes. There is no mouthing-to-nothing fallback on
    purpose: whatever ffmpeg cannot read here, it cannot mux into the video either, so a
    fallback would only produce a silent clip of a face chewing.
    """
    import numpy as np

    x = _decode_pcm(audio_path)
    if x.size < 2:
        raise RuntimeError("the speech audio came back empty")

    rate = 16000
    hop = max(1, int(round(rate / float(fps))))
    x = x[: int(max_duration * rate)]
    n_frames = max(1, -(-x.size // hop))          # ceil: a part-full last frame still counts
    # One row per frame, zero-padded so the last (short) window is still analysed.
    need = n_frames * hop
    if x.size < need:
        x = np.concatenate([x, np.zeros(need - x.size, dtype=np.float32)])
    win = x[:need].reshape(n_frames, hop)

    rms = np.sqrt(np.maximum((win ** 2).mean(axis=1), 0.0))
    loud = rms[rms > 1e-4]
    ref = float(np.percentile(loud, 92)) if loud.size else 0.0
    if ref <= 1e-6:
        return np.zeros(n_frames, dtype=np.float32), np.full(n_frames, 0.5, dtype=np.float32)
    # Gamma ABOVE 1 compresses the quiet end. Below 1 (the first cut used 0.65) it EXPANDS it, so
    # room tone and the tail of every syllable drove a half-open mouth and the face chattered
    # continuously — the "it looks weird" of a jaw that never rests.
    a = np.clip(rms / ref, 0.0, 1.0) ** 1.15
    a[a < 0.12] = 0.0                                  # noise gate — silence is a closed mouth

    out = np.zeros(n_frames, dtype=np.float32)
    prev = 0.0
    for i, v in enumerate(a):
        # Fast attack, slower release — a mouth snaps open on a consonant and closes lazily. Both
        # are gentler than the first cut (0.65/0.28): at 20fps those tracked the waveform so
        # closely that the jaw buzzed on every syllable instead of moving like a jaw.
        k = 0.45 if v > prev else 0.20
        prev = prev + k * (float(v) - prev)
        out[i] = prev

    # Spectral centroid per frame → mouth width. rfft on the (short) frame windows is
    # cheap and needs no scipy.
    mag = np.abs(np.fft.rfft(win * np.hanning(hop).astype(np.float32), axis=1))
    freqs = np.fft.rfftfreq(hop, 1.0 / rate).astype(np.float32)
    tot = mag.sum(axis=1)
    cen = np.where(tot > 1e-6, (mag * freqs).sum(axis=1) / np.maximum(tot, 1e-6), 0.0)
    lo, hi = float(np.percentile(cen, 10)), float(np.percentile(cen, 90))
    width = np.clip((cen - lo) / (hi - lo), 0.0, 1.0) if hi - lo > 1.0 else np.full(n_frames, 0.5)
    return out.astype(np.float32), width.astype(np.float32)


def _mouth_interior(mw: float, drop: float, width: float, angle: float):
    """The inside of an open mouth, as an RGBA patch already rotated to the face's tilt,
    plus the offset from the lip seam to the patch's centre.

    Sized from the JAW TRAVEL, not from an independent "openness" scale: the visible
    opening IS the gap the jaw uncovers, so the cavity runs from just above the lip seam
    down to just past where the jaw will be. Any taller and it paints over the philtrum
    and the nose — the cavity is composited ON TOP of the picture, so an ellipse centred
    on the seam puts half of itself over the upper lip.

    Drawn face-UPRIGHT on its own canvas and then rotated, because Pillow can only draw an
    axis-aligned ellipse. Dark cavity, a tooth strip under the top lip and a tongue at the
    bottom — without those it reads as a black hole punched in the face.
    """
    from PIL import Image, ImageDraw, ImageFilter

    # The cavity starts AT the seam, not above it. Starting above (where the jaw mask is
    # cut) bleaches the upper lip: the cavity is composited on top of the picture, so its
    # tooth strip lands on the lip and the whole thing reads as a grey smear rather than an
    # open mouth. The jaw's own feathered top edge covers the seam.
    top = 0.0
    bot = drop + mw * 0.06
    hh = (bot - top) / 2.0
    off = (bot + top) / 2.0                  # cavity centre, below the seam
    hw = mw * _MOUTH_HALF_W * (0.86 + 0.28 * float(width))
    r = int(math.ceil(max(hw, hh) * 1.5)) + 4
    size = r * 2
    canvas = Image.new("RGBA", (size, size), (0, 0, 0, 0))
    box = [r - hw, r - hh, r + hw, r + hh]
    ImageDraw.Draw(canvas).ellipse(box, fill=(28, 12, 14, 255))
    clip = Image.new("L", (size, size), 0)
    ImageDraw.Draw(clip).ellipse(box, fill=255)
    black = Image.new("L", (size, size), 0)
    if hh > 4.0:
        # Upper teeth: a strip under the top lip, clipped to the cavity. Kept THIN and off
        # white — a fat bright band is the whole cavity on a small mouth, and then the flap
        # looks like a smear of light instead of a hole.
        teeth = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ImageDraw.Draw(teeth).rectangle(
            [r - hw, r - hh, r + hw, r - hh + max(2.0, hh * 0.24)], fill=(214, 206, 194, 255))
        canvas.paste(teeth, (0, 0), Image.composite(clip, black, teeth.split()[3]))
        # Tongue: a soft blob resting on the lower lip.
        tw, th = hw * 0.62, hh * 0.46
        tongue = Image.new("RGBA", (size, size), (0, 0, 0, 0))
        ImageDraw.Draw(tongue).ellipse([r - tw, r + hh - th * 1.7, r + tw, r + hh + th * 0.3],
                                       fill=(158, 74, 84, 255))
        canvas.paste(tongue, (0, 0), Image.composite(clip, black, tongue.split()[3]))
    # Soften the rim so the cavity sits under the lips instead of on top of them.
    canvas = canvas.filter(ImageFilter.GaussianBlur(max(0.8, mw * 0.03)))
    if abs(angle) > 0.5:
        canvas = canvas.rotate(-angle, resample=Image.BICUBIC)   # same convention as the THUG overlay
    return canvas, off


def _jaw_mask(size: Tuple[int, int], cx: float, cy: float, mw: float, chin: float, angle: float):
    """Feathered "L" mask over the lower lip, chin and jaw, in IMAGE coordinates.

    Built face-upright on a square canvas (ellipse + a cut along the lip line, which is
    what stops the nose and the upper lip travelling with the jaw), then rotated onto the
    face's tilt and dropped into a full-size mask. Its height follows the measured
    mouth→chin distance, so it covers the jaw and not the neck; its width stays inside the
    face, because background dragged down beside the chin is the artefact that reads as
    broken rather than as cheap animation. Feathered for the same reason.
    """
    from PIL import Image, ImageDraw, ImageFilter

    hw, hh, dy = mw * _JAW_ELL_HW, chin * _JAW_ELL_HH, chin * _JAW_ELL_DY
    r = int(math.ceil(max(hw, hh + abs(dy)))) + 8
    n = r * 2
    local = Image.new("L", (n, n), 0)
    d = ImageDraw.Draw(local)
    d.ellipse([r - hw, r + dy - hh, r + hw, r + dy + hh], fill=255)
    d.rectangle([0, 0, n, r + mw * _LIP_LINE], fill=0)          # nothing above the lip line moves
    # Feather enough to hide the side seams, but no more: the softer this edge, the more
    # of the ORIGINAL lower lip shows through the cavity at the top of the travel, which
    # reads as a doubled lip.
    local = local.filter(ImageFilter.GaussianBlur(max(1.0, mw * 0.06)))
    if abs(angle) > 0.5:
        local = local.rotate(-angle, resample=Image.BICUBIC)
    mask = Image.new("L", size, 0)
    mask.paste(local, (int(round(cx)) - r, int(round(cy)) - r))
    return mask


def _render_frames(base, cx: float, cy: float, mw: float, chin: float, angle: float,
                   openness, width):
    """Yield one RGB frame per envelope sample. A generator on purpose: a 30s clip is 600
    full-size frames and materialising them is hundreds of MB (frames_to_video consumes
    this lazily for the single-pass case)."""
    W, H = base.size
    mask = _jaw_mask((W, H), cx, cy, mw, chin, angle)
    bbox = mask.getbbox()
    rad = math.radians(angle)
    dxu, dyu = -math.sin(rad), math.cos(rad)            # the face's own "down" (see _face_geometry)
    for a, wdt in zip(openness, width):
        a = float(a)
        if a < _OPEN_EPS or bbox is None:
            yield base.copy()                            # silence is the untouched picture
            continue
        frame = base.copy()
        drop = a * _JAW_DROP * chin
        # The cavity is drawn first and the jaw is pasted over it, so what shows through is
        # exactly the band the jaw uncovered.
        interior, off = _mouth_interior(mw, drop, float(wdt), angle)
        frame.paste(interior, (int(round(cx + off * dxu)) - interior.width // 2,
                               int(round(cy + off * dyu)) - interior.height // 2), interior)
        dx = int(round(drop * dxu))
        dy = int(round(drop * dyu))
        if dx or dy:
            # Integer-offset paste of the ORIGINAL pixels — the jaw is never resampled, so
            # it stays exactly as sharp as the source picture.
            #
            # The mask is cropped at the SOURCE box, not the destination: the jaw's alpha has
            # to TRAVEL with the jaw. Reading it at the destination instead leaves the mask
            # sitting still while the pixels move, so the jaw repaints its own original
            # footprint — including the band at the top that the drop was supposed to
            # uncover, which is exactly where the mouth cavity is. The cavity was drawn and
            # then immediately covered over with cheek.
            #
            # A travelling mask is also what keeps the boundary INVISIBLE, and that is not a
            # detail. An attempt to model the jaw as a hinge — chin translated, the skin below
            # it squeezed to absorb the travel — needed a static mask, and a static mask means
            # the pasted region starts at full alpha: a hard step right across the cheeks, far
            # worse than the thing it set out to fix. The alpha ramp here cross-fades moved and
            # unmoved pixels instead, which reads as motion blur. Don't re-attempt the hinge
            # without a real per-pixel displacement field; a rigid region with a soft edge beats
            # a better-shaped region with a hard one.
            x0, y0, x1, y1 = bbox
            sx0, sy0 = max(0, x0 - dx), max(0, y0 - dy)
            src = base.crop((sx0, sy0, min(W, x1 - dx), min(H, y1 - dy)))
            frame.paste(src, (sx0 + dx, sy0 + dy),
                        mask.crop((sx0, sy0, sx0 + src.width, sy0 + src.height)))
        else:
            frame.paste(base, (0, 0), mask)
        yield frame


def _has_alpha(im) -> bool:
    """True if the picture actually USES its alpha channel (not merely has one)."""
    if im.mode not in ("RGBA", "LA", "PA") and "transparency" not in im.info:
        return False
    try:
        a = im.convert("RGBA").getchannel("A")
        return a.getextrema()[0] < 255
    except Exception:
        return False


def add_talk(image_data: bytes, audio_path: str, fps: int = TALK_FPS,
             keep_alpha: bool = False) -> tuple:
    """Animate the face in `image_data` speaking `audio_path`. Returns ``(bytes, content_type)``.

    With `keep_alpha` and a picture that actually uses transparency, the result is a SILENT
    VP9-alpha WebM instead of an MP4 with sound. That is not a preference, it is the only
    combination that exists: MP4 has no alpha channel at all, so a cut-out rendered to MP4
    comes back as a black rectangle with the subject pasted on it (exactly what a
    background-removed Meme Builder layer did), and an audio stream in a VP9-alpha WebM
    corrupts the alpha on this ffmpeg — see media_service._ALPHA_VCODEC, which is why every
    other alpha layer is silent too. The caller puts the speech on the timeline as its own
    audio layer; `content_type` is how it knows which it got.

    Callers that need ONE self-contained file (chat, Telegram) leave `keep_alpha` off and
    take the MP4: a transparent clip with no audio is useless as a standalone reply.

    Raises RuntimeError when there is no usable face (the caller turns that into the
    "I couldn't find a face" reply), when the audio won't decode, or when the encode
    fails — unlike the still overlays in faces.py, silently returning the input would hand
    back a picture where a video was asked for, which the Meme Builder would then hang on
    the timeline as a one-frame layer.
    """
    from PIL import Image, ImageOps
    from app.services.media_service import frames_to_alpha_video, frames_to_video, mux_audio_loop

    with Image.open(io.BytesIO(image_data)) as im0:
        im0 = ImageOps.exif_transpose(im0)
        alpha = bool(keep_alpha) and _has_alpha(im0)
        im = im0.convert("RGBA" if alpha else "RGB")
    # Downscale BEFORE detecting, so the geometry is already in the working image's
    # coordinates — scaling a detection afterwards is the classic off-by-a-factor bug.
    if max(im.size) > TALK_MAXDIM:
        im.thumbnail((TALK_MAXDIM, TALK_MAXDIM), Image.LANCZOS)
    # Detection always runs on flat RGB bytes: cv2 wants three channels, and a cut-out's
    # transparent region carries whatever RGB happened to be underneath, which is not a face.
    buf = io.BytesIO()
    im.convert("RGB").save(buf, format="JPEG", quality=92)
    image_data = buf.getvalue()

    # Distinguish "this node cannot see faces at all" from "there is no face in this picture".
    # The lean nostr-only image ships neither insightface nor opencv, and answering "no face found"
    # there sends someone off to find a better photo forever.
    import importlib.util
    if importlib.util.find_spec("cv2") is None:
        raise RuntimeError("face detection isn't installed on this node, so I can't animate a mouth")

    geom = _face_geometry(image_data)
    if geom is None:
        raise RuntimeError("no face found in that picture — try one where the face is bigger")
    cx, cy, mw, angle, chin = geom
    # A mouth a handful of pixels wide cannot be animated into anything but mush, and the
    # mask/ellipse maths degenerates below a few pixels.
    if mw < 12:
        raise RuntimeError("the face is too small in that picture to animate the mouth")

    # One envelope sample per video frame, so the clip's length IS the speech's length.
    openness, width = _audio_envelope(audio_path, fps)
    frames = _render_frames(im, cx, cy, mw, chin, angle, openness, width)
    if alpha:
        # Silent by necessity — see the docstring. The caller adds the speech as its own layer.
        return frames_to_alpha_video(frames, fps=fps), "video/webm"
    silent = frames_to_video(frames, fps=fps)
    # The video is exactly the audio's length, so the loop in mux_audio_loop never repeats;
    # it is reused for its `-shortest` + best-effort behaviour, not for the looping.
    return mux_audio_loop(silent, audio_path), "video/mp4"


def talk_attachments(
    attachments: List[Tuple[str, bytes, str]],
    audio_path: str,
) -> Tuple[List[OutputFile], str]:
    """Make the first image attachment lip-sync `audio_path`. Mirrors the other
    ``*_attachments`` processors (same shape → one delivery path for web/Telegram).

    Always the MP4-with-sound form: a chat reply has to be one self-contained file, and the
    transparent variant is silent (see add_talk)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach a picture of a face first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result, _ct = add_talk(data, audio_path)
        out: OutputFile = {
            "filename": f"{stem}_talk.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        return [out], f"## 🗣️ Talk\n\n🗣️ {filename}: {_human_size(len(result))}"
    except RuntimeError as e:
        # The refusals add_talk raises are things the PICTURE is wrong about (no face, too small,
        # no audio) — routine user mistakes, so they get a line, not a stack trace in the journal.
        logger.info(f"talk declined {filename}: {e}")
        return [], f"❌ {filename}: {e}"
    except Exception as e:
        logger.error(f"talk failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"
