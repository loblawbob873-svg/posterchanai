"""Community facts for the bots that post about this node (block bot, daily stats) -- see
app/services/community_stats.py.

NOT the ordinary bot-API auth: who blocked whom is private on the fediverse, so any member's API key
reading it (or a script with one) would be a leak. Only the key the bots are configured with, an
admin's key, or a peer node may ask."""
import json
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request
from sqlalchemy.orm import Session

from app.database import get_db
from app.services import community_stats
from app.utils import lb_auth


async def get_bot_auth(request: Request, x_api_key: Optional[str] = Header(None),
                       authorization: Optional[str] = Header(None), db: Session = Depends(get_db)) -> bool:
    if lb_auth.is_internal(request):
        return True
    key = (x_api_key or "").strip() or ((authorization or "")[7:].strip()
                                         if (authorization or "").startswith("Bearer ") else "")
    if not key:
        raise HTTPException(401, "An API key is required")
    from app.models import Bot
    from app.services import settings_store
    bot_keys = {str(settings_store.get("bots_posterchanai_api_key", "") or "").strip()}
    for (cfg,) in db.query(Bot.config).all():
        try:
            bot_keys.add(str(json.loads(cfg or "{}").get("posterchanai_api_key") or "").strip())
        except (ValueError, TypeError, AttributeError):
            continue
    bot_keys.discard("")
    if key in bot_keys:
        return True
    from app.utils.auth_utils import get_user_from_api_key, query_api_key_with_retry
    try:
        api_key, user_id = query_api_key_with_retry(db, key)
        user = get_user_from_api_key(db, user_id) if api_key and user_id else None
    except Exception:
        user = None
    if user is not None and user.is_admin:
        return True
    raise HTTPException(403, "Only the bots' key or an admin's key can read this")


router = APIRouter(prefix="/api/community", tags=["community"])


async def _read(coro):
    try:
        return await coro
    except Exception as e:
        # "Could not ask the relay" is never an empty answer: a bot that read [] would announce
        # nothing today and, on the next good read, every block of the last day at once.
        raise HTTPException(503, f"could not read the relay: {type(e).__name__}")


@router.get("/blocks")
async def blocks(since: int = 0, _auth: bool = Depends(get_bot_auth)):
    rows = await _read(community_stats.blocks())
    return {"blocks": [r for r in rows if r["at"] > since] if since else rows}


@router.get("/block-leaderboard")
async def block_leaderboard(min: int = 1, _auth: bool = Depends(get_bot_auth)):
    return {"leaderboard": await _read(community_stats.leaderboard(min))}


@router.get("/activity")
async def activity(top: int = 5, _auth: bool = Depends(get_bot_auth)):
    return await _read(community_stats.activity(top))
