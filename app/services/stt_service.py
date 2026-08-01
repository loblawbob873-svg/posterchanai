"""
Speech-to-Text service using faster-whisper.
Provides local transcription for browsers that block Web Speech API (like Brave).
"""

import logging
import tempfile
import os
from typing import Optional, Tuple

logger = logging.getLogger(__name__)

# Lazy load model to avoid startup delay
_model = None
_model_size = "medium"  # Options: tiny, base, small, medium, large-v3


def get_model():
    """Get or initialize the Whisper model."""
    global _model
    if _model is None:
        try:
            from faster_whisper import WhisperModel
            logger.info(f"Loading Whisper model: {_model_size}")
            # Use CPU by default, can be changed to "cuda" for GPU
            _model = WhisperModel(_model_size, device="cpu", compute_type="int8")
            logger.info("Whisper model loaded successfully")
        except ImportError:
            logger.error("faster-whisper not installed. Run: pip install faster-whisper")
            return None
        except Exception as e:
            logger.error(f"Failed to load Whisper model: {e}")
            return None
    return _model


async def transcribe_audio(audio_data: bytes, language: str = "auto") -> Tuple[Optional[str], Optional[str]]:
    """
    Transcribe audio data to text using Whisper.

    Args:
        audio_data: Raw audio bytes (webm, wav, mp3, etc.)
        language: Language code (e.g., "en", "ja", "es"), or "auto"/None to let Whisper detect it.
                  Defaults to "auto" — forcing a language (the old "en" default) garbled non-English
                  speech, and the Live Translate feature needs the detected language to route the turn.

    Returns:
        (text, detected_language) — e.g. ("hello", "en"). (None, None) on error / model unavailable.
        `detected_language` is Whisper's ISO-639-1 guess (from `info.language`), present even when a
        language was forced.
    """
    model = get_model()
    if model is None:
        return None, None

    # Write audio to temp file (faster-whisper needs a file path)
    temp_path = None
    try:
        with tempfile.NamedTemporaryFile(prefix="pcai_stt_", suffix=".webm", delete=False) as f:
            f.write(audio_data)
            temp_path = f.name

        # Transcribe
        segments, info = model.transcribe(
            temp_path,
            language=language if language and language != "auto" else None,
            beam_size=5,
            vad_filter=True,  # Filter out silence
        )

        # Combine all segments
        text = " ".join(segment.text.strip() for segment in segments)
        detected = getattr(info, "language", None)

        logger.info(f"Transcribed {info.duration:.1f}s audio [{detected}]: {text[:50]}...")
        return text.strip(), detected

    except Exception as e:
        logger.error(f"Transcription error: {e}")
        return None, None
    finally:
        # Clean up temp file
        if temp_path and os.path.exists(temp_path):
            try:
                os.unlink(temp_path)
            except Exception:
                pass


def is_available() -> bool:
    """Check if STT service is available."""
    try:
        from faster_whisper import WhisperModel
        return True
    except ImportError:
        return False
