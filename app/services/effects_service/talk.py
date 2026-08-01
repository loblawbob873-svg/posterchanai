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
  * the crude look is the point. This is the Clutch Cargo / cheap-Saturday-cartoon
    jaw flap, which is funnier for a meme than an uncanny half-real mouth.

TWO renderers, because one operation cannot serve both kinds of picture:

  * a PHOTOGRAPH is WARPED (_render_frames) — its own jaw pixels move, so it keeps
    the face's real detail.
  * FLAT ART is REDRAWN (_render_anime_frames) — a cel-shaded mouth is a hard ink
    stroke on a flat fill, and sliding it duplicates and smears it. Anime lip-sync
    has never worked by warping either; it swaps a drawn mouth per frame.

And the mouth can be PLACED BY HAND (`mouth=`), which is what makes this reliable
rather than lucky. Every face model here was trained on photographs: InsightFace
will happily detect an anime face and then put the mouth landmarks on the chin and
a cheek — a confident wrong answer, which is worse than none. The Meme Builder
seeds a marker from detection and lets the user correct it; see docs/TALK.md.

How a frame is built (see _render_frames for the code):
  1. Locate the mouth once — centre, width and the face's tilt (or take the one the
     user placed).
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

# ANIME proportions, as fractions of the anime cascade's FACE BOX. Measured off real art (a 1920x1080
# Chainsaw Man still and the PosterChan mascot), not guessed: the mouth sits at 0.72-0.75 of the box
# height on both, and is 0.10-0.14 of its width. The cascade's own estimate is 0.42 of the width —
# that number belongs to the `blue` effect, which paints a smear AROUND the mouth and wants to be
# generous. Used for lip-sync it makes a mouth roughly three times too wide, which is the "doesn't
# work on anime at all" report: a cavity spanning half the face.
_ANIME_MOUTH_X = 0.50     # of box width
_ANIME_MOUTH_Y = 0.76     # of box height
_ANIME_MOUTH_W = 0.13     # of box width
_ANIME_CHIN = 0.20        # of box height, below the mouth

# The DRAWN mouth, as fractions of the mouth width. These are half-extents, so the open mouth spans
# roughly the mouth it replaces — which sounds obvious and was not what the first cut did: at
# 0.62 + 0.26 it drew a cavity 1.5-1.8x WIDER than the mouth (90-106px on a 60px mouth), and the
# erase behind it then had to be a 112x55px oval spilling onto both cheeks. That oval is what
# "an ugly shadow around the mouth" was, and making the erase properly opaque only made it stand
# out more. Those numbers came from an era when `mw` was the anime cascade's over-estimate; with a
# hand-placed mouth `mw` is the real width, so the drawing has to respect it.
_ANIME_CAV_W = 0.40       # cavity half-width at rest, × mouth width
_ANIME_CAV_WMOD = 0.14    # extra half-width on a bright vowel
# A mouth opens DOWNWARD — the lower lip drops, the upper one barely moves. Centring the cavity on
# the lip line instead made it eat upward into the philtrum and downward into the chin in equal
# measure, and the erase behind it then had to wipe the lower lip and its shading, leaving flat skin
# under the mouth. That is the "shadow below the lips". So the cavity is anchored just ABOVE the lip
# line and grows down from there.
# The marker IS the mouth, so the drawn mouth is CENTRED on it, with only a slight downward bias
# because a real mouth opens by dropping the lower lip. Anchoring the cavity at the marker and
# growing it entirely downward was anatomically defensible and wrong in practice: you place the bar
# on the lips and the mouth renders below it, which is the whole feature missing its own target.
_ANIME_CAV_TOP = -0.26    # cavity top, × mouth width, relative to the marker
_ANIME_CAV_DROP = 0.42    # how far the cavity reaches BELOW the marker at full openness

# InsightFace's 106-point model, indices verified by plotting them (see docs/TALK.md):
# 0-32 is the face contour (chin at the bottom of it) and 52-71 is the lip outline.
# InsightFace ships no semantic table for these, so they are measured, not assumed.
_LMK_LIPS = slice(52, 72)
_LMK_CONTOUR = slice(0, 33)

# Cel art vs a photograph, by how much of the picture is EXACTLY flat. Line art is built from large
# uniform fills; a photograph has grain and texture everywhere, so almost nothing is flat. Measured
# over the samples here: photos 0.13 and 0.20, drawings 0.32 / 0.45 / 0.62 / 0.68 / 0.79 / 0.79 — a
# gap wide enough that the threshold is not delicate.
#
# This is what decides WARP vs REDRAW, and it is a far better signal than "did the anime face
# cascade fire", which is what decided it before. That cascade does not fire on plenty of drawings
# at all, so those defaulted to Photo and got the warp — a smear on flat art. It is why a fixed
# anime renderer could still look completely unchanged: the anime renderer was never running.
# Measured over the SUBJECT (see _is_flat_art) across eleven samples: photos 0.06 / 0.10 / 0.14 /
# 0.20, drawings 0.22 / 0.31 / 0.33 / 0.36 / 0.48 / 0.51 / 0.79. The gap between the classes is only
# 0.017 wide, so this is an educated GUESS, not a classifier — a heavily shaded illustration can sit
# on the photo side of it. A palette-size metric was tried as a second signal and separates nothing
# (it tracks image size, not style). That thin margin is exactly why the Meme Builder asks rather
# than decides, and why it remembers the answer: a wrong default is one tap to fix, and the picker
# will not offer the same wrong one twice.
_FLAT_ART_THRESHOLD = 0.21


def _is_flat_art(im) -> bool:
    """True if this looks like a DRAWING rather than a photograph.

    Measured over the SUBJECT only. A cut-out's transparent background is perfectly uniform, so
    counting it makes every background-removed PHOTO look like line art — which is exactly what it
    did to the Jerry pose (a Seinfeld still, cut out) and would do to any layer that has been
    through Remove the background.
    """
    try:
        import cv2
        import numpy as np
        rgba = im.convert("RGBA")
        rgba.thumbnail((512, 512))
        g = np.asarray(rgba.convert("L"), dtype=np.float32)
        m = cv2.blur(g, (3, 3))
        var = cv2.blur(g * g, (3, 3)) - m * m
        keep = np.asarray(rgba.getchannel("A")) > 128
        # Erode the subject a little: the feathered edge of a cut-out is a gradient that belongs to
        # neither side, and on a thin subject it would otherwise dominate the sample.
        keep = cv2.erode(keep.astype("uint8"), np.ones((5, 5), "uint8")).astype(bool)
        if keep.sum() < 64:
            keep = np.ones_like(keep)
        return float((var[keep] < 1.0).mean()) >= _FLAT_ART_THRESHOLD
    except Exception:
        return False


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


def _anime_geometry(image_data: bytes):
    """``(cx, cy, mouth_width, angle_deg, mouth_to_chin)`` for a STYLISED anime face, or None.

    Returns None unless the anime cascade fires and the real-face cascade does not — that
    combination is what says "this is flat art, not a photograph", and it is the gate that keeps
    photos and semi-realistic drawn characters on the landmark path where they belong.

    The geometry is derived from the cascade BOX by fixed proportions (measured, see the constants),
    because nothing here produces anime landmarks. The TILT still comes from InsightFace's eye
    keypoints when it has any: those land correctly on anime even though its mouth output does not.
    """
    try:
        import cv2
        import numpy as np
        from PIL import Image, ImageOps
        from . import faces
    except Exception:
        return None
    try:
        with Image.open(io.BytesIO(image_data)) as im0:
            im = ImageOps.exif_transpose(im0).convert("RGB")
        gray = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2GRAY)
        boxes = faces._detect_thug_faces(gray, cv2.equalizeHist(gray), im.width, im.height)
        if not boxes or any(kind != "anime" for *_, kind in boxes):
            return None
        x, y, w, h, _ = max(boxes, key=lambda f: f[2] * f[3])
        cx = x + _ANIME_MOUTH_X * w
        cy = y + _ANIME_MOUTH_Y * h
        mw = max(8.0, _ANIME_MOUTH_W * w)
        chin = max(6.0, _ANIME_CHIN * h)
        angle = 0.0
        app = _talk_app()
        if app is not None:
            try:
                bgr = cv2.cvtColor(np.asarray(im), cv2.COLOR_RGB2BGR)
                dets = [d for d in (app.get(bgr) or [])
                        if getattr(d, "kps", None) is not None and len(d.kps) >= 5]
                if dets:
                    # The face whose box overlaps this one — on a group shot the anime box we picked
                    # is the one being animated, so the tilt has to come from the same head.
                    def _near(d):
                        b = d.bbox
                        return abs((b[0] + b[2]) / 2 - (x + w / 2)) + abs((b[1] + b[3]) / 2 - (y + h / 2))
                    f = min(dets, key=_near)
                    a = math.degrees(math.atan2(float(f.kps[1][1] - f.kps[0][1]),
                                                float(f.kps[1][0] - f.kps[0][0])))
                    if abs(a) <= 45:
                        angle = a
            except Exception:
                angle = 0.0
        logger.info("talk: anime face %dx%d -> mouth %.0f,%.0f w=%.1f chin=%.1f ang=%.1f",
                    w, h, cx, cy, mw, chin, angle)
        return (float(cx), float(cy), float(mw), float(angle), float(chin), True)
    except Exception as e:
        logger.warning(f"talk: anime detection failed: {e}")
        return None


def _face_geometry(image_data: bytes) -> Optional[Tuple[float, float, float, float, float, bool]]:
    """``(cx, cy, mouth_width, angle_deg, mouth_to_chin, is_anime)`` for the best face, or None.

    `is_anime` picks the RENDERER: flat art is redrawn, a photograph is warped. See
    _render_anime_frames for why one operation cannot serve both.

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

    # ANIME FIRST, and that order is the whole fix. InsightFace happily DETECTS a stylised anime
    # face — its box and its two EYE keypoints land correctly — but its landmark models are trained
    # on photographs and their mouth output is nonsense on flat art: measured on a Chainsaw Man
    # still, the two "mouth corners" came back on the chin and on a cheek, giving a mouth 1.7x too
    # wide and 16px too low. A confident wrong answer is worse than no answer, because it means the
    # anime path below is never reached. So when the anime cascade fires and the REAL one does not,
    # trust the anime box. (Checked across photos, semi-realistic drawn characters and true anime:
    # this condition is true for exactly the anime, and the drawn characters InsightFace already
    # handles well keep their landmark path.)
    anime = _anime_geometry(image_data)
    if anime is not None:
        return anime

    # WHERE the mouth is and WHAT to do with it are two different questions, and only the first one
    # the cascade above can answer. Whether to warp or redraw is decided by the picture's flatness,
    # so a drawing that InsightFace locates perfectly well still gets the redraw it needs.
    try:
        from PIL import Image as _I, ImageOps as _IO
        with _I.open(io.BytesIO(image_data)) as _im:
            flat = _is_flat_art(_IO.exif_transpose(_im))
    except Exception:
        flat = False

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
                    return (float(centre[0]), float(centre[1]), mw, angle, chin, flat)
                # No landmarks on this face: the keypoint mouth, nudged down off the nostrils.
                lm, rm = f.kps[3], f.kps[4]
                mw = max(8.0, math.hypot(float(rm[0] - lm[0]), float(rm[1] - lm[1])))
                c = np.array([(lm[0] + rm[0]) / 2.0, (lm[1] + rm[1]) / 2.0]) + down * (0.17 * mw)
                return (float(c[0]), float(c[1]), mw, angle, 0.70 * mw, flat)
        except Exception as e:
            logger.warning(f"talk: insightface path failed, falling back: {e}")

    loc = faces._locate_mouth(image_data)
    if loc is None:
        return None
    cx, cy, mw = loc
    return (float(cx), float(cy), float(mw), 0.0, 0.70 * float(mw), flat)


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


def _skin_tone(base, cx: float, cy: float, mw: float):
    """The character's skin colour just ABOVE the mouth — the band between nose and lip, which on
    cel-shaded art is a single flat fill. Median, so the ink line of the mouth itself (and any
    stray blush pixel) cannot drag it."""
    import numpy as np

    W, H = base.size
    # SIDES of the mouth, not above it. Above is the nose and its shadow, which on cel art is a
    # different, darker fill — sampling there tinted the cover-up and left a visible dark halo
    # where it met the real cheek. Level with the mouth, just outside it, is flat skin.
    rgb = base.convert("RGB")
    bands = []
    # Close in on BOTH sides and just below. Far out is hair, a hand or the background — on the
    # Chainsaw Man still, sampling two mouth-widths out picked up her hair and tinted the cover.
    y0, y1 = int(max(0, cy - mw * 0.25)), int(min(H, cy + mw * 0.25))
    for x0, x1 in ((cx - mw * 1.25, cx - mw * 0.75), (cx + mw * 0.75, cx + mw * 1.25)):
        a, b = int(max(0, x0)), int(min(W, x1))
        if b - a >= 2 and y1 - y0 >= 2:
            bands.append(np.asarray(rgb.crop((a, y0, b, y1)), dtype=np.int16).reshape(-1, 3))
    cy0, cy1 = int(max(0, cy + mw * 0.55)), int(min(H, cy + mw * 1.0))
    cx0, cx1 = int(max(0, cx - mw * 0.5)), int(min(W, cx + mw * 0.5))
    if cx1 - cx0 >= 2 and cy1 - cy0 >= 2:
        bands.append(np.asarray(rgb.crop((cx0, cy0, cx1, cy1)), dtype=np.int16).reshape(-1, 3))
    if not bands:
        return (240, 214, 198)
    patch = np.concatenate(bands, axis=0)
    lum = patch.mean(axis=1)
    keep = patch[lum > np.percentile(lum, 35)]      # drop the darkest third: line art and shadow
    if not len(keep):
        keep = patch
    return tuple(int(v) for v in np.median(keep, axis=0))


def _mouth_erased(base, cx: float, cy: float, mw: float, hw: float, hh: float,
                  dy: float = 0.0, angle: float = 0.0):
    """`base` with the drawn mouth painted out, following the picture's own shading.

    A FLAT fill was the obvious thing and it is visibly wrong: cel art still has shaded regions —
    here a hand throwing a shadow across the chin — so a single skin colour shows up as a lighter
    oval sitting on top of the art. This inpaints instead: every row of the patch is a horizontal
    blend between the clean skin just outside it on the LEFT and on the RIGHT, which reproduces the
    vertical shading (row by row) and any horizontal gradient (across each row) for the cost of one
    interpolation. A mouth is wide and flat, so its left and right neighbours are exactly the clean
    skin to blend between.

    Computed ONCE for a clip: only the cavity drawn on top of it changes between frames.
    """
    import numpy as np
    from PIL import Image, ImageDraw, ImageFilter

    W, H = base.size
    pad = max(2, int(mw * 0.25))
    x0, x1 = int(round(cx - hw)), int(round(cx + hw))
    y0, y1 = int(round(cy + dy - hh)), int(round(cy + dy + hh))
    sx0, sx1 = max(0, x0 - pad), min(W, x1 + pad)
    sy0, sy1 = max(0, y0), min(H, y1)
    if sx1 - sx0 < 2 * pad + 2 or sy1 - sy0 < 2:
        return base.copy()
    strip = np.asarray(base.convert("RGB").crop((sx0, sy0, sx1, sy1)), dtype=np.float32)
    left = strip[:, :pad].mean(axis=1)                     # (h, 3) clean skin on each side
    right = strip[:, -pad:].mean(axis=1)
    inner = strip.shape[1] - 2 * pad
    if inner < 2:
        return base.copy()
    t = np.linspace(0.0, 1.0, inner, dtype=np.float32)[None, :, None]
    fill = left[:, None, :] * (1.0 - t) + right[:, None, :] * t
    patch = Image.fromarray(np.clip(fill, 0, 255).astype("uint8"), "RGB")
    mask = Image.new("L", patch.size, 0)
    ImageDraw.Draw(mask).ellipse([0, 0, patch.width - 1, patch.height - 1], fill=255)
    # ROTATE the mask with the face. An axis-aligned ellipse over a tilted mouth leaves the corners
    # of the drawn line sticking out — on a smile that rises to one side, that leftover is read as
    # "the mouth is not aligned", because the artist's mouth is still visible above the new one.
    if abs(angle) > 0.5:
        mask = mask.rotate(-angle, resample=Image.BICUBIC)
    # Just enough feather to avoid a stair-stepped edge; cel art has hard edges, so more than a
    # pixel or two of gradient reads as a smudge rather than as the drawing.
    mask = mask.filter(ImageFilter.GaussianBlur(max(0.6, mw * 0.02)))
    out = base.copy()
    out.paste(patch, (sx0 + pad, sy0), mask)
    return out


def _render_anime_frames(base, cx: float, cy: float, mw: float, angle: float, openness, width):
    """Yield frames for FLAT ART, by REDRAWING the mouth rather than warping the picture.

    The warp in _render_frames moves real pixels, which is exactly what a photograph gives it. Cel
    shading gives it none: the mouth is a hard ink stroke on a flat fill, and sliding the region
    below it down just duplicates that stroke and smears the chin — the "doesn't work on anime at
    all" report. Anime lip-sync has never worked that way either; it swaps a drawn mouth per frame.
    So the original mouth is painted out (see _mouth_erased) and an open one is drawn in its place,
    which is both cheaper and the way the medium actually does it.

    A closed frame is the untouched picture, so silence shows the artist's own mouth.
    """
    from PIL import Image, ImageDraw

    skin = _skin_tone(base, cx, cy, mw)
    ink = tuple(max(0, int(c * 0.22)) for c in skin)          # the art's own line-art darkness
    cavity = (58, 26, 34)
    tongue = (196, 104, 116)
    # The erase has to cover the widest cavity this clip will draw as well as the artist's own
    # mouth — a cover narrower than the cavity lets the drawn outline land on untouched art, which
    # is half of what the "ugly shadow around the mouth" was.
    max_hw = mw * (_ANIME_CAV_W + _ANIME_CAV_WMOD)
    # The erase spans exactly what the widest, most-open cavity will cover, and no more — every
    # pixel beyond that is art being flattened for nothing.
    # The erase spans exactly what the widest, most-open cavity will cover, and NO MORE. Growing it to
    # chase the last of the artist's smile line was tried and is strictly worse: every pixel beyond
    # the cavity is art being flattened, and a large flat patch reads as a smear far more loudly than
    # a bit of the original mouth peeking out at the corners — which, on a real face, is just what
    # the corners of a mouth do. It is ROTATED with the face, which costs nothing and is what keeps
    # a tilted mouth's corners inside it.
    e_top, e_bot = mw * _ANIME_CAV_TOP, mw * _ANIME_CAV_DROP
    erased = _mouth_erased(base, cx, cy, mw, max_hw * 1.08,
                           (e_bot - e_top) / 2 + mw * 0.04, (e_bot + e_top) / 2, angle)
    for a, wdt in zip(openness, width):
        a = float(a)
        if a < _OPEN_EPS:
            yield base.copy()
            continue
        hw = mw * (_ANIME_CAV_W + _ANIME_CAV_WMOD * float(wdt))   # bright vowels spread it wider
        top = mw * _ANIME_CAV_TOP
        bot = mw * _ANIME_CAV_DROP * a
        hh = max(1.0, (bot - top) / 2)
        off = (bot + top) / 2                                     # the cavity hangs BELOW the lip line
        r = int(math.ceil(max(hw, hh) * 1.6)) + 6
        n = r * 2
        patch = Image.new("RGBA", (n, n), (0, 0, 0, 0))
        d = ImageDraw.Draw(patch)
        # Draw the open mouth: a dark cavity with a tongue, outlined in the art's own ink weight.
        d.ellipse([r - hw, r + off - hh, r + hw, r + off + hh], fill=cavity + (255,),
                  outline=ink + (255,), width=max(1, int(mw * 0.05)))
        if hh > mw * 0.12:
            tw, th = hw * 0.66, hh * 0.42
            d.ellipse([r - tw, r + off + hh - th * 1.6, r + tw, r + off + hh - th * 0.1],
                      fill=tongue + (255,))
        # A LOWER LIP under the opening. Without it the erased region just ends in flat skin, which
        # is the other half of "a shadow below the lips" — a mouth with no lip beneath it.
        # Light and thin: it is a lip, and the cavity already has its own outline right above it.
        # A heavy stroke here just reads as a second dark line under the mouth.
        lip_y = r + off + hh
        d.arc([r - hw * 0.96, lip_y - hh * 0.55, r + hw * 0.96, lip_y + max(2.0, mw * 0.16)],
              start=12, end=168, fill=ink + (140,), width=max(1, int(mw * 0.032)))
        if abs(angle) > 0.5:
            patch = patch.rotate(-angle, resample=Image.BICUBIC)
        frame = erased.copy()
        frame.paste(patch, (int(round(cx)) - patch.width // 2,
                            int(round(cy)) - patch.height // 2), patch)
        yield frame


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


def detect_mouth(image_data: bytes) -> dict:
    """Where this picture's mouth appears to be, in NORMALISED coordinates.

    ``{found, x, y, w, angle, anime}`` — x/y are fractions of the image's width/height and w is the
    mouth width as a fraction of the image WIDTH, so the answer survives any resize the client or
    the renderer does to the picture. `found` is False when nothing was detected, and the caller is
    expected to let the user place it by hand: on flat art the detectors are unreliable enough that
    a guess presented as fact is worse than an honest "put it here".
    """
    # The Photo/Drawing default is worth answering even when the mouth is not — failing to find a
    # face does not stop the picture being a drawing, and that toggle decides which renderer runs.
    # Leaving it False here sent hand-placed drawings to the WARP, which is the smear.
    flat = False
    W = H = 1
    try:
        from PIL import Image, ImageOps
        with Image.open(io.BytesIO(image_data)) as im0:
            im0 = ImageOps.exif_transpose(im0)
            W, H = im0.size
            flat = _is_flat_art(im0)
    except Exception:
        pass

    g = _face_geometry(image_data)
    if g is None or W <= 1:
        return {"found": False, "x": 0.5, "y": 0.62, "w": 0.12, "angle": 0.0, "anime": flat}
    cx, cy, mw, angle, _chin, is_anime = g
    return {"found": True, "x": cx / max(1, W), "y": cy / max(1, H), "w": mw / max(1, W),
            "angle": angle, "anime": bool(is_anime)}


# Mouth-to-chin distance as a fraction of MOUTH WIDTH, for a hand-placed mouth (which carries no
# chin of its own). Measured on the landmark path, where both are real: 26.2/36.9 and 25.6/36.5 —
# 0.71 on both faces. Only the warp uses it; the redraw has no jaw.
_CHIN_FROM_MW = 0.71


def add_talk(image_data: bytes, audio_path: str, fps: int = TALK_FPS,
             keep_alpha: bool = False, mouth: Optional[dict] = None) -> tuple:
    """Animate the face in `image_data` speaking `audio_path`. Returns ``(bytes, content_type)``.

    `mouth` is an explicit, NORMALISED placement — ``{x, y, w, angle, anime}`` as produced by
    detect_mouth and then corrected by the user. When given it REPLACES detection entirely, which is
    the only thing that makes this reliable on art the models were never trained for: anime, 3D
    renders, mascots, a face in a crowd. `anime` also picks the renderer, because "redraw or warp"
    is a judgement about the artwork that the person looking at it can make and the detector cannot.

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
    if mouth:
        # A hand-placed mouth is normalised, so it is immune to the downscale above.
        cx = float(mouth.get("x", 0.5)) * im.width
        cy = float(mouth.get("y", 0.62)) * im.height
        mw = max(8.0, float(mouth.get("w", 0.12)) * im.width)
        try:
            angle = float(mouth.get("angle") or 0.0)
        except (TypeError, ValueError):
            angle = 0.0
        angle = angle if abs(angle) <= 45 else 0.0
        chin = mw * _CHIN_FROM_MW
        is_anime = bool(mouth.get("anime"))
    else:
        # Detection runs on flat RGB bytes: cv2 wants three channels, and a cut-out's transparent
        # region carries whatever RGB happened to be underneath, which is not a face.
        buf = io.BytesIO()
        im.convert("RGB").save(buf, format="JPEG", quality=92)
        image_data = buf.getvalue()
        # Distinguish "this node cannot see faces at all" from "there is no face in this picture".
        # The lean nostr-only image ships neither insightface nor opencv. Checked HERE and not
        # above, because a HAND-PLACED mouth needs no detector at all — the renderers are pure
        # Pillow + numpy — so that path must keep working on a node without one.
        import importlib.util
        if importlib.util.find_spec("cv2") is None:
            raise RuntimeError("face detection isn't installed on this node — place the mouth by hand")
        geom = _face_geometry(image_data)
        if geom is None:
            raise RuntimeError("no face found in that picture — place the mouth by hand instead")
        cx, cy, mw, angle, chin, is_anime = geom
    # A mouth a handful of pixels wide cannot be animated into anything but mush, and the
    # mask/ellipse maths degenerates below a few pixels.
    if mw < 12:
        raise RuntimeError("that mouth is too small to animate — make it wider, or use a bigger picture")

    # One envelope sample per video frame, so the clip's length IS the speech's length.
    openness, width = _audio_envelope(audio_path, fps)
    frames = (_render_anime_frames(im, cx, cy, mw, angle, openness, width) if is_anime
              else _render_frames(im, cx, cy, mw, chin, angle, openness, width))
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
