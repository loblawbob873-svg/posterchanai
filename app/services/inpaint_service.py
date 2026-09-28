"""Magic Eraser — fill a brushed-over region so it matches what surrounds it (object removal).

The Meme Builder's ✂ "Erase parts" makes a region TRANSPARENT. This is its sibling: the region is
painted back in from its surroundings, so a photobomber, a watermark or a stray cable is simply gone
and the picture looks as if it was never there.

THREE FILLERS, BEST FIRST, AND THE LAST ONE ALWAYS WORKS:

  lama       LaMa (big-lama, ONNX export) on onnxruntime's CPU provider. Real texture synthesis: it
             continues stripes, brick and foliage through the hole. Measured on this repo's
             synthetic stripe test (100x100 hole in 16px stripes, 512x512): mean abs error 17.7 vs
             OpenCV Telea 40.1 / NS 45.4. ~1.8s per fill on server1's CPU, 3.9s to load once.
             The weights (~208 MB) are fetched ON DEMAND the first time somebody uses the tool, in
             a background thread, and that request is answered by the next filler — so the first
             use is never a wait on a download and never a dead button.
  opencv     cv2.inpaint (Navier-Stokes). Smooth, not textured: excellent on skies/gradients/flat
             walls (exact on a linear gradient), a smear on busy texture.
  diffusion  pure numpy: a push-pull pyramid plus relaxation. The floor for a build with neither
             onnxruntime nor OpenCV (the nostr-only image ships numpy and Pillow only).

GPU: none. Every filler runs on the CPU, deliberately — one image at a 512px working size is a
second or two, so it does NOT take GPUResourceLock (which would queue it behind a minute of video
generation for no gain) and it behaves the same on CUDA, Arc/XPU, ROCm and GPU-less nodes. The
endpoint runs it inside the meme render slot like every other Meme Builder render.

THE HOLE IS FILLED FROM A CROP, NOT THE WHOLE PICTURE. The model's input is a fixed 512x512, and the
classical fillers' cost grows with the picture; cropping around the brushed region (plus context)
keeps both bounded on a 12 MP phone photo, and gives LaMa the resolution where it matters.

What is guaranteed about the output (and pinned by tests/test_magic_eraser.py): same size as the
input, and every pixel farther than the edge feather from the brushed region is byte-identical —
alpha included, so a background-removed cut-out stays cut out.
"""

from __future__ import annotations

import io
import logging
import os
import threading
import time

import numpy as np

logger = logging.getLogger(__name__)

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

MODEL_FILE = "lama_fp32.onnx"
MODEL_URL = "https://huggingface.co/Carve/LaMa-ONNX/resolve/main/lama_fp32.onnx"
MODEL_SHA256 = "1faef5301d78db7dda502fe59966957ec4b79dd64e16f03ed96913c7a4eb68d6"
MODEL_SIZE = 208044816
LAMA_SIZE = 512                      # the export's fixed input size

MAX_PIXELS = 50_000_000              # decompression bound for the layer image (a 12 MP photo is fine)
CLASSIC_EDGE = 768                   # classical fillers work on a crop no longer than this

_DATA_ASSETS = "/var/lib/posterchanai/assets"   # the Docker data volume (where depth/u2net live)


class NothingToErase(ValueError):
    """The mask marks no pixels — the user pressed Apply without brushing anything."""


# ---------------------------------------------------------------------------------------------
# The model: where it lives, whether this node may fetch it, and fetching it once in background.
# ---------------------------------------------------------------------------------------------

def _candidates() -> list[str]:
    return [p for p in (os.environ.get("INPAINT_MODEL_PATH", ""),
                        os.path.join(_DATA_ASSETS, MODEL_FILE),
                        os.path.join(_REPO_ROOT, "assets", MODEL_FILE)) if p]


def model_path() -> str:
    for p in _candidates():
        try:
            if os.path.getsize(p) == MODEL_SIZE:
                return p
        except OSError:
            continue
    return ""


def _download_dest() -> str:
    env = os.environ.get("INPAINT_MODEL_PATH", "")
    if env:
        return env
    if os.path.isdir(_DATA_ASSETS) and os.access(_DATA_ASSETS, os.W_OK):
        return os.path.join(_DATA_ASSETS, MODEL_FILE)
    return os.path.join(_REPO_ROOT, "assets", MODEL_FILE)


def _have_onnxruntime() -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec("onnxruntime") is not None
    except Exception:
        return False


def model_blocked() -> str:
    """Why this node will not use/fetch the model, or "" when it may.

    The same two signals as every other weight download (model_download_service._no_ai_build:
    PC_ACCEL=nostr, POSTERCHANAI_NOSTR_ONLY), plus the obvious one — no onnxruntime, nothing could
    load it — and an operator switch, POSTERCHANAI_INPAINT_MODEL=0, for a node that wants the
    classical filler only."""
    if (os.getenv("POSTERCHANAI_INPAINT_MODEL", "1") or "").strip().lower() in ("0", "false", "no", "off"):
        return "the inpainting model is switched off on this node (POSTERCHANAI_INPAINT_MODEL=0)"
    try:
        from app.services.model_download_service import _no_ai_build
        why = _no_ai_build()
        if why:
            return why
    except Exception:
        pass
    if not _have_onnxruntime():
        return "onnxruntime is not installed on this build"
    return ""


_dl_lock = threading.Lock()
_dl_state = {"running": False, "failed_at": 0.0, "error": ""}
_DL_RETRY_S = 3600.0


def model_state() -> str:
    """ready | downloading | unavailable — what the client is told, so the first (classical) fill
    can say that a better one is on its way instead of looking like the best this can do."""
    if model_path():
        return "ready"
    if model_blocked():
        return "unavailable"
    return "downloading" if _dl_state["running"] else "unavailable"


def _download() -> None:
    import hashlib

    import httpx
    dest = _download_dest()
    tmp = dest + ".part"
    try:
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        h = hashlib.sha256()
        n = 0
        t0 = time.monotonic()
        logger.info("[magic-eraser] downloading the inpainting model (%d MB) -> %s",
                    MODEL_SIZE // (1024 * 1024), dest)
        with httpx.stream("GET", MODEL_URL, follow_redirects=True,
                          timeout=httpx.Timeout(60.0, connect=15.0)) as r:
            r.raise_for_status()
            with open(tmp, "wb") as fh:
                for chunk in r.iter_bytes(1 << 20):
                    fh.write(chunk)
                    h.update(chunk)
                    n += len(chunk)
                    if n > MODEL_SIZE:
                        raise RuntimeError("model download is larger than expected")
        if n != MODEL_SIZE or h.hexdigest() != MODEL_SHA256:
            raise RuntimeError(f"model download did not verify (size {n}, sha {h.hexdigest()[:12]})")
        os.replace(tmp, dest)
        logger.info("[magic-eraser] inpainting model ready in %.1fs: %s", time.monotonic() - t0, dest)
        _dl_state["error"] = ""
    except Exception as e:
        _dl_state["failed_at"] = time.monotonic()
        _dl_state["error"] = str(e)
        logger.warning("[magic-eraser] model download failed (%s) — the classical filler keeps "
                       "working; retrying in %ds", e, int(_DL_RETRY_S))
        try:
            os.unlink(tmp)
        except OSError:
            pass
    finally:
        _dl_state["running"] = False


def ensure_model() -> str:
    """Start the one-time background download if this node may and does not have the model yet.
    Never blocks. Returns model_state() as it stands after the call."""
    if model_path() or model_blocked():
        return model_state()
    with _dl_lock:
        if _dl_state["running"]:
            return "downloading"
        if _dl_state["failed_at"] and time.monotonic() - _dl_state["failed_at"] < _DL_RETRY_S:
            return "unavailable"
        _dl_state["running"] = True
    threading.Thread(target=_download, name="magic-eraser-model", daemon=True).start()
    return "downloading"


_sess = None
_sess_path = ""
_sess_lock = threading.Lock()


def _session():
    """The cached CPU session, or None. CPU ONLY on purpose — see the module docstring."""
    global _sess, _sess_path
    path = model_path()
    if not path or model_blocked():
        return None
    with _sess_lock:
        if _sess is not None and _sess_path == path:
            return _sess
        try:
            import onnxruntime as ort
            so = ort.SessionOptions()
            so.intra_op_num_threads = max(1, (os.cpu_count() or 4) // 2)
            t0 = time.monotonic()
            _sess = ort.InferenceSession(path, sess_options=so, providers=["CPUExecutionProvider"])
            _sess_path = path
            logger.info("[magic-eraser] model loaded in %.1fs", time.monotonic() - t0)
        except Exception as e:
            logger.warning("[magic-eraser] could not load %s (%s) — using the classical filler", path, e)
            _sess = None
    return _sess


# ---------------------------------------------------------------------------------------------
# The fillers. Each takes an HxWx3 uint8 crop and an HxW bool hole and returns the filled crop.
# ---------------------------------------------------------------------------------------------

def _fill_lama(rgb: np.ndarray, hole: np.ndarray) -> np.ndarray | None:
    sess = _session()
    if sess is None:
        return None
    from PIL import Image
    h, w = hole.shape
    x = np.asarray(Image.fromarray(rgb).resize((LAMA_SIZE, LAMA_SIZE), Image.BICUBIC), np.float32)
    m = np.asarray(Image.fromarray((hole * 255).astype(np.uint8)).resize((LAMA_SIZE, LAMA_SIZE),
                                                                         Image.BILINEAR)) > 0
    x = x.transpose(2, 0, 1)[None] / 255.0
    mk = m.astype(np.float32)[None, None]
    out = sess.run(None, {"image": x.astype(np.float32), "mask": mk})[0][0].transpose(1, 2, 0)
    # This export answers in 0..255; a 0..1 export would be all but black — normalise either way.
    if float(out.max()) <= 1.5:
        out = out * 255.0
    out = np.clip(out, 0, 255).astype(np.uint8)
    if (h, w) != (LAMA_SIZE, LAMA_SIZE):
        out = np.asarray(Image.fromarray(out).resize((w, h), Image.BICUBIC))
    return out


def _fill_opencv(img: np.ndarray, hole: np.ndarray) -> np.ndarray | None:
    try:
        import cv2
    except Exception:
        return None
    radius = max(3, int(round(max(hole.shape) / 100)))
    return cv2.inpaint(np.ascontiguousarray(img), hole.astype(np.uint8), radius, cv2.INPAINT_NS)


def _fill_diffusion(img: np.ndarray, hole: np.ndarray) -> np.ndarray:
    """Pure numpy: push-pull pyramid (a coarse-to-fine average of the known pixels), then Jacobi
    relaxation inside the hole so the fill meets its border smoothly."""
    squeeze = img.ndim == 2
    v = img.astype(np.float32)
    if squeeze:
        v = v[..., None]
    known = ~hole

    def pushpull(vals, k):
        h, w = k.shape
        if k.all():
            return vals
        if not k.any():
            return vals          # caller guarantees at least one known pixel at the top level
        if h <= 2 or w <= 2:
            mean = vals[k].mean(0)
            out = vals.copy()
            out[~k] = mean
            return out
        ph, pw = h + (h & 1), w + (w & 1)
        pv = np.zeros((ph, pw, vals.shape[2]), np.float32)
        pk = np.zeros((ph, pw), np.float32)
        pv[:h, :w] = vals * k[..., None]
        pk[:h, :w] = k
        sv = pv.reshape(ph // 2, 2, pw // 2, 2, -1).sum((1, 3))
        sk = pk.reshape(ph // 2, 2, pw // 2, 2).sum((1, 3))
        ck = sk > 0
        cv = np.where(ck[..., None], sv / np.maximum(sk, 1e-6)[..., None], 0)
        coarse = pushpull(cv, ck)
        up = np.repeat(np.repeat(coarse, 2, 0), 2, 1)[:h, :w]
        return np.where(k[..., None], vals, up)

    out = pushpull(v, known)
    # Relaxation: each hole pixel becomes the mean of its 4 neighbours. Bounded — the pyramid has
    # already placed every value near its answer, this only removes the block structure.
    for _ in range(80):
        p = np.pad(out, ((1, 1), (1, 1), (0, 0)), mode="edge")
        avg = (p[:-2, 1:-1] + p[2:, 1:-1] + p[1:-1, :-2] + p[1:-1, 2:]) * 0.25
        out = np.where(hole[..., None], avg, out)
    out = np.clip(out + 0.5, 0, 255).astype(np.uint8)
    return out[..., 0] if squeeze else out


def _fill_classic(img: np.ndarray, hole: np.ndarray, allow_opencv: bool = True) -> tuple[np.ndarray, str]:
    if allow_opencv:
        r = _fill_opencv(img, hole)
        if r is not None:
            return r, "opencv"
    return _fill_diffusion(img, hole), "diffusion"


# ---------------------------------------------------------------------------------------------
# Geometry helpers (Pillow-only, so they work on every build).
# ---------------------------------------------------------------------------------------------

def _dilate(mask: np.ndarray, r: int) -> np.ndarray:
    if r <= 0:
        return mask
    try:
        import cv2
        k = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (2 * r + 1, 2 * r + 1))
        return cv2.dilate(mask.astype(np.uint8), k) > 0
    except ImportError:
        pass
    from PIL import Image, ImageFilter
    im = Image.fromarray((mask * 255).astype(np.uint8))
    # MaxFilter cost grows with size², so a large radius is done as several small passes.
    left = r
    while left > 0:
        step = min(left, 3)
        im = im.filter(ImageFilter.MaxFilter(2 * step + 1))
        left -= step
    return np.asarray(im) > 127


def _feather(mask: np.ndarray, r: float) -> np.ndarray:
    from PIL import Image, ImageFilter
    im = Image.fromarray((mask * 255).astype(np.uint8)).filter(ImageFilter.GaussianBlur(max(0.5, r)))
    return np.asarray(im, np.float32) / 255.0


def _resize(arr: np.ndarray, w: int, h: int, resample=None) -> np.ndarray:
    from PIL import Image
    if arr.shape[1] == w and arr.shape[0] == h:
        return arr
    return np.asarray(Image.fromarray(arr).resize((w, h), resample if resample is not None else Image.BICUBIC))


def read_mask(mask_bytes: bytes, w: int, h: int) -> np.ndarray:
    """The brushed region as an HxW bool array at the image's size. WHITE = erase: a pixel counts
    when it is both opaque and light, so both a white-on-transparent canvas export and a
    white-on-black mask mean the same thing."""
    from PIL import Image
    try:
        m = Image.open(io.BytesIO(mask_bytes))
        m.load()
    except Exception:
        raise ValueError("the mask is not a readable image")
    if m.width * m.height > MAX_PIXELS:
        raise ValueError("the mask is too large")
    m = m.convert("RGBA")
    if m.size != (w, h):
        m = m.resize((w, h), Image.BILINEAR)
    a = np.asarray(m, np.uint16)
    lum = (a[..., 0] * 3 + a[..., 1] * 6 + a[..., 2]) // 10
    return (a[..., 3] > 127) & (lum > 127)


def load_image(image_bytes: bytes):
    from PIL import Image, ImageOps
    Image.MAX_IMAGE_PIXELS = max(Image.MAX_IMAGE_PIXELS or 0, MAX_PIXELS)
    try:
        im = Image.open(io.BytesIO(image_bytes))
        if im.width * im.height > MAX_PIXELS:
            raise ValueError("the picture is too large to fill (50 megapixel limit)")
        im.load()
    except ValueError:
        raise
    except Exception:
        raise ValueError("the layer is not a readable picture")
    # The browser shows the picture EXIF-rotated, and the mask was painted over what it showed.
    im = ImageOps.exif_transpose(im)
    return np.asarray(im.convert("RGBA")).copy()


# ---------------------------------------------------------------------------------------------
# The whole operation.
# ---------------------------------------------------------------------------------------------

def inpaint(rgba: np.ndarray, hole: np.ndarray, *, method: str = "auto") -> tuple[np.ndarray, str]:
    """Fill `hole` in an HxWx4 uint8 picture. Returns (new picture, filler used).

    method: auto (model if present, else classical) | lama | opencv | diffusion.
    """
    h, w = hole.shape
    if not hole.any():
        raise NothingToErase("brush over what you want removed first")
    if hole.all():
        raise NothingToErase("the whole picture is marked — leave some of it to fill from")

    # Grow the brush a touch: a stroke that stops ON an object's anti-aliased edge leaves a halo
    # of its colour, and every filler would then faithfully continue the halo into the hole.
    grow = max(2, int(round(max(h, w) / 256)))

    ys, xs = np.nonzero(hole)
    y0, y1, x0, x1 = int(ys.min()), int(ys.max()) + 1, int(xs.min()), int(xs.max()) + 1
    # Context around the hole: as much again as the hole is big, and at least 48px. Squared up
    # where the picture allows, since the model's input is square.
    side = max(y1 - y0, x1 - x0)
    pad = max(48, side) + grow * 2
    cy, cx = (y0 + y1) / 2.0, (x0 + x1) / 2.0
    half = side / 2.0 + pad
    cy0, cy1 = max(0, int(cy - half)), min(h, int(np.ceil(cy + half)))
    cx0, cx1 = max(0, int(cx - half)), min(w, int(np.ceil(cx + half)))

    crop = rgba[cy0:cy1, cx0:cx1]
    chole = _dilate(hole[cy0:cy1, cx0:cx1], grow)
    if chole.all():
        chole = hole[cy0:cy1, cx0:cx1]
    ch, cw = chole.shape
    rgb = np.ascontiguousarray(crop[..., :3])
    alpha = np.ascontiguousarray(crop[..., 3])

    used = ""
    filled = None
    if method in ("auto", "lama"):
        try:
            filled = _fill_lama(rgb, chole)
        except Exception as e:
            logger.warning("[magic-eraser] model fill failed (%s) — using the classical filler", e)
            filled = None
        if filled is not None:
            used = "lama"
        elif method == "lama":
            raise RuntimeError("the inpainting model is not available on this node")
    if filled is None:
        # Work at a bounded size — cost of both classical fillers grows with the crop.
        k = min(1.0, CLASSIC_EDGE / max(ch, cw))
        sw, sh = max(2, int(round(cw * k))), max(2, int(round(ch * k)))
        from PIL import Image
        s_rgb = _resize(rgb, sw, sh)
        s_hole = _resize((chole * 255).astype(np.uint8), sw, sh, Image.BILINEAR) > 0
        s_filled, used = _fill_classic(s_rgb, s_hole, allow_opencv=(method != "diffusion"))
        filled = _resize(s_filled, cw, ch)

    # Alpha inside the hole: continue the surrounding transparency (a cut-out stays a cut-out).
    if (alpha < 255).any():
        a_fill, _ = _fill_classic(alpha, chole, allow_opencv=(used != "diffusion"))
    else:
        a_fill = alpha

    # Blend: 1 inside the brushed region, easing to 0 across the grown ring, exactly 0 beyond it.
    wgt = _feather(chole, grow / 2.0)
    wgt[hole[cy0:cy1, cx0:cx1]] = 1.0
    wgt[~chole] = 0.0
    out_rgb = (filled.astype(np.float32) * wgt[..., None]
               + rgb.astype(np.float32) * (1.0 - wgt[..., None]))
    out = rgba.copy()
    out[cy0:cy1, cx0:cx1, :3] = np.clip(out_rgb + 0.5, 0, 255).astype(np.uint8)
    # Alpha changes ONLY where the user brushed — nowhere else, not even in the feather ring.
    user = hole[cy0:cy1, cx0:cx1]
    out[cy0:cy1, cx0:cx1, 3] = np.where(user, a_fill, alpha)
    return out, used


def magic_erase(image_bytes: bytes, mask_bytes: bytes, *, method: str = "auto") -> tuple[bytes, str]:
    """Bytes in, PNG bytes out — the endpoint's whole job. Returns (png, filler used)."""
    from PIL import Image
    rgba = load_image(image_bytes)
    h, w = rgba.shape[:2]
    hole = read_mask(mask_bytes, w, h)
    t0 = time.monotonic()
    out, used = inpaint(rgba, hole, method=method)
    buf = io.BytesIO()
    Image.fromarray(out, "RGBA").save(buf, format="PNG", compress_level=6)
    logger.info("[magic-eraser] %dx%d filled with %s in %.2fs (%d px brushed)",
                w, h, used, time.monotonic() - t0, int(hole.sum()))
    return buf.getvalue(), used
