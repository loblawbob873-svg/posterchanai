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


def detect_faces(image: Image.Image):
    """Detect all faces in image, returns list."""
    app = get_face_analyzer()
    if not app:
        return []

    img_bgr = cv2.cvtColor(np.array(image), cv2.COLOR_RGB2BGR)
    faces = app.get(img_bgr)
    return faces if faces else []


def detect_face(image: Image.Image):
    """Detect single face - returns face only if exactly ONE face found."""
    faces = detect_faces(image)
    if len(faces) == 1:
        return faces[0]
    # Multiple or no faces - return None to skip face swap
    if len(faces) > 1:
        logger.warning(f"Multiple faces detected ({len(faces)}) - skipping face swap")
    return None


def create_body_mask(image: Image.Image, face_margin: float = 0.3) -> Optional[Image.Image]:
    """
    Create a mask for inpainting body while preserving face.
    Returns mask where WHITE=inpaint (body), BLACK=keep (face).
    Only works if exactly ONE face detected (avoids complex backgrounds).
    """
    faces = detect_faces(image)
    if not faces:
        logger.warning("No face detected - cannot create body mask")
        return None
    if len(faces) > 1:
        logger.warning(f"Multiple faces ({len(faces)}) detected - skipping face mask to avoid chaos")
        return None

    # Create white mask (inpaint everything)
    mask = Image.new('L', image.size, 255)
    draw = ImageDraw.Draw(mask)

    # Draw black ellipse for the single face
    for face in faces:
        x1, y1, x2, y2 = [int(x) for x in face.bbox]
        face_w = x2 - x1
        face_h = y2 - y1

        # Add margin around face
        margin_x = int(face_w * face_margin)
        margin_y = int(face_h * face_margin)
        x1 = max(0, x1 - margin_x)
        y1 = max(0, y1 - margin_y)
        x2 = min(image.width, x2 + margin_x)
        y2 = min(image.height, y2 + margin_y)

        # Draw black ellipse (keep face area)
        draw.ellipse([x1, y1, x2, y2], fill=0)

    # Blur mask edges for smooth blending
    mask = mask.filter(ImageFilter.GaussianBlur(radius=20))

    logger.info(f"Created body mask preserving {len(faces)} face(s)")
    return mask


def create_body_mask_bytes(image_bytes: bytes, face_margin: float = 0.3) -> Optional[bytes]:
    """Create body mask from image bytes, return mask as PNG bytes."""
    try:
        image = Image.open(io.BytesIO(image_bytes)).convert('RGB')
        mask = create_body_mask(image, face_margin)
        if mask is None:
            return None
        output = io.BytesIO()
        mask.save(output, format='PNG')
        return output.getvalue()
    except Exception as e:
        logger.error(f"Failed to create body mask: {e}")
        return None


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

    # Extract face from original - tight crop, just the face
    ox1, oy1, ox2, oy2 = [int(x) for x in orig_face.bbox]
    face_h = oy2 - oy1
    face_w = ox2 - ox1
    margin_side = int(face_w * 0.05)  # Minimal side margin
    margin_top = int(face_h * 0.1)    # Small top margin
    margin_bottom = int(face_h * 0.05) # Minimal bottom margin
    ox1 = max(0, ox1 - margin_side)
    oy1 = max(0, oy1 - margin_top)
    ox2 = min(original.width, ox2 + margin_side)
    oy2 = min(original.height, oy2 + margin_bottom)
    face_crop = original.crop((ox1, oy1, ox2, oy2))

    # Get target position in generated image
    gx1, gy1, gx2, gy2 = [int(x) for x in gen_face.bbox]
    face_h = gy2 - gy1
    face_w = gx2 - gx1
    margin_side = int(face_w * 0.05)
    margin_top = int(face_h * 0.1)
    margin_bottom = int(face_h * 0.05)
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

    # Create elliptical mask - tight to face with heavy feathering
    mask = Image.new('L', (target_w, target_h), 0)
    draw = ImageDraw.Draw(mask)
    # Larger insets for tight mask
    inset_x = int(target_w * 0.12)
    inset_top = int(target_h * 0.08)
    inset_bottom = int(target_h * 0.12)
    draw.ellipse([inset_x, inset_top, target_w - inset_x, target_h - inset_bottom], fill=255)
    # Heavy blur for smooth blending
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
