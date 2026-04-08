"""
Schedule Scheduler - Daily calendar summary.

Runs at 6:00 AM to:
1. Collect calendar events for the day
2. Generate AI summary
3. Store in a "Today" conversation for each user with schedule enabled
"""
import logging
from datetime import datetime, timedelta
from typing import Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import User, UserSetting, Conversation, Message
from app.services.chat_service import ChatService

logger = logging.getLogger(__name__)

# Global scheduler instance
schedule_scheduler: Optional[AsyncIOScheduler] = None

# Name of the Today conversation
TODAY_CHAT_TITLE = "Today"


def get_or_create_today_chat(db: Session, user_id: int) -> Conversation:
    """Get the Today chat for a user, creating it if it doesn't exist."""
    today_chat = db.query(Conversation).filter(
        Conversation.user_id == user_id,
        Conversation.title == TODAY_CHAT_TITLE
    ).first()

    if today_chat:
        return today_chat

    today_chat = Conversation(
        user_id=user_id,
        title=TODAY_CHAT_TITLE
    )
    db.add(today_chat)
    db.commit()
    db.refresh(today_chat)
    logger.info(f"Created Today chat for user {user_id}")
    return today_chat


async def generate_schedule_summary(db: Session, user: User, events_text: str) -> str:
    """Use AI to summarize the day's events."""
    today = datetime.now().strftime("%A, %B %d, %Y")

    messages = [
        {
            "role": "system",
            "content": "You are a helpful personal assistant. Provide a friendly, concise summary of today's schedule. Highlight important events and any potential conflicts. Use emojis to make it engaging."
        },
        {
            "role": "user",
            "content": f"Summarize my schedule for {today}:\n\n{events_text}"
        }
    ]

    try:
        chat_service = ChatService(db, user)
        summary = await chat_service.chat(messages)
        return summary
    except Exception as e:
        logger.error(f"Error generating schedule summary: {e}")
        return f"Error generating summary: {str(e)}\n\nRaw schedule:\n{events_text}"


async def run_daily_schedule_for_user(user_id: int):
    """Schedule generation disabled - calendar integration removed."""
    pass


async def check_and_run_schedules():
    """Schedule generation disabled - calendar integration removed."""
    pass


def start_schedule_scheduler():
    """Start the schedule scheduler."""
    global schedule_scheduler

    if schedule_scheduler is not None:
        logger.warning("Schedule scheduler already running")
        return

    schedule_scheduler = AsyncIOScheduler()

    # Run at 6:00 AM every day
    schedule_scheduler.add_job(
        check_and_run_schedules,
        CronTrigger(hour=6, minute=0),
        id="schedule_scheduler",
        name="Daily Schedule Scheduler",
        replace_existing=True
    )

    schedule_scheduler.start()
    logger.info("Schedule scheduler started - running at 6:00 AM daily")


def stop_schedule_scheduler():
    """Stop the schedule scheduler."""
    global schedule_scheduler

    if schedule_scheduler is not None:
        schedule_scheduler.shutdown()
        schedule_scheduler = None
        logger.info("Schedule scheduler stopped")
