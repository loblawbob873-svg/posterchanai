"""Face swap for the Meme Builder: swap two faces in one picture, or put a face from another picture
onto one (or every) face in this one.

Asked for: "Meme Builder -> If you can add a face swapping feature that would be cool, make sure UI good
on mobile and desktop".

A CLASSIC SWAP, ON THE CPU, WITH WEIGHTS THE NODE ALREADY HAS. The 106-point landmarks come from the
buffalo_l pack Talking pictures already loads (effects_service.talk._talk_app) -- no new download, no
GPU, so it never waits behind (or holds) the GPU lock that chat, image, music and video share. Per face:

  1. the source face is ALIGNED onto the target with one similarity transform over the inner-face
     landmarks (brows, eyes, nose, mouth) -- rotated, scaled and moved, never stretched;
  2. its colour is matched to the target's skin (LAB mean/spread over the face region only), and
  3. only the inner face is blended in, through a feathered mask, so the target keeps its own jaw,
     hair and ears.
  A first version triangle-warped the whole face including the jaw onto the target's shape: on a turned
  head that stretched the face into a smear, and seamless cloning washed out its colour.

A neural swapper (inswapper) looks more realistic but is a 550 MB non-commercial-licence download and a
GPU job; this is the meme-grade swap people expect from a meme builder, and it works on drawings too.

Every refusal is a ValueError with a sentence (no face found, face too small, picture too big), which
the endpoint hands back as a 400 the person can act on.
"""
from __future__ import annotations

import io
import logging

logger = logging.getLogger(__name__)

MAX_PIXELS = 24_000_000          # a phone photo is ~12 MP; this is a DoS bound, not a feature
MIN_FACE_PX = 24                 # smaller than this there is nothing to swap convincingly
MAX_FACES = 12


def _decode(data: bytes):
    """(BGR uint8 array, alpha or None) from image bytes. Raises ValueError with a sentence."""
    import numpy as np
    from PIL import Image, ImageOps
    try:
        im = Image.open(io.BytesIO(data))
        im.load()
    except Exception:
        raise ValueError("That picture could not be read.")
    if im.width * im.height > MAX_PIXELS:
        raise ValueError("That picture is too large to swap faces in (24 megapixels at most).")
    im = ImageOps.exif_transpose(im)
    alpha = None
    if im.mode in ("RGBA", "LA") or (im.mode == "P" and "transparency" in im.info):
        im = im.convert("RGBA")
        alpha = np.array(im)[:, :, 3]
    rgb = np.array(im.convert("RGB"))
    return rgb[:, :, ::-1].copy(), alpha


def _encode(bgr, alpha) -> bytes:
    import numpy as np
    from PIL import Image
    rgb = bgr[:, :, ::-1]
    if alpha is not None:
        im = Image.fromarray(np.dstack([rgb, alpha]).astype("uint8"), "RGBA")
    else:
        im = Image.fromarray(rgb.astype("uint8"), "RGB")
    out = io.BytesIO()
    im.save(out, "PNG", compress_level=6)
    return out.getvalue()


def _faces(bgr) -> list:
    """Faces with 106 landmarks, left to right (the order the picker numbers them in)."""
    import numpy as np
    from app.services.effects_service.talk import _talk_app
    app = _talk_app()
    if app is None:
        raise ValueError("Face detection is not available on this server.")
    out = []
    for f in app.get(bgr) or []:
        lmk = getattr(f, "landmark_2d_106", None)
        if lmk is None or len(lmk) < 106:
            continue
        x1, y1, x2, y2 = [float(v) for v in f.bbox]
        if min(x2 - x1, y2 - y1) < MIN_FACE_PX:
            continue
        out.append({"box": (x1, y1, x2, y2), "pts": np.asarray(lmk, dtype=np.float32),
                    "score": float(getattr(f, "det_score", 1.0) or 1.0)})
    out.sort(key=lambda f: (f["box"][0] + f["box"][2]) / 2)
    return out[:MAX_FACES]


def detect(data: bytes) -> dict:
    """{"width", "height", "faces": [{"x","y","w","h"}]} in NORMALISED coordinates, left to right --
    what the picker draws its numbered boxes from, at whatever size it shows the picture."""
    bgr, _ = _decode(data)
    h, w = bgr.shape[:2]
    faces = []
    for f in _faces(bgr):
        x1, y1, x2, y2 = f["box"]
        faces.append({"x": round(max(0.0, x1) / w, 4), "y": round(max(0.0, y1) / h, 4),
                      "w": round((min(w, x2) - max(0.0, x1)) / w, 4),
                      "h": round((min(h, y2) - max(0.0, y1)) / h, 4)})
    return {"width": w, "height": h, "faces": faces}


# 2d106 layout: 0-32 are the jaw contour, 33-105 the brows, eyes, nose and mouth. The JAW is left out of
# both the alignment and the mask: a turned head's contour collapses on the far side, and pulling the
# swapped face out to it is what stretched the first version into a smear. The inner face carries the
# identity; the target keeps its own jaw, hair and ears, which is what makes the result read as a swap.
_INNER = slice(33, 106)


def _align(src_pts, dst_pts):
    """2x3 SIMILARITY transform (rotation, uniform scale, shift -- no shear, no stretch) taking the
    source's inner-face landmarks onto the target's: ordinary Procrustes, via SVD."""
    import numpy as np
    a = np.asarray(src_pts, dtype=np.float64)
    b = np.asarray(dst_pts, dtype=np.float64)
    ca, cb = a.mean(axis=0), b.mean(axis=0)
    a0, b0 = a - ca, b - cb
    sa, sb = np.sqrt((a0 ** 2).sum() / len(a0)), np.sqrt((b0 ** 2).sum() / len(b0))
    a0, b0 = a0 / sa, b0 / sb
    u, _, vt = np.linalg.svd(a0.T @ b0)
    r = (u @ vt).T
    m = np.zeros((2, 3))
    m[:, :2] = (sb / sa) * r
    m[:, 2] = cb - m[:, :2] @ ca
    return m


def _face_mask(shape, pts, feather):
    """The inner face (convex hull of brows, eyes, nose, mouth), grown a little and feathered."""
    import cv2
    import numpy as np
    mask = np.zeros(shape[:2], dtype=np.float32)
    cv2.fillConvexPoly(mask, cv2.convexHull(np.asarray(pts, dtype=np.int32)), 1.0)
    k = max(3, int(feather) | 1)
    mask = cv2.dilate(mask, np.ones((k, k), np.uint8))
    return cv2.GaussianBlur(mask, (k * 2 + 1, k * 2 + 1), 0)


# A PUPPET, CARTOON OR DRAWING still DETECTS, but the landmark model (trained on photographs) bunches its
# points into a patch -- on a Muppet they covered 46% x 45% of the face box ("face swap did not
# actually swap": the face went onto under half of the puppet's face). Neither span alone separates
# that from a real face: a real head turned sideways measured 46% wide too. Two signals must agree:
#   * the landmarks cover little of the box: area < 0.30 (real faces 0.25-0.52, the Muppet 0.21), and
#   * the detector is unsure it is a face: score < 0.75 (8 real faces 0.785-0.92, the Muppet 0.62).
# A turned real head has the first and not the second, and keeps its landmarks.
_STYLISED_AREA = 0.30
_STYLISED_SCORE = 0.75


def _stylised(face) -> bool:
    import numpy as np
    x1, y1, x2, y2 = face["box"]
    bw, bh = float(x2 - x1), float(y2 - y1)
    if bw <= 0 or bh <= 0:
        return False
    inner = face["pts"][_INNER]
    area = (float(np.ptp(inner[:, 0])) / bw) * (float(np.ptp(inner[:, 1])) / bh)
    return area < _STYLISED_AREA and float(face.get("score", 1.0)) < _STYLISED_SCORE


def _skin_like(lab_mean) -> bool:
    """Is this average colour a HUMAN skin tone (OpenCV 8-bit LAB, 128 = neutral)? Measured skin
    across tones sits at a~135-165, b~130-175; a purple puppet, a blue monster, a green alien do not."""
    _, a, b = lab_mean
    return 128 <= a <= 172 and 125 <= b <= 185


def _correct_colour(dst, warped, region):
    """Match the warped face's colour to the target's: per-channel mean and spread in LAB, measured
    over the face region ONLY. A spatial ratio of blurred pictures (the first attempt here) pulled in
    whatever surrounds the face -- hair, a hand, playing cards -- and painted it onto the skin as
    glowing blobs beside the nose.

    COLOUR ONLY WHERE THE TARGET IS SKIN. Brightness is always matched (that is what makes the face sit
    in the target's light). The two colour channels are matched fully only when the target's face is a
    human skin tone; on a purple puppet they move a quarter of the way, or the pasted person turns
    purple and disappears into the puppet -- which is what "did not actually swap" looked like."""
    import cv2
    import numpy as np
    m = region > 0.5
    if m.sum() < 50:
        return warped.astype(np.float64)
    a = cv2.cvtColor(warped, cv2.COLOR_BGR2LAB).astype(np.float64)
    b = cv2.cvtColor(dst, cv2.COLOR_BGR2LAB).astype(np.float64)
    target_mean = [b[..., c][m].mean() for c in range(3)]
    chroma = 1.0 if _skin_like(target_mean) else 0.25
    for c in range(3):
        am, asd = a[..., c][m].mean(), a[..., c][m].std() + 1e-6
        bm, bsd = target_mean[c], b[..., c][m].std() + 1e-6
        if c == 0:
            # The spread is only nudged (never more than 1.5x either way): the source keeps its own
            # contrast, so its features stay legible on a flat-lit target.
            a[..., c] = (a[..., c] - am) * float(np.clip(bsd / asd, 0.67, 1.5)) + bm
        else:
            a[..., c] = a[..., c] + (bm - am) * chroma
    return cv2.cvtColor(np.clip(a, 0, 255).astype(np.uint8), cv2.COLOR_LAB2BGR).astype(np.float64)


def _put(src_img, src_face, dst_img, dst_face):
    """dst_img with src_face's inner face aligned onto dst_face and blended in."""
    import cv2
    import numpy as np
    s_in, d_in = src_face["pts"][_INNER], dst_face["pts"][_INNER]
    stylised = _stylised(dst_face)
    if stylised:
        # The target's landmarks are not where its face is: place the source face by the target's
        # BOX instead -- its inner points mapped from the source box into the target box, so it takes
        # the face's whole extent at a real face's proportions.
        sx1, sy1, sx2, sy2 = src_face["box"]
        dx1, dy1, dx2, dy2 = dst_face["box"]
        rel = (s_in - np.array([sx1, sy1])) / np.array([max(1.0, sx2 - sx1), max(1.0, sy2 - sy1)])
        d_in = rel * np.array([dx2 - dx1, dy2 - dy1]) + np.array([dx1, dy1])
    m = _align(s_in, d_in)
    h, w = dst_img.shape[:2]
    warped = cv2.warpAffine(src_img, m, (w, h), flags=cv2.INTER_LINEAR, borderMode=cv2.BORDER_REFLECT)
    # Size of the face on the target, for the feather: the eye-to-eye span.
    span = float(np.ptp(d_in[:, 0])) or 40.0
    # Where the SOURCE face actually lands (never blend the source picture's background in).
    src_mask = np.maximum(cv2.warpAffine(_face_mask(src_img.shape, s_in, span * 0.06), m, (w, h)), 0)
    # On a stylised target its own landmarks are the unreliable part, so the source's outline decides.
    mask = (src_mask if stylised else np.minimum(_face_mask(dst_img.shape, d_in, span * 0.06), src_mask))[..., None]
    corrected = _correct_colour(dst_img, warped, mask[..., 0])
    out = dst_img.astype(np.float64) * (1 - mask) + corrected * mask
    return np.clip(out, 0, 255).astype(np.uint8)


def swap(target: bytes, a: int = 0, b: int = 1) -> bytes:
    """Swap faces `a` and `b` (left-to-right numbering) within one picture."""
    img, alpha = _decode(target)
    faces = _faces(img)
    if len(faces) < 2:
        raise ValueError("Swapping needs two faces in the picture — this one has "
                         + ("one." if len(faces) == 1 else "none that could be found."))
    if a == b or not (0 <= a < len(faces)) or not (0 <= b < len(faces)):
        raise ValueError("Pick two different faces.")
    original = img.copy()
    out = _put(original, faces[b], img, faces[a])
    out = _put(original, faces[a], out, faces[b])
    return _encode(out, alpha)


def paste(target: bytes, source: bytes, source_face: int = 0, targets: list | None = None) -> bytes:
    """Put `source_face` from `source` onto the chosen faces of `target` (every face when None)."""
    img, alpha = _decode(target)
    faces = _faces(img)
    if not faces:
        raise ValueError("No face could be found in this picture.")
    src, _ = _decode(source)
    src_faces = _faces(src)
    if not src_faces:
        raise ValueError("No face could be found in the picture to take the face from.")
    if not (0 <= source_face < len(src_faces)):
        raise ValueError("Pick a face in the other picture.")
    chosen = list(range(len(faces))) if targets is None else sorted({int(t) for t in targets})
    if not chosen or any(not (0 <= t < len(faces)) for t in chosen):
        raise ValueError("Pick which face to replace.")
    out = img
    for t in chosen:
        out = _put(src, src_faces[source_face], out, faces[t])
    logger.info("[faceswap] pasted onto %d of %d face(s)", len(chosen), len(faces))
    return _encode(out, alpha)
