"""
Speech-to-Text API endpoint.
Uses Whisper for local transcription - works in any browser.
"""

from fastapi import APIRouter, UploadFile, File, HTTPException, Depends
from fastapi.responses import JSONResponse
from app.auth import get_current_user
from app.models import User
from app.services import stt_service
import logging

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/stt", tags=["stt"])


@router.get("/status")
async def stt_status():
    """Check if STT service is available."""
    available = stt_service.is_available()
    return {"available": available}


@router.post("/transcribe")
async def transcribe(
    audio: UploadFile = File(...),
    current_user: User = Depends(get_current_user)
):
    """
    Transcribe audio to text using Whisper.

    Accepts audio file (webm, wav, mp3, ogg, etc.)
    Returns transcribed text.
    """
    if not stt_service.is_available():
        raise HTTPException(
            status_code=503,
            detail="STT service not available. Install faster-whisper."
        )

    # Read audio data
    try:
        audio_data = await audio.read()
    except Exception as e:
        logger.error(f"Failed to read audio: {e}")
        raise HTTPException(status_code=400, detail="Failed to read audio file")

    if len(audio_data) < 100:
        raise HTTPException(status_code=400, detail="Audio file too small")

    # Limit file size (10MB max)
    if len(audio_data) > 10 * 1024 * 1024:
        raise HTTPException(status_code=400, detail="Audio file too large (max 10MB)")

    # Transcribe
    text = await stt_service.transcribe_audio(audio_data)

    if text is None:
        raise HTTPException(status_code=500, detail="Transcription failed")

    return {"text": text}
