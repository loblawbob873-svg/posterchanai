"""
Image generation backend for the bots.

Unified codebase: image generation always goes through the PosterChanAI server's image API
(posterchanai_api). The old ComfyUI / Stable-Diffusion backends were removed in the merge —
the bots reach the one server endpoint for both chat and images. Import image generation from
here so every listener shares the same backend.
"""
from posterchanai_api import (
    generate_image_bytes,
    generate_image_bytes_with_retries,
    describe_image_with_wd14,
    extract_prompt_from_image,
)

__all__ = [
    'generate_image_bytes',
    'generate_image_bytes_with_retries',
    'describe_image_with_wd14',
    'extract_prompt_from_image',
]
