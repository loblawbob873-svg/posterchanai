"""
Simple mask generation service for inpainting.
Generates masks for common operations like nudification.
"""
import io
import logging
from typing import Optional, Tuple
from PIL import Image, ImageDraw, ImageFilter

logger = logging.getLogger("mask_service")


def generate_body_mask(image_bytes: bytes, preserve_face: bool = True) -> Optional[bytes]:
    """
    Generate a mask for the body area of a person image.
    White (255) = area to inpaint (body/clothing)
    Black (0) = area to preserve (face/background)

    Simple approach: assumes person is centered, masks middle body area.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = img.size

        # Create black mask (preserve everything by default)
        mask = Image.new("L", (w, h), 0)
        draw = ImageDraw.Draw(mask)

        # Simple heuristic: body is roughly center 60% width, from 20% to 90% height
        # Face is top 25% of image typically

        body_left = int(w * 0.15)
        body_right = int(w * 0.85)

        if preserve_face:
            # Start below face area (roughly top 30% is head)
            body_top = int(h * 0.28)
        else:
            body_top = int(h * 0.1)

        body_bottom = int(h * 0.95)

        # Draw white rectangle for body area
        draw.rectangle([body_left, body_top, body_right, body_bottom], fill=255)

        # Feather the edges for smoother blending
        mask = mask.filter(ImageFilter.GaussianBlur(radius=10))

        # Convert back to bytes
        mask_bytes = io.BytesIO()
        mask.save(mask_bytes, format="PNG")
        return mask_bytes.getvalue()

    except Exception as e:
        logger.error(f"Error generating body mask: {e}")
        return None


def generate_clothing_mask_from_tags(image_bytes: bytes, tags: str) -> Optional[bytes]:
    """
    Generate a mask based on WD14 tags.
    If clothing tags detected, create body mask.
    """
    clothing_tags = [
        "shirt", "dress", "skirt", "pants", "shorts", "jacket", "coat",
        "uniform", "swimsuit", "bikini", "bra", "underwear", "panties",
        "leotard", "bodysuit", "armor", "clothes", "clothing", "outfit",
        "blouse", "sweater", "hoodie", "vest", "suit", "gown", "robe"
    ]

    tags_lower = tags.lower()
    has_clothing = any(tag in tags_lower for tag in clothing_tags)

    if has_clothing:
        return generate_body_mask(image_bytes, preserve_face=True)

    # No clothing detected, return None (no mask needed)
    return None


def generate_background_mask(image_bytes: bytes) -> Optional[bytes]:
    """
    Generate a mask for the background (everything except center subject).
    Useful for background replacement.
    """
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = img.size

        # Create white mask (inpaint everything by default)
        mask = Image.new("L", (w, h), 255)
        draw = ImageDraw.Draw(mask)

        # Preserve center area (subject) - roughly center 50%
        subject_left = int(w * 0.2)
        subject_right = int(w * 0.8)
        subject_top = int(h * 0.05)
        subject_bottom = int(h * 0.95)

        # Draw black ellipse for subject area (preserve)
        draw.ellipse([subject_left, subject_top, subject_right, subject_bottom], fill=0)

        # Feather edges
        mask = mask.filter(ImageFilter.GaussianBlur(radius=15))

        mask_bytes = io.BytesIO()
        mask.save(mask_bytes, format="PNG")
        return mask_bytes.getvalue()

    except Exception as e:
        logger.error(f"Error generating background mask: {e}")
        return None


def detect_mask_type_from_prompt(prompt: str) -> str:
    """
    Detect what type of mask is needed based on the prompt.
    Returns: "body", "background", "none", or "custom"
    """
    prompt_lower = prompt.lower()

    # Nude/clothing removal keywords
    nude_keywords = ["nude", "naked", "undress", "remove clothes", "no clothes",
                     "strip", "nudify", "topless", "bottomless"]
    if any(kw in prompt_lower for kw in nude_keywords):
        return "body"

    # Background change keywords
    bg_keywords = ["background", "scene", "location", "setting", "environment",
                   "beach", "forest", "city", "room", "outdoor", "indoor"]
    if any(kw in prompt_lower for kw in bg_keywords):
        return "background"

    return "none"


def auto_generate_mask(image_bytes: bytes, prompt: str, tags: str = None) -> Optional[bytes]:
    """
    Automatically generate appropriate mask based on prompt and image tags.

    Args:
        image_bytes: Source image
        prompt: User's modification prompt (e.g., "nude", "beach background")
        tags: Optional WD14 tags for the image

    Returns:
        Mask bytes or None if no mask needed
    """
    mask_type = detect_mask_type_from_prompt(prompt)

    if mask_type == "body":
        # For nude requests, generate body mask
        if tags:
            return generate_clothing_mask_from_tags(image_bytes, tags)
        else:
            return generate_body_mask(image_bytes, preserve_face=True)

    elif mask_type == "background":
        return generate_background_mask(image_bytes)

    # No automatic mask for other types
    return None
