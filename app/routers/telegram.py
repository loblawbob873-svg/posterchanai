from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
import logging
import json
from datetime import datetime

from app.database import get_db
from app.models import User, Setting
from app.auth import get_current_user, get_admin_user
from app.services.telegram_service import telegram_service

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/api/telegram", tags=["telegram"])


class TelegramWebhookUpdate(BaseModel):
    update_id: int
    message: Optional[dict] = None
    callback_query: Optional[dict] = None
    edited_message: Optional[dict] = None
    inline_query: Optional[dict] = None
    chosen_inline_result: Optional[dict] = None


# Allow any incoming dict for the webhook
class TelegramUpdate(BaseModel):
    update_id: int
    message: Optional[dict] = None
    callback_query: Optional[dict] = None
    edited_message: Optional[dict] = None
    inline_query: Optional[dict] = None
    chosen_inline_result: Optional[dict] = None
    my_chat_member: Optional[dict] = None
    chat_member: Optional[dict] = None
    
    class Config:
        extra = "allow"


class TelegramBotConfig(BaseModel):
    bot_token: Optional[str] = None
    webhook_url: Optional[str] = None
    enabled: bool = False


class TelegramChatSetup(BaseModel):
    chat_id: str
    notifications: str = "news,downloads,mentions"


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


@router.post("/webhook")
async def telegram_webhook(update: dict, db: Session = Depends(get_db)):
    """Handle incoming webhook updates from Telegram."""
    logger.info(f"Received Telegram webhook update: {update}")
    try:
        from app.services.chat_service import ChatService
        
        bot_token = db.query(Setting).filter(Setting.key == "telegram_bot_token").first()
        if not bot_token or not bot_token.value:
            logger.warning("Telegram bot not configured")
            return {"ok": False, "error": "Bot not configured"}
        
        telegram_service.set_token(bot_token.value)
        
        message = update.get("message")
        if message:
            chat_id = str(message.get("chat", {}).get("id"))
            text = message.get("text", "")
            user = message.get("from", {})
            username = user.get("username", "unknown")
            
            logger.info(f"Received Telegram message from {username} (chat_id: {chat_id}): {text}")
            
            # Find user by linked Telegram chat_id
            user_obj = db.query(User).filter(
                User.telegram_chat_id == chat_id,
                User.telegram_enabled == True
            ).first()
            
            logger.info(f"Found user: {user_obj.username if user_obj else 'None'}")
            
            if not user_obj:
                # Check if this is a /start command with a verification code
                if text.startswith("/start "):
                    code = text.replace("/start ", "").strip()
                    # Handle verification code if implemented
                    await telegram_service.send_message(
                        chat_id,
                        "Your Telegram account is not linked. Please enable Telegram in your account settings."
                    )
                    return {"ok": True}
                
                await telegram_service.send_message(
                    chat_id,
                    "Your Telegram account is not linked to any PosterChanAI user. Please enable Telegram in your account settings."
                )
                return {"ok": True}
            
            # Process the message through chat service
            chat_service = ChatService(db, user=user_obj)
            
            # Build messages for the chat
            messages = [
                {"role": "system", "content": chat_service.system_prompt},
                {"role": "user", "content": text}
            ]
            
            # Send user message and get AI response
            result = await chat_service.chat(messages)
            logger.info(f"chat_service.chat result type: {type(result)}, is dict: {isinstance(result, dict)}")
            
            # Check if result is a dict (with type and content) or just a string
            if isinstance(result, dict):
                response_type = result.get("type", "text")
                response_content = result.get("content", "")
                image_data = result.get("image")  # base64 image if generated
                logger.info(f"Result type: {response_type}, has image: {bool(image_data)}")
                
                if response_type == "generated_image" and image_data:
                    logger.info(f"Generated image detected, sending via Telegram, image length: {len(image_data)}")
                    # Send the image to Telegram
                    photo_result = await telegram_service.send_photo(chat_id, image_data, response_content)
                    if not photo_result.get("ok"):
                        logger.error(f"Failed to send photo: {photo_result}")
                        await telegram_service.send_message(chat_id, f"{response_content}\n\n(Image generation failed to send)")
                    return {"ok": True}
                else:
                    await telegram_service.send_message(chat_id, response_content)
            else:
                logger.info(f"AI response: {result[:100]}...")
                # Send the AI response back to Telegram
                await telegram_service.send_message(chat_id, result)
            
            return {"ok": True}
        
        callback_query = update.get("callback_query")
        if callback_query:
            # Handle inline button callbacks
            chat_id = str(callback_query.get("message", {}).get("chat", {}).get("id"))
            data = callback_query.get("data", "")
            callback_query_id = callback_query.get("id")
            
            logger.info(f"Received Telegram callback query: {data}")
            
            # Answer the callback query
            await telegram_service.answer_callback_query(callback_query_id)
            
            return {"ok": True}
        
        return {"ok": True}
    
    except Exception as e:
        logger.error(f"Error processing Telegram webhook: {e}", exc_info=True)
        return {"ok": False, "error": str(e)}


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


@router.post("/link")
async def link_telegram_chat(
    data: TelegramChatSetup,
    current_user: User = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Link current user's account to a Telegram chat."""
    current_user.telegram_chat_id = data.chat_id
    current_user.telegram_enabled = True
    current_user.telegram_notifications = data.notifications
    
    db.commit()
    
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
