import logging
import httpx
import asyncio
import os
from typing import Optional

logger = logging.getLogger(__name__)

# Telegram rejects messages longer than 4096 chars; split with a safety margin.
_TELEGRAM_MSG_LIMIT = 4000

# Telegram rejects media captions (photo/video/audio/document) longer than 1024 chars.
_TELEGRAM_CAPTION_LIMIT = 1024


def _clamp_caption(caption):
    """Truncate a media caption to Telegram's 1024-char limit so the send doesn't fail with
    'message caption is too long' — the media still goes out, with a trimmed caption."""
    if caption and len(caption) > _TELEGRAM_CAPTION_LIMIT:
        return caption[:_TELEGRAM_CAPTION_LIMIT - 1].rstrip() + "…"
    return caption


def _split_for_telegram(text: str, limit: int = _TELEGRAM_MSG_LIMIT) -> list[str]:
    """Split text into <=limit-char pieces, preferring newline/space boundaries."""
    chunks = []
    remaining = text
    while len(remaining) > limit:
        cut = remaining.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = remaining.rfind(" ", 0, limit)
        if cut < limit // 2:
            cut = limit
        chunks.append(remaining[:cut])
        remaining = remaining[cut:].lstrip("\n")
    if remaining:
        chunks.append(remaining)
    return chunks


class TelegramService:
    DEFAULT_API_ROOT = "https://api.telegram.org"

    def __init__(self, bot_token: str = None):
        self.bot_token = bot_token
        self.api_root = self.DEFAULT_API_ROOT
        self.api_base = f"{self.api_root}/bot"

    def set_token(self, token: str):
        self.bot_token = token

    def set_api_base(self, api_root: str = None):
        """Point the service at a Bot API server.

        `api_root` is the server root, e.g. "https://api.telegram.org" (cloud,
        20 MB file cap) or "http://localhost:8081" (local Bot API server, ~2 GB).
        Falls back to the cloud API when empty.
        """
        root = (api_root or self.DEFAULT_API_ROOT).strip().rstrip("/")
        self.api_root = root
        self.api_base = f"{root}/bot"

    @property
    def is_local_api(self) -> bool:
        return self.api_root != self.DEFAULT_API_ROOT
    
    async def send_message(self, chat_id: str, text: str, parse_mode: str = "Markdown", reply_markup: dict = None, disable_web_page_preview: bool = False) -> dict:
        """Send a message to a Telegram chat. Long text (e.g. full-page translations)
        is split into multiple messages, since Telegram rejects >4096 chars.
        Set `disable_web_page_preview` to suppress the auto link-preview card (e.g. for
        a list of pinned URLs)."""
        if not self.bot_token:
            logger.warning("Telegram bot token not configured")
            return {"ok": False, "error": "Bot token not configured"}

        if text and len(text) > _TELEGRAM_MSG_LIMIT:
            parts = _split_for_telegram(text)
            result = {"ok": True}
            for i, part in enumerate(parts):
                # Keep the keyboard only on the final chunk.
                result = await self.send_message(
                    chat_id, part, parse_mode=parse_mode,
                    reply_markup=reply_markup if i == len(parts) - 1 else None,
                    disable_web_page_preview=disable_web_page_preview,
                )
                await asyncio.sleep(0.1)  # avoid Telegram flood limits
            return result

        url = f"{self.api_base}{self.bot_token}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": text,
            "parse_mode": parse_mode
        }
        if disable_web_page_preview:
            payload["disable_web_page_preview"] = True
        if reply_markup:
            payload["reply_markup"] = reply_markup

        try:
            async with httpx.AsyncClient(timeout=30.0) as client:
                response = await client.post(url, json=payload)
                result = response.json()
                if not result.get("ok"):
                    # If markdown parsing failed, retry without parse_mode
                    error_desc = result.get("description", "")
                    if "can't parse entities" in error_desc and parse_mode:
                        logger.warning(f"Telegram markdown parse error, retrying without formatting: {error_desc}")
                        payload_plain = {
                            "chat_id": chat_id,
                            "text": text
                            # No parse_mode = plain text
                        }
                        if disable_web_page_preview:
                            payload_plain["disable_web_page_preview"] = True
                        if reply_markup:
                            payload_plain["reply_markup"] = reply_markup
                        response = await client.post(url, json=payload_plain)
                        result = response.json()
                        if not result.get("ok"):
                            logger.error(f"Telegram API error (plain text fallback): {result}")
                        return result
                    logger.error(f"Telegram API error: {result}")
                return result
        except Exception as e:
            logger.error(f"Failed to send Telegram message: {e}")
            return {"ok": False, "error": str(e)}
    
    async def send_photo(self, chat_id: str, photo_data: str, caption: str = None, reply_markup: dict = None, parse_mode: str = "Markdown") -> dict:
        """Send a photo to a Telegram chat. Can accept URL or base64 data.

        `parse_mode` defaults to Markdown for backward compatibility; pass "" to send the
        caption as plain text (e.g. when it contains a bare URL whose `_` would otherwise
        be mangled by Markdown — Telegram still auto-links bare URLs in plain mode)."""
        import base64
        import tempfile
        import os
        import json
        
        if not self.bot_token:
            logger.warning("Telegram bot token not configured")
            return {"ok": False, "error": "Bot token not configured"}

        caption = _clamp_caption(caption)
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
                with tempfile.NamedTemporaryFile(prefix="tg_png_", suffix=".png", delete=False) as tmp:
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
                        if parse_mode:
                            data["parse_mode"] = parse_mode
                    if reply_markup:
                        data["reply_markup"] = json.dumps(reply_markup)
                    response = await client.post(url, data=data, files=files)
                else:
                    # Send as URL
                    payload = {
                        "chat_id": chat_id,
                        "photo": photo_data,
                    }
                    if caption:
                        payload["caption"] = caption
                        if parse_mode:
                            payload["parse_mode"] = parse_mode
                    if reply_markup:
                        payload["reply_markup"] = reply_markup
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
        
        caption = _clamp_caption(caption)
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
    
    async def send_document_bytes(self, chat_id: str, file_bytes, filename: str, caption: str = None,
                                  content_type: str = "application/pdf") -> dict:
        """Send a document from raw bytes to a Telegram chat (multipart upload).

        content_type defaults to PDF for back-compat; pass image/png for screenshots so
        Telegram shows an inline image preview (and full-resolution tap-to-open).
        """
        if not self.bot_token:
            return {"ok": False, "error": "Bot token not configured"}
        caption = _clamp_caption(caption)
        url = f"{self.api_base}{self.bot_token}/sendDocument"
        data = {"chat_id": chat_id}
        if caption:
            data["caption"] = caption
        if hasattr(file_bytes, 'read'):
            file_content = file_bytes.read()
        else:
            file_content = bytes(file_bytes)
        try:
            async with httpx.AsyncClient(timeout=60.0) as client:
                response = await client.post(url, data=data, files={"document": (filename, file_content, content_type)})
                result = response.json()
                if not result.get("ok"):
                    logger.error(f"Telegram sendDocument error: {result}")
                return result
        except Exception as e:
            logger.error(f"Failed to send Telegram document bytes: {e}")
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
                data = response.json()
                # Editing a message to identical content is a no-op, not a failure —
                # Telegram returns 400 "message is not modified". Treat it as success.
                if not data.get("ok") and "not modified" in str(data.get("description", "")).lower():
                    return {"ok": True, "not_modified": True}
                return data
        except Exception as e:
            logger.error(f"Failed to edit message: {e}")
            return {"ok": False, "error": str(e)}

    async def edit_message_media_photo(self, chat_id: str, message_id: int, photo_bytes: bytes,
                                       caption: str = None, reply_markup: dict = None,
                                       parse_mode: str = "") -> dict:
        """Replace an existing message's photo in place (editMessageMedia) with new PNG bytes.
        Used for the flashcards image-card navigation (flip / next / prev)."""
        import json
        if not self.bot_token:
            return {"ok": False, "error": "Bot token not configured"}
        url = f"{self.api_base}{self.bot_token}/editMessageMedia"
        media = {"type": "photo", "media": "attach://photo"}
        if caption:
            media["caption"] = _clamp_caption(caption)
            if parse_mode:
                media["parse_mode"] = parse_mode
        data = {"chat_id": str(chat_id), "message_id": str(message_id), "media": json.dumps(media)}
        if reply_markup:
            data["reply_markup"] = json.dumps(reply_markup)
        try:
            async with httpx.AsyncClient(timeout=120.0) as client:
                response = await client.post(
                    url, data=data, files={"photo": ("card.png", photo_bytes, "image/png")})
                result = response.json()
                if not result.get("ok") and "not modified" in str(result.get("description", "")).lower():
                    return {"ok": True, "not_modified": True}
                return result
        except Exception as e:
            logger.error(f"Failed to edit message media: {e}")
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
        """Download a file from Telegram (cloud or local Bot API server)."""
        if not self.bot_token:
            return None

        # A local Bot API server in --local mode returns an absolute filesystem
        # path from getFile (files are served off disk, not over HTTP). Read it
        # directly. (posterchanai and the daemon share the host/filesystem.)
        if file_path.startswith("/"):
            try:
                return await asyncio.to_thread(lambda: open(file_path, "rb").read())
            except Exception as e:
                logger.error(f"Failed to read local Bot API file {file_path}: {e}")
                return None

        url = f"{self.api_root}/file/bot{self.bot_token}/{file_path}"

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

    async def send_audio(self, chat_id: str, file_path: str, title: str = None, performer: str = None, duration: int = None, caption: str = None) -> dict:
        """Send an audio file to a Telegram chat."""
        if not self.bot_token:
            return {"ok": False, "error": "Bot token not configured"}

        caption = _clamp_caption(caption)
        url = f"{self.api_base}{self.bot_token}/sendAudio"

        try:
            with open(file_path, "rb") as f:
                audio_bytes = f.read()

            data = {"chat_id": chat_id}
            if title:
                data["title"] = title
            if performer:
                data["performer"] = performer
            if duration is not None:
                data["duration"] = str(duration)
            if caption:
                data["caption"] = caption

            files = {"audio": (title or "audio.mp3", audio_bytes, "audio/mpeg")}

            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.post(url, data=data, files=files)
                result = response.json()
                if not result.get("ok"):
                    logger.error(f"Telegram sendAudio error: {result}")
                return result
        except Exception as e:
            logger.error(f"Failed to send audio: {e}")
            return {"ok": False, "error": str(e)}

    async def send_video(self, chat_id: str, file_path: str, caption: str = None, duration: int = None, width: int = None, height: int = None) -> dict:
        """Send a video file to a Telegram chat.

        Args:
            chat_id: Telegram chat ID
            file_path: Path to the video file
            caption: Optional caption for the video
            duration: Video duration in seconds
            width: Video width in pixels
            height: Video height in pixels
        """
        if not self.bot_token:
            return {"ok": False, "error": "Bot token not configured"}

        # Validate file exists and is readable
        if not os.path.exists(file_path):
            logger.error(f"send_video: File does not exist: {file_path}")
            return {"ok": False, "error": f"File not found: {file_path}"}

        file_size = os.path.getsize(file_path)
        logger.info(f"send_video: Sending file {file_path}, size={file_size} bytes, chat_id={chat_id}")

        caption = _clamp_caption(caption)
        url = f"{self.api_base}{self.bot_token}/sendVideo"

        try:
            with open(file_path, "rb") as f:
                video_bytes = f.read()

            # Determine content type from file extension
            ext = os.path.splitext(file_path)[1].lower()
            content_type = "video/mp4" if ext == ".mp4" else "video/webm" if ext == ".webm" else "video/x-matroska" if ext == ".mkv" else "video/mp4"

            filename = os.path.basename(file_path)

            data = {"chat_id": chat_id, "supports_streaming": "true"}
            if caption:
                data["caption"] = caption
                data["parse_mode"] = "Markdown"
            if duration is not None:
                data["duration"] = str(duration)
            if width is not None:
                data["width"] = str(width)
            if height is not None:
                data["height"] = str(height)

            files = {"video": (filename, video_bytes, content_type)}
            logger.debug(f"send_video: POST to {url.replace(self.bot_token, '***')} with data={data}")

            async with httpx.AsyncClient(timeout=300.0) as client:
                response = await client.post(url, data=data, files=files)
                try:
                    result = response.json()
                except Exception as json_err:
                    # If JSON parsing fails, capture the raw response
                    raw_text = response.text[:500] if response.text else "<empty>"
                    logger.error(f"Telegram sendVideo JSON parse error: {json_err}, status={response.status_code}, body={raw_text}")
                    return {"ok": False, "error": f"HTTP {response.status_code}: {raw_text}"}
                if not result.get("ok"):
                    logger.error(f"Telegram sendVideo error: {result}")
                return result
        except Exception as e:
            logger.error(f"Failed to send video: {e}", exc_info=True)
            return {"ok": False, "error": str(e)}


telegram_service = TelegramService()


def configure_from_settings(db) -> None:
    """Point the shared telegram_service at the local Bot API server if the admin
    enabled one, else the cloud API. Call this anywhere the service is used so
    every Bot API call (webhook setup, sends, file ops) targets the same server.
    """
    from app.services import settings_store
    local = settings_store.get("telegram_local_api", "")
    base = settings_store.get("telegram_api_base", "")
    if str(local).lower() in ("true", "1", "yes") and base:
        telegram_service.set_api_base(base)
    else:
        telegram_service.set_api_base(None)
