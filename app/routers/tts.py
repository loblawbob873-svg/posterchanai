from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session
from app.database import get_db
from app.models import User
from app.schemas import TTSRequest, TTSResponse
from app.auth import get_current_user
from app.services.tts_service import TTSService

router = APIRouter(prefix="/api", tags=["tts"])


@router.post("/tts", response_model=TTSResponse)
async def generate_tts(
    request: TTSRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_user)
):
    tts_service = TTSService(db)
    audio = await tts_service.generate_speech(request.text, request.voice)

    if not audio:
        raise HTTPException(status_code=500, detail="Failed to generate audio")

    return TTSResponse(audio=audio)


@router.get("/tts/voices")
async def list_voices():
    from app.services.tts_service import TTSService
    voices = await TTSService.list_voices()
    return {"voices": voices}
