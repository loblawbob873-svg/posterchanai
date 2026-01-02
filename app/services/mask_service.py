"""
Simple mask generation service for inpainting.
Generates masks for common operations like nudification.
"""
import io
import logging
from typing import Optional, Tuple
from PIL import Image, ImageDraw, ImageFilter

logger = logging.getLogger("mask_service")


def generate_body_mask(image_bytes: bytes, preserve_face: bool = True, mask_type: str = "chest") -> Optional[bytes]:
    """
    Generate a mask for specific body areas.
    White (255) = area to inpaint
    Black (0) = area to preserve

    mask_type: "chest" (default), "lower", "hands", "full"
    """
    try:
        img = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = img.size

        # Create black mask (preserve everything by default)
        mask = Image.new("L", (w, h), 0)
        draw = ImageDraw.Draw(mask)

        if mask_type == "chest":
            # Elliptical mask for chest/torso (nudification)
            draw.ellipse([int(w*0.25), int(h*0.32), int(w*0.75), int(h*0.78)], fill=255)
        elif mask_type == "lower":
            # Lower body area (for dildo, etc.)
            draw.ellipse([int(w*0.20), int(h*0.55), int(w*0.80), int(h*0.95)], fill=255)
        elif mask_type == "hands":
            # Hand areas on sides
            draw.ellipse([int(w*0.0), int(h*0.35), int(w*0.30), int(h*0.70)], fill=255)
            draw.ellipse([int(w*0.70), int(h*0.35), int(w*1.0), int(h*0.70)], fill=255)
        elif mask_type == "full":
            # Full body except face
            draw.rectangle([int(w*0.10), int(h*0.30), int(w*0.90), int(h*0.95)], fill=255)

        # Heavy blur for seamless blending
        mask = mask.filter(ImageFilter.GaussianBlur(radius=30))

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
    Returns: "chest", "lower", "hands", "full", "background", or "none"
    """
    prompt_lower = prompt.lower()

    # Nude/clothing removal keywords -> chest mask
    nude_keywords = ["nude", "naked", "undress", "topless", "breasts", "nipples", "bare"]
    if any(kw in prompt_lower for kw in nude_keywords):
        return "chest"

    # Lower body keywords -> lower mask
    lower_keywords = ["dildo", "vibrator", "pussy", "ass", "butt", "bottomless", "panties", "penis", "cock"]
    if any(kw in prompt_lower for kw in lower_keywords):
        return "lower"

    # Holding/hand object keywords -> hands mask
    hand_keywords = ["holding", "carry", "grab", "hand", "can", "bottle", "phone", "weapon", "sword", "gun"]
    if any(kw in prompt_lower for kw in hand_keywords):
        return "hands"

    # Background change keywords
    bg_keywords = ["background", "scene", "location", "setting", "environment",
                   "beach", "forest", "city", "room", "outdoor", "indoor"]
    if any(kw in prompt_lower for kw in bg_keywords):
        return "background"

    # Default to full body for general changes
    return "full"


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
