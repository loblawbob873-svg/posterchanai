"""POST /api/bots/talk — a talking bot turns its reply TEXT into its talking-face clip.

Called by the bot process (botframework/nostrListener.py) with the credential the manager issued to
THAT bot (talkbot_service.token). The face, voice and mouth come from the bot's own saved config, so a
bot can only ever render as itself. See app/services/talkbot_service.py.
"""
import logging
import secrets
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException
from fastapi.responses import Response
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.database import get_db

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/bots", tags=["bots"])


# One shuffle bag per bot: every face is used once, in random order, before any repeats, and a new
# round never starts with the face that just ended the last one. A plain random pick per reply
# showed the same picture three times in a row (1 in 9 for three faces, and it happened on day one).
# Per process is fine: this runs on the single port-3051 app, and a restart only starts a new round.
_bags: dict = {}


def next_face(bot: str, faces: list) -> tuple:
    """(index, face) for this bot's next reply."""
    key = (bot, tuple(f["sha"] for f in faces))
    bag = _bags.get(key)
    if not bag or not bag["order"]:
        order = list(range(len(faces)))
        secrets.SystemRandom().shuffle(order)
        last = bag["last"] if bag else None
        if len(order) > 1 and order[0] == last:
            order.append(order.pop(0))           # never the same face twice in a row across rounds
        bag = _bags[key] = {"order": order, "last": last}
    i = bag["order"].pop(0)
    bag["last"] = i
    return i, faces[i]


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
    faces = talkbot_service.faces_of(cfg)
    if not (faces and cfg.get("talk_voice_sha")):
        raise HTTPException(status_code=409, detail="this bot has no face or voice set (Admin → Bots)")
    idx, face = next_face(req.bot, faces)
    logger.info("[talkbot] %s speaks with face %d/%d (%s)", req.bot, idx + 1, len(faces), face["sha"][:12])
    try:
        try:
            limit = max(3, min(60, int(cfg.get("talk_max_words") or 15)))
        except (TypeError, ValueError):
            limit = 15
        clip = await talkbot_service.render(db, face["sha"], cfg["talk_voice_sha"], face["mouth"], req.text,
                                            max_words=limit + limit // 3)   # a little slack over the ask
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))
    return Response(content=clip, media_type="video/mp4")
