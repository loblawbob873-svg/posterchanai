"""POST /api/bots/talk — a talking bot turns its reply TEXT into its talking-face clip.

Called by the bot process (botframework/nostrListener.py) with the credential the manager issued to
THAT bot (talkbot_service.token). The face, voice and mouth come from the bot's own saved config, so a
bot can only ever render as itself. See app/services/talkbot_service.py.
"""
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db

router = APIRouter(prefix="/api/bots", tags=["bots"])


class TalkReq(BaseModel):
    bot: str
    text: str


@router.post("/talk")
async def bot_talk(req: TalkReq, x_pc_talk_token: Optional[str] = Header(None),
                   db: Session = Depends(get_db)):
    from app.services import talkbot_service
    if not talkbot_service.token_ok(req.bot, x_pc_talk_token or ""):
        raise HTTPException(status_code=401, detail="not this bot's credential")
    try:
        cfg = talkbot_service.bot_config(db, req.bot)
    except LookupError:
        raise HTTPException(status_code=404, detail="no such bot")
    if not (cfg.get("talk_face_sha") and cfg.get("talk_voice_sha")):
        raise HTTPException(status_code=409, detail="this bot has no face or voice set (Admin → Bots)")
    try:
        clip = await talkbot_service.render(db, cfg["talk_face_sha"], cfg["talk_voice_sha"],
                                            cfg.get("talk_mouth"), req.text)
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return Response(content=clip, media_type="video/mp4")
