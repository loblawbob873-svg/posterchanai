"""Auto-split from the original telegram.py monolith. No behavior change."""
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Optional
import logging
import re
import asyncio
import time
from datetime import datetime, timedelta
_MEDIA_GROUP_CACHE: dict = {}
from app.database import get_db, SessionLocal
from app.models import User, Conversation, Message
from app.auth import get_current_user, get_admin_user
from app.services.telegram_service import telegram_service, configure_from_settings as _configure_telegram
from app.services.chat_service import ChatService
from app.services.command_service import CommandService
logger = logging.getLogger(__name__)
_HELP_SECTIONS = {
    "pins": (
        "📌 *Pins*\n\n"
        "Pin anything you run often — a search or any command — then re-run it with one tap:\n\n"
        "• `pin ai news`\n"
        "• `pin latest xrp news and price`\n"
        "• `pin screenshot https://google.com`\n\n"
        "Send `pins` to see your pins — each has a ▶ Run and a 🗑️ Delete button\\."
    ),
    "reminders": (
        "⏰ *Reminders*\n\n"
        "Set a reminder in plain language — I work out the time and ping you here "
        "\\(and in the web UI\\):\n\n"
        "• `remind open the oven in 10m`\n"
        "• `remind me next tuesday to call mom`\n\n"
        "Send `reminders` to list your pending ones, each with a 🗑️ Cancel button\\. "
        "You can also `remind cancel <id>`\\."
    ),
    "chat": (
        "💬 *Chat & URLs*\n\n"
        "Just send any message to chat with the AI\\.\n\n"
        "• Reply to a message to use it as context\n"
        "• Send any URL to get a summary\n"
        "• Forward any article or link — auto\\-summarized\n"
        "• Send a photo to describe it or extract text \\(OCR\\)\n"
        "• The bot remembers recent conversation context"
    ),
    "geni": (
        "🎨 *Image Generation*\n\n"
        "`geni <prompt>`\n"
        "Generates an image from your description using the configured AI backend\\.\n\n"
        "*Examples:*\n"
        "`geni a sunset over a cyberpunk city`\n"
        "`geni portrait of a samurai in watercolor style`"
    ),
    "logs": (
        "📋 *System Health Report*\n\n"
        "`logs` — Per node: disk, SMART, RAID, failed services, swap and a 6h error count,\n"
        "with the top error lines quoted underneath\\.\n"
        "Also answers to `syslogs`\\. It is a STATUS BOARD, not a log tail\\."
    ),
}
router = APIRouter(prefix="/api/telegram", tags=["telegram"])
_MAX_SEEN_IDS = 500  # Keep a bounded window; Telegram won't replay further back
_link_action_cache: dict = {}
class TelegramBotConfig(BaseModel):
    bot_token: Optional[str] = None
    webhook_url: Optional[str] = None
    enabled: bool = False
class TelegramChatSetup(BaseModel):
    chat_id: str
    notifications: str = "news,downloads,mentions"
