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

    # Extract face from original - include forehead/hair area
    ox1, oy1, ox2, oy2 = [int(x) for x in orig_face.bbox]
    face_h = oy2 - oy1
    face_w = ox2 - ox1
    margin_side = int(face_w * 0.15)  # Small side margin
    margin_top = int(face_h * 0.35)   # Top margin for hair (not too much)
    margin_bottom = int(face_h * 0.1) # Small bottom margin
    ox1 = max(0, ox1 - margin_side)
    oy1 = max(0, oy1 - margin_top)
    ox2 = min(original.width, ox2 + margin_side)
    oy2 = min(original.height, oy2 + margin_bottom)
    face_crop = original.crop((ox1, oy1, ox2, oy2))

    # Get target position in generated image
    gx1, gy1, gx2, gy2 = [int(x) for x in gen_face.bbox]
    face_h = gy2 - gy1
    face_w = gx2 - gx1
    margin_side = int(face_w * 0.15)
    margin_top = int(face_h * 0.35)
    margin_bottom = int(face_h * 0.1)
    gx1 = max(0, gx1 - margin_side)
    gy1 = max(0, gy1 - margin_top)
    gx2 = min(generated.width, gx2 + margin_side)
    gy2 = min(generated.height, gy2 + margin_bottom)

    # Resize face to target size
    target_w = gx2 - gx1
    target_h = gy2 - gy1
    face_resized = face_crop.resize((target_w, target_h), Image.LANCZOS)

    # Start with generated image
    result = generated.copy()

    # Create elliptical mask - covers face and hair with soft edges
    mask = Image.new('L', (target_w, target_h), 0)
    draw = ImageDraw.Draw(mask)
    # Ellipse with small insets - covers most of the crop
    inset_x = int(target_w * 0.05)
    inset_top = int(target_h * 0.02)  # Minimal top inset to include hair
    inset_bottom = int(target_h * 0.08)
    draw.ellipse([inset_x, inset_top, target_w - inset_x, target_h - inset_bottom], fill=255)
    # Moderate blur for blending
    blur_radius = max(20, target_w // 5)
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
