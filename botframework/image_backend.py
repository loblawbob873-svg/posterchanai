"""
Image Generation Backend Selector.
Automatically selects between posterchanai (native diffusers) and ComfyUI.
Use this instead of importing from comfyui directly.
"""
from config import USE_POSTERCHANAI

if USE_POSTERCHANAI:
    print("[IMAGE] Using posterchanai native backend")
    from posterchanai_api import (
        generate_image_bytes,
        generate_image_bytes_with_retries,
        describe_image_with_wd14,
        extract_prompt_from_image,
    )
else:
    print("[IMAGE] Using ComfyUI backend")
    from comfyui import (
        generate_image_bytes,
        generate_image_bytes_with_retries,
        describe_image_with_wd14,
        extract_prompt_from_image,
    )

# Re-export everything
__all__ = [
    'generate_image_bytes',
    'generate_image_bytes_with_retries',
    'describe_image_with_wd14',
    'extract_prompt_from_image',
]
