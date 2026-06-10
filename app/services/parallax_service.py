"""Depth-based 2.5D parallax — make a STILL photo move.

Estimates a depth map with Depth-Anything V2 (small ViT-S ONNX), then animates a
subtle looping camera move so near things shift more than far things — the photo
gains real 3-D parallax (a "3D photo" / live-wallpaper feel) rather than an overlay.

The motion is a seamless loop: the virtual camera traces a closed elliptical orbit
(sin/cos over one cycle), so the last frame meets the first. A small base zoom keeps
the moving edges off-screen so no border smear shows.

Output is a silent H.264 MP4 (reuses media_service's ffmpeg/HW-accel autodetect).
The ONNX session and model are loaded lazily and cached.
"""

import io
import logging
import os
import shutil
import subprocess
import tempfile

import numpy as np

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_MODEL_CANDIDATES = [
    os.environ.get("DEPTH_MODEL_PATH", ""),
    os.path.join(_REPO_ROOT, "assets", "depth_anything_v2_vits.onnx"),
    "/var/lib/posterchanai/assets/depth_anything_v2_vits.onnx",
]

# ImageNet normalisation (Depth-Anything V2 preprocessing).
_MEAN = np.array([0.485, 0.456, 0.406], dtype=np.float32)
_STD = np.array([0.229, 0.224, 0.225], dtype=np.float32)
_INFER_SIZE = 518          # model input is resized to this (a multiple of 14)

_SESSION = None
_SESSION_TRIED = False


def _model_path() -> str:
    for p in _MODEL_CANDIDATES:
        if p and os.path.exists(p):
            return p
    return ""


def _session():
    """Lazily build a cached ONNX Runtime session for the depth model (CPU)."""
    global _SESSION, _SESSION_TRIED
    if _SESSION_TRIED:
        return _SESSION
    _SESSION_TRIED = True
    path = _model_path()
    if not path:
        logger.warning("parallax: depth model not found (%s)", _MODEL_CANDIDATES)
        return None
    try:
        import onnxruntime as ort
        so = ort.SessionOptions()
        so.intra_op_num_threads = max(1, (os.cpu_count() or 4) // 2)
        _SESSION = ort.InferenceSession(path, sess_options=so, providers=["CPUExecutionProvider"])
        logger.info("parallax: depth model loaded from %s", path)
    except Exception as e:
        logger.warning("parallax: failed to load depth model: %s", e)
        _SESSION = None
    return _SESSION


def estimate_depth(rgb: np.ndarray) -> np.ndarray:
    """Return a float32 depth map in [0,1] (1=near, 0=far) at the image's H×W."""
    import cv2
    h, w = rgb.shape[:2]
    inp = cv2.resize(rgb, (_INFER_SIZE, _INFER_SIZE), interpolation=cv2.INTER_AREA)
    x = inp.astype(np.float32) / 255.0
    x = (x - _MEAN) / _STD
    x = np.transpose(x, (2, 0, 1))[None].astype(np.float32)  # [1,3,H,W]
    sess = _session()
    out = sess.run(None, {sess.get_inputs()[0].name: x})[0]   # [1,Hd,Wd]
    depth = out[0]
    depth = cv2.resize(depth, (w, h), interpolation=cv2.INTER_CUBIC)
    dmin, dmax = float(depth.min()), float(depth.max())
    if dmax - dmin < 1e-6:
        return np.full((h, w), 0.5, dtype=np.float32)
    return ((depth - dmin) / (dmax - dmin)).astype(np.float32)  # near=1, far=0


def render_parallax(rgb: np.ndarray, depth: np.ndarray, frames: int = 48,
                    amplitude: float = 0.035, zoom: float = 1.06) -> list:
    """Render `frames` RGB numpy frames of a looping parallax orbit.

    `amplitude` is the camera sway as a fraction of the image width; `zoom` is the
    base crop-in that hides the moving edges. Near pixels (depth→1) shift most.
    Uses backward sampling (cv2.remap) so there are no disocclusion holes — depth
    edges smear slightly, which is invisible at these small amplitudes.
    """
    import cv2
    import math
    h, w = rgb.shape[:2]
    # Anchor the parallax at the FAR plane (low depth percentile) and normalise to
    # [0,1], so the NEAR subject is what swings while the background stays roughly put.
    # (Mean-centring made a subject that fills the frame sit still — only the
    # background moved; this makes the character itself the thing that moves.)
    far = float(np.percentile(depth, 20))
    d = np.clip(depth - far, 0.0, None)
    dmax = float(d.max())
    if dmax > 1e-6:
        d = d / dmax
    base_x, base_y = np.meshgrid(np.arange(w, dtype=np.float32),
                                 np.arange(h, dtype=np.float32))
    ax = amplitude * w
    ay = amplitude * w * 0.6                     # gentler vertical than horizontal
    # Base zoom: sample from a window scaled by 1/zoom about the centre.
    cx, cy = w / 2.0, h / 2.0
    inv = 1.0 / zoom
    out_frames = []
    for fi in range(frames):
        t = 2.0 * math.pi * fi / frames          # one full cycle → seamless loop
        ox = ax * math.sin(t)
        oy = ay * math.cos(t)
        map_x = cx + (base_x - cx) * inv + ox * d
        map_y = cy + (base_y - cy) * inv + oy * d
        warped = cv2.remap(rgb, map_x, map_y, interpolation=cv2.INTER_LINEAR,
                           borderMode=cv2.BORDER_REFLECT)
        out_frames.append(warped)
    return out_frames


# ---------------------------------------------------------------------------
# Encoding (reuses media_service's ffmpeg resolution + HW-accel encoder list)
# ---------------------------------------------------------------------------

def _encode(frames: list, fps: int = 24, loops: int = 2) -> bytes:
    """Encode RGB numpy frames to a silent H.264 MP4 (looped `loops` times)."""
    import cv2
    from app.services import media_service as ms
    ffmpeg = ms.resolve_ffmpeg()
    if not ms.ffmpeg_available():
        raise RuntimeError("ffmpeg is not installed on the server")
    tmp = tempfile.mkdtemp(prefix="parallax_")
    pattern = os.path.join(tmp, "f_%05d.png")
    out_path = os.path.join(tmp, "out.mp4")
    try:
        idx = 0
        for _ in range(max(int(loops), 1)):
            for fr in frames:
                cv2.imwrite(pattern % idx, cv2.cvtColor(fr, cv2.COLOR_RGB2BGR))
                idx += 1
        scale = "scale=trunc(iw/2)*2:trunc(ih/2)*2"
        candidates = ms._video_encoder_candidates(ffmpeg)
        last_err = ""
        for enc in candidates:
            pre, vf = [], scale
            if enc == "h264_nvenc":
                venc = ["-c:v", "h264_nvenc", "-preset", "p5", "-cq", str(ms.VIDEO_CRF)]
            elif enc == "h264_vaapi":
                pre = ["-vaapi_device", ms._render_node()]
                vf = scale + ",format=nv12,hwupload"
                venc = ["-c:v", "h264_vaapi", "-qp", str(ms.VIDEO_CRF)]
            else:
                venc = ["-c:v", "libx264", "-preset", ms.VIDEO_PRESET, "-crf", str(ms.VIDEO_CRF)]
            cmd = [ffmpeg] + pre + ["-framerate", str(fps), "-i", pattern, "-vf", vf] + venc + \
                  ["-pix_fmt", "yuv420p", "-r", str(fps), "-an", "-movflags", "+faststart", "-y", out_path]
            r = subprocess.run(cmd, capture_output=True, timeout=3600, text=True)
            if r.returncode == 0 and os.path.exists(out_path) and os.path.getsize(out_path) > 0:
                with open(out_path, "rb") as f:
                    return f.read()
            last_err = (r.stderr or "")[-300:]
            logger.warning("parallax encoder %s failed: %s", enc, last_err)
        raise RuntimeError(f"parallax encode failed (tried {candidates}): {last_err}")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


_MAXDIM = 1280   # cap working long edge so depth + warp + encode stay cheap


def add_parallax(data: bytes, amplitude: float = 0.035, zoom: float = 1.06,
                 frames: int = 48, fps: int = 24, loops: int = 2) -> bytes:
    """Turn a still image into a looping 2.5D-parallax MP4. Returns MP4 bytes."""
    import cv2
    from PIL import Image, ImageOps
    try:
        from pillow_heif import register_heif_opener
        register_heif_opener()
    except Exception:
        pass

    if _session() is None:
        raise RuntimeError("depth model unavailable")

    with Image.open(io.BytesIO(data)) as img:
        img = ImageOps.exif_transpose(img)
        if img.mode != "RGB":
            img = img.convert("RGB")
        if max(img.size) > _MAXDIM:
            img.thumbnail((_MAXDIM, _MAXDIM), Image.LANCZOS)
        rgb = np.asarray(img)

    depth = estimate_depth(rgb)
    # Light smoothing so depth edges don't tear the warp.
    depth = cv2.GaussianBlur(depth, (0, 0), sigmaX=max(rgb.shape[1] * 0.004, 1.0))
    fr = render_parallax(rgb, depth, frames=frames, amplitude=amplitude, zoom=zoom)
    return _encode(fr, fps=fps, loops=loops)


# Intensity presets (amplitude as a fraction of width, + matching base zoom so the
# stronger sway still hides its edges). Picked by the `alive` command's arg.
_INTENSITY = {
    "subtle": (0.022, 1.04),
    "normal": (0.035, 1.06),
    "strong": (0.055, 1.09),
}


def alive_attachments(attachments, arg: str = ""):
    """Make the first image attachment come alive (looping 2.5D parallax). `arg`
    picks intensity (subtle/normal/strong); default subtle (it reads best). Returns
    (output_files, summary) like the effects_service processors."""
    from pathlib import Path
    from app.services.media_service import is_image, _human_size

    images = [(fn, d, ct) for fn, d, ct in (attachments or []) if is_image(fn, ct)]
    if not images:
        return [], "No image — attach an image first."
    key = (arg or "").strip().split()[0].lower() if (arg or "").strip() else "subtle"
    amp, zoom = _INTENSITY.get(key, _INTENSITY["subtle"])
    label = key if key in _INTENSITY else "subtle"
    filename, data, _ = images[0]
    stem = Path(filename).stem or "image"
    try:
        result = add_parallax(data, amplitude=amp, zoom=zoom)
        out = {"filename": f"{stem}_alive.mp4", "data": result, "content_type": "video/mp4"}
        summary = f"## ✨ Alive ({label})\n\n✨ {filename}: {_human_size(len(result))}"
        return [out], summary
    except Exception as e:
        logger.error(f"parallax/alive failed for {filename}: {e}", exc_info=True)
        return [], f"❌ {filename}: {e}"
