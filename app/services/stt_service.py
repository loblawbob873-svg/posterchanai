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
            _model = WhisperModel(_model_size, device="cpu", compute_type="int8", cpu_threads=_STT_THREADS)
            logger.info("Whisper model loaded successfully")
        except ImportError:
            logger.error("faster-whisper not installed. Run: pip install faster-whisper")
            return None
        except Exception as e:
            logger.error(f"Failed to load Whisper model: {e}")
            return None
    return _model


# TRANSCRIPTION NEVER RUNS ON THE EVENT LOOP. It used to: transcribe_audio is async but called model.transcribe and
# walked its segments right on the loop, and the app is ONE uvicorn worker -- so every clip froze the whole site for
# its duration. On 2026-10-15 13:19 a 4.6 s clip, on a CPU saturated by a test gate, froze poster.place for 27
# minutes (the last line the app logged was "[faster_whisper] Processing audio with duration 00:04.619"; every
# client and bot request timed out until a restart). Now: a worker thread, ONE transcription at a time, a bounded
# CPU share, and a time limit -- a stuck one costs that request, never the site. tests/test_stt_never_blocks_the_loop.py
_STT_TIMEOUT_S = 120.0
_STT_THREADS = max(1, min(4, (os.cpu_count() or 4) // 3))
_stt_gate = None


def _gate():
    global _stt_gate
    if _stt_gate is None:
        import asyncio
        _stt_gate = asyncio.Semaphore(1)
    return _stt_gate


def _transcribe_blocking(model, path, language):
    segments, info = model.transcribe(path, language=language, beam_size=5, vad_filter=True)
    text = " ".join(segment.text.strip() for segment in segments)   # the segments are lazy: decoding happens here
    return text, info


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

        import asyncio
        lang = language if language and language != "auto" else None
        async with _gate():
            try:
                text, info = await asyncio.wait_for(
                    asyncio.to_thread(_transcribe_blocking, model, temp_path, lang), timeout=_STT_TIMEOUT_S)
            except asyncio.TimeoutError:
                logger.error("Transcription took longer than %.0fs -- given up (the server kept serving)", _STT_TIMEOUT_S)
                return None, None
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
