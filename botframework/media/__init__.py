# Media package - TTS and image generation modules
"""
Media package for Posterchan.
Provides text-to-speech and image generation capabilities.

Modules:
    tts - Text-to-speech using Edge TTS
    comfyui - ComfyUI image generation (when available)
    stablediffusion - Stable Diffusion WebUI (when available)

Note: This package re-exports from existing root-level modules for convenience.
Direct imports (e.g., `from tts import ...`) still work.
"""

__all__ = []

# Re-export TTS functions
try:
    from tts import (
        generate_speech,
        generate_speech_with_retries,
        generate_narration_video,
        clean_text_for_tts,
        list_voices,
    )
    __all__.extend([
        'generate_speech',
        'generate_speech_with_retries',
        'generate_narration_video',
        'clean_text_for_tts',
        'list_voices',
    ])
except ImportError:
    generate_speech = None
    generate_speech_with_retries = None
    generate_narration_video = None
    clean_text_for_tts = None
    list_voices = None

# Re-export ComfyUI functions
try:
    from comfyui import (
        generate_image_bytes,
        generate_image_bytes_with_retries,
        generate_img2img_bytes,
        generate_img2img_bytes_with_retries,
    )
    __all__.extend([
        'generate_image_bytes',
        'generate_image_bytes_with_retries',
        'generate_img2img_bytes',
        'generate_img2img_bytes_with_retries',
    ])
except ImportError:
    generate_image_bytes = None
    generate_image_bytes_with_retries = None
    generate_img2img_bytes = None
    generate_img2img_bytes_with_retries = None
