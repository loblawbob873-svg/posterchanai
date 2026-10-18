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
    import asyncio
    try:
        with tempfile.NamedTemporaryFile(prefix="pcai_stt_", suffix=".webm", delete=False) as f:
            f.write(audio_data)
            temp_path = f.name
    except OSError as e:
        logger.error("Transcription error: %s", type(e).__name__)
        return None, None

    def _cleanup(_fut=None):
        try:
            os.unlink(temp_path)
        except OSError:
            pass

    lang = language if language and language != "auto" else None
    gate = _gate()
    try:
        await asyncio.wait_for(gate.acquire(), timeout=_STT_TIMEOUT_S)   # behind a hung decode: give up, never hang
    except asyncio.TimeoutError:
        _cleanup()
        logger.error("Transcription waited %.0fs for the one before it -- given up", _STT_TIMEOUT_S)
        return None, None
    try:
        work = asyncio.ensure_future(asyncio.to_thread(_transcribe_blocking, model, temp_path, lang))
    except BaseException:
        gate.release()
        _cleanup()
        raise
    # ONE AT A TIME MEANS ONE RUNNING, not one waited for: the slot (and the temp file) are let go when the THREAD
    # ends. Released at the timeout, a slow clip's thread kept decoding while the next one started, and repeated slow
    # clips stacked up threads of _STT_THREADS cores each (security review 2026-10-18).
    work.add_done_callback(lambda _f: (gate.release(), _cleanup()))
    try:
        text, info = await asyncio.wait_for(asyncio.shield(work), timeout=_STT_TIMEOUT_S)
    except asyncio.TimeoutError:
        logger.error("Transcription took longer than %.0fs -- given up (the server kept serving)", _STT_TIMEOUT_S)
        return None, None
    except Exception as e:
        logger.error("Transcription error: %s", type(e).__name__)
        return None, None
    detected = getattr(info, "language", None)
    # The SIZE only -- never what was said (no user content in logs).
    logger.info("Transcribed %.1fs audio [%s]: %d chars", info.duration, detected, len(text))
    return text.strip(), detected


def is_available() -> bool:
    """Check if STT service is available."""
    try:
        from faster_whisper import WhisperModel
        return True
    except ImportError:
        return False
