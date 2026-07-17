# Media package - TTS and image generation modules
"""
Media package for Posterchan.
Provides text-to-speech and image generation capabilities.

Modules:
    tts - Text-to-speech using Edge TTS

Image generation lives in image_backend (native diffusers via posterchanai_api) — import it
from there, not this package.

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
