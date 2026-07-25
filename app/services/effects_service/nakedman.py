"""`nakedman` — a fat cartoon man dancing (with an exaggerated penis) over the input
image, rendered as an 8s looping MP4 set to an audio clip, then branded like the other
effect videos. Everything is drawn in Pillow per-frame (no shipped image asset for the
man); only the audio track is a bundled asset (assets/nakedman.mp3).

This mirrors the animated `fire` effect (per-frame PIL draw → frames_to_video) and the
audio gags (bundled mp3 muxed onto the clip). The man is procedurally drawn — simple
flesh-toned shapes/limbs — so it is a crude cartoon, not a photo of a real person, in
keeping with the app's existing crude effect library (dildo/cum/blood/blacked...).
"""
import math
import os

from ._common import (
    List, OutputFile, Path, Tuple, _NAKEDMAN_ANIM_FPS, _NAKEDMAN_ANIM_FRAMES,
    _NAKEDMAN_ANIM_LOOPS, _NAKEDMAN_AUDIO_CANDIDATES, _human_size, io, is_image, logger,
)

# --- palette (cartoon flesh) ---
_SKIN = (240, 194, 156)
_SKIN_DK = (196, 150, 116)
_GLANS = (232, 158, 150)
_OUTLINE = (58, 32, 18)
_HAIR = (70, 46, 30)


def _nakedman_audio_path() -> str:
    """First existing nakedman mp3 from the candidate list ("" if none)."""
    for p in _NAKEDMAN_AUDIO_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def _capsule(draw, p0, p1, w, fill, outline, ow=None):
    """Draw an outlined capsule (thick round-capped line) between two points."""
    ow = ow if ow is not None else max(int(w * 0.28), 3)
    x0, y0 = p0
    x1, y1 = p1
    r = w / 2.0
    ro = r + ow
    # outline pass (fat), then fill pass on top
    draw.line([x0, y0, x1, y1], fill=outline, width=int(ro * 2))
    for (cx, cy) in (p0, p1):
        draw.ellipse([cx - ro, cy - ro, cx + ro, cy + ro], fill=outline)
    draw.line([x0, y0, x1, y1], fill=fill, width=int(r * 2))
    for (cx, cy) in (p0, p1):
        draw.ellipse([cx - r, cy - r, cx + r, cy + r], fill=fill)


def _oval(draw, cx, cy, rx, ry, fill, outline, ow):
    draw.ellipse([cx - rx - ow, cy - ry - ow, cx + rx + ow, cy + ry + ow], fill=outline)
    draw.ellipse([cx - rx, cy - ry, cx + rx, cy + ry], fill=fill)


def _draw_dancing_man(overlay, cx, ground_y, M, phase):
    """Draw one frame of the fat dancing man (naked, huge penis) onto `overlay`
    (an RGBA layer) at the given dance `phase` (0..2π, wraps seamlessly)."""
    from PIL import ImageDraw
    d = ImageDraw.Draw(overlay)

    p = phase
    ow = max(int(M * 0.012), 2)               # outline thickness
    # --- dance motion (all periodic in `p` so the loop wraps) ---
    bob = math.sin(2 * p)                      # two bounces per loop
    sway = math.sin(p)                         # side-to-side hip sway
    y_off = -0.028 * M * (0.5 + 0.5 * bob)     # whole body bounces up on the beat
    sway_x = 0.055 * M * sway

    # feet planted (shuffle a little), body/hips sway + bob above them
    foot_dx = 0.14 * M
    lift_l = max(0.0, math.sin(p)) * 0.05 * M
    lift_r = max(0.0, -math.sin(p)) * 0.05 * M
    left_foot = (cx - foot_dx, ground_y - lift_l)
    right_foot = (cx + foot_dx, ground_y - lift_r)

    hip_y = ground_y - 0.24 * M + y_off
    hip_x = cx + sway_x
    belly_ry = 0.30 * M
    belly_rx = 0.29 * M
    belly_cy = hip_y - belly_ry * 0.62
    belly_cx = hip_x
    shoulder_y = belly_cy - belly_ry * 0.72
    head_r = 0.135 * M
    head_cy = shoulder_y - head_r * 0.95
    head_cx = hip_x + sway_x * 0.25

    leg_w = 0.11 * M
    arm_w = 0.075 * M

    # soft ground shadow (scales with the bounce)
    sh_rx = 0.30 * M * (1.0 + 0.05 * bob)
    d.ellipse([cx - sh_rx, ground_y + 0.01 * M - 0.03 * M,
               cx + sh_rx, ground_y + 0.05 * M], fill=(0, 0, 0, 70))

    # --- legs (behind belly) ---
    hipL = (hip_x - 0.12 * M, hip_y)
    hipR = (hip_x + 0.12 * M, hip_y)
    _capsule(d, hipL, left_foot, leg_w, _SKIN, _OUTLINE, ow)
    _capsule(d, hipR, right_foot, leg_w, _SKIN, _OUTLINE, ow)

    # --- big round belly/torso ---
    _oval(d, belly_cx, belly_cy, belly_rx, belly_ry, _SKIN, _OUTLINE, ow)
    # belly button + nipples
    d.ellipse([belly_cx - ow, belly_cy + belly_ry * 0.35 - ow,
               belly_cx + ow, belly_cy + belly_ry * 0.35 + ow], fill=_SKIN_DK)
    for nx in (-belly_rx * 0.42, belly_rx * 0.42):
        d.ellipse([belly_cx + nx - ow, shoulder_y + belly_ry * 0.18 - ow,
                   belly_cx + nx + ow, shoulder_y + belly_ry * 0.18 + ow], fill=_SKIN_DK)

    # --- arms (swing opposite each other) ---
    sh_l = (belly_cx - belly_rx * 0.85, shoulder_y + belly_ry * 0.05)
    sh_r = (belly_cx + belly_rx * 0.85, shoulder_y + belly_ry * 0.05)
    swL = math.sin(p)
    swR = -swL
    hand_l = (sh_l[0] - 0.20 * M, sh_l[1] - 0.20 * M * swL)
    hand_r = (sh_r[0] + 0.20 * M, sh_r[1] - 0.20 * M * swR)
    _capsule(d, sh_l, hand_l, arm_w, _SKIN, _OUTLINE, ow)
    _capsule(d, sh_r, hand_r, arm_w, _SKIN, _OUTLINE, ow)

    # --- head + simple face ---
    _oval(d, head_cx, head_cy, head_r, head_r, _SKIN, _OUTLINE, ow)
    # little hair cap
    d.pieslice([head_cx - head_r, head_cy - head_r,
                head_cx + head_r, head_cy + head_r * 0.7],
               180, 360, fill=_HAIR)
    eye_dx = head_r * 0.38
    eye_y = head_cy - head_r * 0.05
    er = max(head_r * 0.10, ow)
    for ex in (-eye_dx, eye_dx):
        d.ellipse([head_cx + ex - er, eye_y - er, head_cx + ex + er, eye_y + er], fill=_OUTLINE)
    # open grin
    d.arc([head_cx - head_r * 0.5, head_cy + head_r * 0.05,
           head_cx + head_r * 0.5, head_cy + head_r * 0.6],
          10, 170, fill=_OUTLINE, width=max(ow, 2))

    # --- penis + balls (huge, swings with the sway; drawn LAST so the full shaft
    # hangs in FRONT of the belly/legs — the exaggerated cartoon anatomy) ---
    crotch = (hip_x, belly_cy + belly_ry * 0.72)
    scr_rx, scr_ry = 0.11 * M, 0.09 * M
    _oval(d, crotch[0], crotch[1] + 0.06 * M, scr_rx, scr_ry, _SKIN_DK, _OUTLINE, ow)
    swing = 0.5 * sway                          # pendulum angle (radians) from vertical
    p_len = 0.40 * M
    p_w = 0.11 * M
    tip = (crotch[0] + math.sin(swing) * p_len,
           crotch[1] + math.cos(swing) * p_len)
    _capsule(d, crotch, tip, p_w, _SKIN, _OUTLINE, ow)
    # glans (slightly pinker bulb at the tip)
    gr = p_w * 0.74
    _oval(d, tip[0], tip[1], gr, gr, _GLANS, _OUTLINE, ow)


def add_nakedman_animated(data: bytes) -> bytes:
    """Render the input image with a fat cartoon man dancing over it as an 8s MP4
    set to the nakedman audio clip. Returns MP4 bytes (video + audio, no outro — the
    command layer appends the branded end-card). Raises on unrecoverable failure."""
    from PIL import Image, ImageOps
    from app.services.media_service import frames_to_video, mux_audio_loop
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except Exception:
        pass

    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode in ("RGBA", "LA", "P"):
            bg = Image.new("RGB", img.size, (255, 255, 255))
            rgba = img.convert("RGBA")
            bg.paste(rgba, mask=rgba.split()[-1])
            img = bg
        elif img.mode != "RGB":
            img = img.convert("RGB")

        # Cap working resolution so 160 frames stay cheap to draw/encode.
        max_edge = 1280
        if max(img.size) > max_edge:
            r = max_edge / float(max(img.size))
            img = img.resize((max(int(img.width * r), 2), max(int(img.height * r), 2)))

        base = img.convert("RGBA")
        W, H = base.size

        # Man geometry: prominent but framed (never wider than the image).
        M = min(H * 0.62, W * 0.52)
        cx = W * 0.5
        ground_y = H * 0.93

        frames = []
        for fi in range(_NAKEDMAN_ANIM_FRAMES):
            phase = math.tau * (fi / _NAKEDMAN_ANIM_FRAMES)
            overlay = Image.new("RGBA", (W, H), (0, 0, 0, 0))
            _draw_dancing_man(overlay, cx, ground_y, M, phase)
            frame = base.copy()
            frame.alpha_composite(overlay)
            frames.append(frame.convert("RGB"))

    silent = frames_to_video(frames, fps=_NAKEDMAN_ANIM_FPS, loops=_NAKEDMAN_ANIM_LOOPS)
    audio = _nakedman_audio_path()
    if not audio:
        logger.warning("nakedman audio (assets/nakedman.mp3) missing — returning silent clip")
        return silent
    return mux_audio_loop(silent, audio, "nakedman.mp4")


def nakedman_attachments(
    attachments: List[Tuple[str, bytes, str]],
) -> Tuple[List[OutputFile], str]:
    """Overlay a dancing fat cartoon man on the first image attachment → 8s MP4.
    Mirrors fire_attachments / whoabuddy_attachments (video output)."""
    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_nakedman_animated(data)
        out: OutputFile = {
            "filename": f"{stem}_nakedman.mp4",
            "data": result,
            "content_type": "video/mp4",
        }
        summary = f"## 🍆 Naked man\n\n🍆 {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"nakedman failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"
