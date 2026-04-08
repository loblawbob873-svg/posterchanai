import logging
import httpx
import asyncio
from typing import Optional, List
from datetime import datetime

logger = logging.getLogger(__name__)


class TelegramService:
    def __init__(self, bot_token: str = None):
        self.bot_token = bot_token
        self.api_base = "https://api.telegram.org/bot"
    
    def set_token(self, token: str):
        self.bot_token = token
    
    async def send_message(self, chat_id: str, text: str, parse_mode: str = "Markdown") -> dict:
        """Send a message to a Telegram chat."""
        if not self.bot_token:
            logger.warning("Telegram bot token not configured")
            return {"ok": False, "error": "Bot token not configured"}
        
        url = f"{self.api_base}{self.bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode
        }
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=payload)
                result = response.json()
                if not result.get("ok"):
                    logger.error(f"Telegram API error: {result}")
                return result
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return {"ok": False, "error": str(e)}
    
    async def send_photo(self, chat_id: str, photo_data: str, caption: str = None) -> dict:
        """Send a photo to a Telegram chat. Can accept URL or base64 data."""
        import base64
        import tempfile
        import os
        
        if not self.bot_token:
            logger.warning("Telegram bot token not configured")
            return {"ok": False, "error": "Bot token not configured"}
        
        url = f"{self.api_base}{self.bot_token}/sendPhoto"
        
        # Determine if photo_data is base64 or a URL
        if photo_data.startswith("data:image"):
            # Handle data URL format (data:image/png;base64,...)
            photo_data = photo_data.split(",", 1)[1]
        
        if photo_data.startswith("/") or photo_data.startswith("."):
            # It's a file path
            with open(photo_data, "rb") as f:
                photo_bytes = f.read()
        elif len(photo_data) > 200 and not photo_data.startswith("http"):
            # Probably base64 - try to decode and save to temp file
            try:
                image_bytes = base64.b64decode(photo_data)
                with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as tmp:
                    tmp.write(image_bytes)
                    tmp_path = tmp.name
                
                try:
                    with open(tmp_path, "rb") as f:
                        photo_bytes = f.read()
                finally:
                    os.unlink(tmp_path)
            except Exception as e:
                logger.error(f"Failed to decode base64 image: {e}")
                return {"ok": False, "error": f"Failed to decode image: {e}"}
        else:
            # Assume it's a URL - Telegram will download it
            photo_bytes = None
        
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                if photo_bytes:
                    # Send as multipart file
                    files = {"photo": ("image.png", photo_bytes, "image/png")}
                    data = {"chat_id": chat_id}
                    if caption:
                        data["caption"] = caption
                    response = await client.post(url, data=data, files=files)
                else:
                    # Send as URL
                    payload = {
                        "chat_id": chat_id,
                        "photo": photo_data,
                    }
                    if caption:
                        payload["caption"] = caption
                    response = await client.post(url, json=payload)
                
                result = response.json()
                if not result.get("ok"):
                    logger.error(f"Telegram API error: {result}")
                return result
        except Exception as e:
            logger.error(f"Failed to send Telegram photo: {e}")
            return {"ok": False, "error": str(e)}
    
    async def send_document(self, chat_id: str, document_url: str, caption: str = None) -> dict:
        """Send a document to a Telegram chat."""
        if not self.bot_token:
            logger.warning("Telegram bot token not configured")
            return {"ok": False, "error": "Bot token not configured"}
        
        url = f"{self.api_base}{self.bot_token}/sendDocument"
        payload = {
            "chat_id": chat_id,
            "document": document_url,
        }
        if caption:
            payload["caption"] = caption
        
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(url, json=payload)
                result = response.json()
                if not result.get("ok"):
                    logger.error(f"Telegram API error: {result}")
                return result
        except Exception as e:
            logger.error(f"Failed to send Telegram document: {e}")
            return {"ok": False, "error": str(e)}
    
    async def set_webhook(self, webhook_url: str) -> dict:
        """Set the webhook for the bot."""
        if not self.bot_token:
            return {"ok": False, "error": "Bot token not configured"}
        
        url = f"{self.api_base}{self.bot_token}/setWebhook"
        payload = {"url": webhook_url}
        
        logger.info(f"Setting Telegram webhook to: {webhook_url}")
        logger.info(f"Telegram API URL: {url}")
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=payload)
                result = response.json()
                logger.info(f"Telegram setWebhook response: {result}")
                return result
        except Exception as e:
            logger.error(f"Failed to set Telegram webhook: {e}")
            return {"ok": False, "error": str(e)}
    
    async def delete_webhook(self) -> dict:
        """Delete the webhook for the bot."""
        if not self.bot_token:
            return {"ok": False, "error": "Bot token not configured"}
        
        url = f"{self.api_base}{self.bot_token}/deleteWebhook"
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url)
                return response.json()
        except Exception as e:
            logger.error(f"Failed to delete Telegram webhook: {e}")
            return {"ok": False, "error": str(e)}
    
    async def get_me(self) -> dict:
        """Get bot information."""
        if not self.bot_token:
            return {"ok": False, "error": "Bot token not configured"}
        
        url = f"{self.api_base}{self.bot_token}/getMe"
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.get(url)
                return response.json()
        except Exception as e:
            logger.error(f"Failed to get Telegram bot info: {e}")
            return {"ok": False, "error": str(e)}
    
    async def get_updates(self, limit: int = 100, offset: int = None) -> dict:
        """Get bot updates."""
        if not self.bot_token:
            return {"ok": False, "error": "Bot token not configured"}
        
        url = f"{self.api_base}{self.bot_token}/getUpdates"
        payload = {"limit": limit}
        if offset:
            payload["offset"] = offset
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=payload)
                return response.json()
        except Exception as e:
            logger.error(f"Failed to get Telegram updates: {e}")
            return {"ok": False, "error": str(e)}
    
    async def answer_callback_query(self, callback_query_id: str, text: str = None, show_alert: bool = False) -> dict:
        """Answer a callback query from an inline button."""
        if not self.bot_token:
            return {"ok": False, "error": "Bot token not configured"}
        
        url = f"{self.api_base}{self.bot_token}/answerCallbackQuery"
        payload = {
            "callback_query_id": callback_query_id,
            "show_alert": show_alert
        }
        if text:
            payload["text"] = text
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=payload)
                return response.json()
        except Exception as e:
            logger.error(f"Failed to answer callback query: {e}")
            return {"ok": False, "error": str(e)}
    
    async def edit_message_text(self, chat_id: str, message_id: int, text: str, parse_mode: str = "Markdown", reply_markup: dict = None) -> dict:
        """Edit an existing message."""
        if not self.bot_token:
            return {"ok": False, "error": "Bot token not configured"}
        
        url = f"{self.api_base}{self.bot_token}/editMessageText"
        payload = {
            "chat_id": chat_id,
            "message_id": message_id,
            "text": text,
            "parse_mode": parse_mode
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=payload)
                return response.json()
        except Exception as e:
            logger.error(f"Failed to edit message: {e}")
            return {"ok": False, "error": str(e)}
    
    async def get_file(self, file_id: str) -> dict:
        """Get file information from Telegram."""
        if not self.bot_token:
            return {"ok": False, "error": "Bot token not configured"}
        
        url = f"{self.api_base}{self.bot_token}/getFile"
        payload = {"file_id": file_id}
        
        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=payload)
                return response.json()
        except Exception as e:
            logger.error(f"Failed to get file: {e}")
            return {"ok": False, "error": str(e)}
    
    async def download_file(self, file_path: str) -> Optional[bytes]:
        """Download a file from Telegram."""
        if not self.bot_token:
            return None
        
        url = f"https://api.telegram.org/file/bot{self.bot_token}/{file_path}"
        
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.get(url)
                if response.status_code == 200:
                    return response.content
                else:
                    logger.error(f"Failed to download file: {response.status_code}")
                    return None
        except Exception as e:
            logger.error(f"Failed to download file: {e}")
            return None


telegram_service = TelegramService()
