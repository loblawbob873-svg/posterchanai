"""Face swap service using InsightFace."""
import numpy as np
from PIL import Image, ImageDraw, ImageFilter
import cv2
import io
from typing import Optional
import logging

logger = logging.getLogger(__name__)

# Lazy load face analyzer
_face_app = None

def get_face_analyzer():
    """Get or initialize face analyzer."""
    global _face_app
    if _face_app is None:
        try:
            from insightface.app import FaceAnalysis
            _face_app = FaceAnalysis(name='buffalo_l', providers=['CPUExecutionProvider'])
            _face_app.prepare(ctx_id=0, det_size=(640, 640))
            logger.info("Face analyzer initialized")
        except Exception as e:
            logger.error(f"Failed to initialize face analyzer: {e}")
            return None
    return _face_app


def detect_face(image: Image.Image):
    """Detect largest face in image."""
    app = get_face_analyzer()
    if not app:
        return None

    img_bgr = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
    faces = app.get(img_bgr)
    if not faces:
        return None
    return max(faces, key=lambda x: (x.bbox[2]-x.bbox[0]) * (x.bbox[3]-x.bbox[1]))


def swap_face(original: Image.Image, generated: Image.Image) -> Image.Image:
    """
    Swap face from original image onto generated image.
    Returns generated image with original face blended in.
    """
    orig_face = detect_face(original)
    gen_face = detect_face(generated)

    if not orig_face or not gen_face:
        logger.warning("Could not detect face in one or both images")
        return generated

    # Extract face from original with margin for blending
    ox1, oy1, ox2, oy2 = [int(x) for x in orig_face.bbox]
    margin = int((ox2 - ox1) * 0.35)  # Larger margin for more context
    ox1 = max(0, ox1 - margin)
    oy1 = max(0, oy1 - margin)
    ox2 = min(original.width, ox2 + margin)
    oy2 = min(original.height, oy2 + margin)
    face_crop = original.crop((ox1, oy1, ox2, oy2))

    # Get target position in generated image
    gx1, gy1, gx2, gy2 = [int(x) for x in gen_face.bbox]
    margin = int((gx2 - gx1) * 0.35)  # Match margin
    gx1 = max(0, gx1 - margin)
    gy1 = max(0, gy1 - margin)
    gx2 = min(generated.width, gx2 + margin)
    gy2 = min(generated.height, gy2 + margin)

    # Resize face to target size
    target_w = gx2 - gx1
    target_h = gy2 - gy1
    face_resized = face_crop.resize((target_w, target_h), Image.LANCZOS)

    # Start with generated image
    result = generated.copy()

    # Create elliptical mask with heavy feathering for seamless blend
    mask = Image.new('L', (target_w, target_h), 0)
    draw = ImageDraw.Draw(mask)
    # Large inset - only blend the center face area
    inset_x = int(target_w * 0.2)
    inset_y = int(target_h * 0.15)
    draw.ellipse([inset_x, inset_y, target_w - inset_x, target_h - inset_y], fill=255)
    # Heavy blur for seamless transition
    blur_radius = max(25, target_w // 4)
    mask = mask.filter(ImageFilter.GaussianBlur(radius=blur_radius))

    # Paste face with mask
    result.paste(face_resized, (gx1, gy1), mask)

    logger.info("Face swap completed")
    return result


def swap_face_bytes(original_bytes: bytes, generated_bytes: bytes) -> Optional[bytes]:
    """
    Swap face from original onto generated, return result as bytes.
    """
    try:
        original = Image.open(io.BytesIO(original_bytes)).convert('RGB')
        generated = Image.open(io.BytesIO(generated_bytes)).convert('RGB')

        result = swap_face(original, generated)

        output = io.BytesIO()
        result.save(output, format='PNG')
        return output.getvalue()
    except Exception as e:
        logger.error(f"Face swap failed: {e}")
        return None
