"""Auto-split from the original telegram.py monolith. No behavior change."""
from ._common import Depends, HTTPException, Session, Setting, TelegramBotConfig, TelegramChatSetup, User, _configure_telegram, datetime, get_admin_user, get_current_user, get_db, logger, router, telegram_service, timedelta

_bot_username_cache = None
async def _get_bot_username(db):
    """The bot's @username (for building t.me deep links), cached after the first getMe."""
    global _bot_username_cache
    if _bot_username_cache:
        return _bot_username_cache
    if not getattr(telegram_service, "bot_token", None):
        row = db.query(Setting).filter(Setting.key == "telegram_bot_token").first()
        if row and row.value:
            telegram_service.set_token(row.value)
    try:
        r = await telegram_service.get_me()
        if r.get("ok"):
            _bot_username_cache = (r.get("result") or {}).get("username")
    except Exception:
        pass
    return _bot_username_cache


@router.get("/me")
async def get_bot_info(db: Session = Depends(get_db), admin: User = Depends(get_admin_user)):
    """Get information about the configured bot."""
    bot_token = db.query(Setting).filter(Setting.key == "telegram_bot_token").first()
    if not bot_token or not bot_token.value:
        raise HTTPException(status_code=400, detail="Telegram bot not configured")
    
    result = await telegram_service.get_me()
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to get bot info"))
    
    return result.get("result", {})


@router.post("/test")
async def test_telegram_connection(
    data: TelegramBotConfig,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Test Telegram bot connection."""
    if not data.bot_token:
        raise HTTPException(status_code=400, detail="Bot token required")
    
    telegram_service.set_token(data.bot_token)
    _configure_telegram(db)
    result = await telegram_service.get_me()

    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to connect to Telegram"))
    
    bot_info = result.get("result", {})
    return {
        "ok": True,
        "bot": {
            "id": bot_info.get("id"),
            "username": bot_info.get("username"),
            "first_name": bot_info.get("first_name")
        }
    }


@router.post("/test-local-api")
async def test_local_api(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Ping the configured local Bot API server (getMe) to verify it's reachable
    and the bot is registered there — useful before enabling local mode."""
    base = db.query(Setting).filter(Setting.key == "telegram_api_base").first()
    token = db.query(Setting).filter(Setting.key == "telegram_bot_token").first()
    if not base or not base.value:
        raise HTTPException(status_code=400, detail="Set and save the Bot API server URL first.")
    if not token or not token.value:
        raise HTTPException(status_code=400, detail="Telegram bot token not configured.")

    from app.services.telegram_service import TelegramService
    svc = TelegramService(token.value)
    svc.set_api_base(base.value)
    try:
        result = await svc.get_me()
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Could not reach {base.value}: {e}")
    if not result.get("ok"):
        detail = result.get("error") or result.get("description") or \
            "Reached the server, but the bot isn't registered there yet (run the setup script; after a cloud logOut it can take ~10 min)."
        raise HTTPException(status_code=400, detail=detail)

    info = result.get("result", {})
    return {
        "ok": True,
        "api_base": svc.api_root,
        "bot": {"username": info.get("username"), "first_name": info.get("first_name")},
    }


@router.post("/set-webhook")
async def configure_webhook(
    data: TelegramBotConfig,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Configure Telegram bot webhook."""
    logger.info(f"configure_webhook called with bot_token={'***' if data.bot_token else None}, webhook_url={data.webhook_url}")
    
    # First, save the token if provided
    if data.bot_token:
        setting = db.query(Setting).filter(Setting.key == "telegram_bot_token").first()
        if setting:
            setting.value = data.bot_token
        else:
            db.add(Setting(key="telegram_bot_token", value=data.bot_token))
        db.commit()
        telegram_service.set_token(data.bot_token)
    else:
        bot_token = db.query(Setting).filter(Setting.key == "telegram_bot_token").first()
        if not bot_token or not bot_token.value:
            raise HTTPException(status_code=400, detail="Telegram bot token not configured")
        telegram_service.set_token(bot_token.value)

    # Register the webhook with the local Bot API server when enabled, else cloud.
    _configure_telegram(db)

    if data.webhook_url:
        logger.info(f"Calling set_webhook with URL: {data.webhook_url}")
        result = await telegram_service.set_webhook(data.webhook_url)
        logger.info(f"set_webhook result: {result}")
    else:
        result = await telegram_service.delete_webhook()
    
    if not result.get("ok"):
        raise HTTPException(status_code=400, detail=result.get("error", "Failed to configure webhook"))
    
    return result


@router.get("/users")
async def list_telegram_users(
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """List users with Telegram enabled."""
    users = db.query(User).filter(
        User.telegram_enabled == True,
        User.telegram_chat_id.isnot(None)
    ).all()
    
    return [
        {
            "id": u.id,
            "username": u.username,
            "telegram_chat_id": u.telegram_chat_id,
            "telegram_notifications": u.telegram_notifications
        }
        for u in users
    ]


@router.post("/generate-key")
async def generate_telegram_key(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Generate a one-time key the user sends to the bot via /start to link their account."""
    import secrets
    from datetime import datetime, timedelta
    previous_key_revoked = bool(current_user.telegram_key)
    key = secrets.token_urlsafe(32)
    current_user.telegram_key = key
    current_user.telegram_key_expires_at = datetime.utcnow() + timedelta(hours=24)
    db.commit()
    db.refresh(current_user)
    bot_username = await _get_bot_username(db)
    return {
        "ok": True,
        "key": key,
        "expires_at": current_user.telegram_key_expires_at.isoformat(),
        "previous_key_revoked": previous_key_revoked,
        "bot_username": bot_username,
        # Telegram deep link: tapping it opens the bot with the key pre-filled, so the user just
        # taps Start to link (no copy/paste of /start <key>). Null if the bot username is unknown.
        "deep_link": (f"https://t.me/{bot_username}?start={key}" if bot_username else None),
    }


@router.delete("/generate-key")
async def revoke_telegram_key(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Revoke (clear) the pending Telegram link key."""
    current_user.telegram_key = None
    current_user.telegram_key_expires_at = None
    db.commit()
    db.refresh(current_user)
    return {"ok": True}


@router.post("/link")
async def link_telegram_chat(
    data: TelegramChatSetup,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Link current user's account to a Telegram chat."""
    from sqlalchemy.exc import IntegrityError
    current_user.telegram_chat_id = data.chat_id
    current_user.telegram_enabled = True
    current_user.telegram_notifications = data.notifications
    try:
        db.commit()
    except IntegrityError:
        db.rollback()
        raise HTTPException(status_code=400, detail="That Telegram chat ID is already linked to another account.")
    return {"ok": True, "message": f"Linked to chat {data.chat_id}"}


@router.post("/unlink")
async def unlink_telegram_chat(
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Unlink current user's Telegram account."""
    current_user.telegram_enabled = False
    current_user.telegram_chat_id = None
    current_user.telegram_notifications = ""
    current_user.telegram_key = None
    current_user.telegram_key_expires_at = None

    db.commit()
    
    return {"ok": True, "message": "Telegram account unlinked"}


@router.post("/broadcast")
async def broadcast_to_telegram_users(
    message: str,
    db: Session = Depends(get_db),
    admin: User = Depends(get_admin_user)
):
    """Broadcast a message to all users with Telegram enabled."""
    bot_token = db.query(Setting).filter(Setting.key == "telegram_bot_token").first()
    if not bot_token or not bot_token.value:
        raise HTTPException(status_code=400, detail="Telegram bot not configured")
    
    telegram_service.set_token(bot_token.value)
    _configure_telegram(db)

    users = db.query(User).filter(
        User.telegram_enabled == True,
        User.telegram_chat_id.isnot(None)
    ).all()
    
    results = []
    for user in users:
        try:
            result = await telegram_service.send_message(user.telegram_chat_id, message)
            results.append({"user_id": user.id, "ok": result.get("ok", False)})
        except Exception as e:
            logger.error(f"Failed to send message to user {user.id}: {e}")
            results.append({"user_id": user.id, "ok": False, "error": str(e)})
    
    return {"results": results}
