"""Admin API for managed bots (the merged ~/posterchan framework).

CRUD over the `Bot` table plus runtime actions (On/Off via `enabled`, restart, status). The
actual process lifecycle lives in app/services/bot_manager_service.py; these endpoints just
edit rows and nudge the manager to reconcile. Admin-gated like app/routers/admin.py.
"""

import json
import logging
from typing import List, Optional, Dict, Any

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from sqlalchemy.exc import IntegrityError
from pydantic import BaseModel

import httpx

from app.database import get_db
from app.models import Bot, User
from app.auth import get_admin_user
from app.services import bot_manager_service, pleroma_service, misskey_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/admin/bots", tags=["bots"])


class BotPayload(BaseModel):
    name: str
    enabled: bool = True
    bot_type: str = "text"          # "text" | "image"
    platform: str = "misskey"       # "misskey" | "pleroma" | "matrix"
    host: Optional[str] = ""        # node hostname; empty = any node
    modes: Optional[str] = ""       # comma-separated main.py flags
    config: Dict[str, Any] = {}     # all other per-bot fields (creds, prompt, feature opts)


class OAuthTokenPayload(BaseModel):
    platform: str = "pleroma"       # "pleroma" (OAuth password grant) | "misskey" (/api/signin)
    server: str                     # instance URL, e.g. https://poster.place
    username: str                   # bot account login (handle, no leading @)
    password: str
    totp: str = ""                  # optional 2FA code (Misskey only)
    scopes: str = "read write follow push"


class BotUpdate(BaseModel):
    name: Optional[str] = None
    enabled: Optional[bool] = None
    bot_type: Optional[str] = None
    platform: Optional[str] = None
    host: Optional[str] = None
    modes: Optional[str] = None
    config: Optional[Dict[str, Any]] = None


def _serialize(bot: Bot) -> dict:
    try:
        cfg = json.loads(bot.config) if bot.config else {}
    except (ValueError, TypeError):
        cfg = {}
    return {
        "id": bot.id,
        "name": bot.name,
        "enabled": bool(bot.enabled),
        "bot_type": bot.bot_type,
        "platform": bot.platform,
        "host": bot.host or "",
        "modes": bot.modes or "",
        "config": cfg,
    }


@router.get("")
def list_bots(db: Session = Depends(get_db), admin: User = Depends(get_admin_user)):
    return [_serialize(b) for b in db.query(Bot).order_by(Bot.name).all()]


@router.get("/status")
def bots_status(admin: User = Depends(get_admin_user)):
    """Live runtime status (running/pid/restarts) merged with DB rows."""
    return bot_manager_service.get_status()


@router.post("/oauth/token")
async def mint_oauth_token(payload: OAuthTokenPayload, admin: User = Depends(get_admin_user)):
    """Mint a fedi access token from the bot account's username/password so an admin can
    connect a bot in the UI without running a script or a browser auth flow. Pleroma uses the
    OAuth password grant; Misskey uses /api/signin. The token is returned for the caller to
    save into the bot's config; nothing is persisted here."""
    server = (payload.server or "").strip()
    username = (payload.username or "").lstrip("@").strip()
    if not server or not username or not payload.password:
        raise HTTPException(status_code=400, detail="Server, username and password are required")
    try:
        if payload.platform == "misskey":
            token = await misskey_service.password_signin(
                server, username, payload.password, token=payload.totp.strip(),
            )
        else:
            token = await pleroma_service.password_grant(
                server, username, payload.password, scopes=payload.scopes,
            )
    except httpx.HTTPStatusError as e:
        body = e.response.text[:300] if e.response is not None else ""
        raise HTTPException(status_code=400,
                            detail=f"Instance rejected the request ({e.response.status_code if e.response is not None else '?'}): {body}")
    except (httpx.HTTPError, ValueError) as e:
        raise HTTPException(status_code=400, detail=f"Could not mint token: {e}")
    return {"access_token": token}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_bot(payload: BotPayload, db: Session = Depends(get_db),
               admin: User = Depends(get_admin_user)):
    bot = Bot(
        name=payload.name.strip(),
        enabled=payload.enabled,
        bot_type=payload.bot_type,
        platform=payload.platform,
        host=(payload.host or "").strip(),
        modes=(payload.modes or "").strip(),
        config=json.dumps(payload.config or {}),
    )
    db.add(bot)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail=f"A bot named '{payload.name}' already exists")
    db.refresh(bot)
    bot_manager_service.reconcile_now()
    return _serialize(bot)


@router.put("/{bot_id}")
def update_bot(bot_id: int, payload: BotUpdate, db: Session = Depends(get_db),
               admin: User = Depends(get_admin_user)):
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    if payload.name is not None:
        bot.name = payload.name.strip()
    if payload.enabled is not None:
        bot.enabled = payload.enabled
    if payload.bot_type is not None:
        bot.bot_type = payload.bot_type
    if payload.platform is not None:
        bot.platform = payload.platform
    if payload.host is not None:
        bot.host = payload.host.strip()
    if payload.modes is not None:
        bot.modes = payload.modes.strip()
    if payload.config is not None:
        bot.config = json.dumps(payload.config)
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="Bot name must be unique")
    db.refresh(bot)
    # config/cred/mode changes need a respawn; nudge a reconcile and restart the running child.
    bot_manager_service.restart_bot(bot.name)
    return _serialize(bot)


@router.delete("/{bot_id}")
def delete_bot(bot_id: int, db: Session = Depends(get_db),
               admin: User = Depends(get_admin_user)):
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    name = bot.name
    db.delete(bot)
    db.commit()
    bot_manager_service.reconcile_now()  # manager stops the now-absent child
    return {"status": "deleted", "name": name}


@router.post("/{bot_id}/start")
def start_bot(bot_id: int, db: Session = Depends(get_db),
              admin: User = Depends(get_admin_user)):
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    bot.enabled = True
    db.commit()
    bot_manager_service.reconcile_now()
    return {"status": "started", "name": bot.name}


@router.post("/{bot_id}/stop")
def stop_bot(bot_id: int, db: Session = Depends(get_db),
             admin: User = Depends(get_admin_user)):
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    bot.enabled = False
    db.commit()
    bot_manager_service.reconcile_now()
    return {"status": "stopped", "name": bot.name}


@router.post("/{bot_id}/restart")
def restart_bot(bot_id: int, db: Session = Depends(get_db),
                admin: User = Depends(get_admin_user)):
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    if not bot.enabled:
        bot.enabled = True
        db.commit()
    bot_manager_service.restart_bot(bot.name)
    return {"status": "restarted", "name": bot.name}


@router.post("/{bot_id}/test-post/preview")
async def test_post_preview(bot_id: int, db: Session = Depends(get_db),
                            admin: User = Depends(get_admin_user)):
    """Generate from the bot's SAVED config and return it WITHOUT publishing.
    Text bots → the generated post text; image bots → a generated image (base64)."""
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")

    if bot.bot_type == "image":
        try:
            cfg = json.loads(bot.config) if bot.config else {}
        except (ValueError, TypeError):
            cfg = {}
        prompt = (cfg.get("prompt") or "").strip()
        if not prompt:
            return {"ok": False, "error": "This image bot has no prompt set."}
        # Mirror imageposter: if random scenes are on, append one so the preview matches posts.
        if cfg.get("random_scenes"):
            scene = bot_manager_service.random_scene()
            if scene:
                prompt = f"{prompt}, {scene}"
        from app.services.image_factory import generate_image_with_load_balancing
        img = await generate_image_with_load_balancing(db=db, prompt=prompt)
        if img:
            return {"ok": True, "image": img}
        return {"ok": False, "error": "Image generation failed (check image servers)."}

    # Text bots: the preview spawns a blocking subprocess — run it off the event loop.
    import asyncio
    return await asyncio.get_event_loop().run_in_executor(None, bot_manager_service.preview_post, bot.name)


@router.post("/{bot_id}/test-post/publish")
def test_post_publish(bot_id: int, db: Session = Depends(get_db),
                      admin: User = Depends(get_admin_user)):
    """Fire one real post now from the bot's SAVED config, bypassing the schedule
    (Test → Publish now in the editor)."""
    bot = db.query(Bot).filter(Bot.id == bot_id).first()
    if not bot:
        raise HTTPException(status_code=404, detail="Bot not found")
    return bot_manager_service.publish_post(bot.name)
