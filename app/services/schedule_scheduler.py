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
from app.services.caldav_service import (
    get_all_user_events,
    format_events_for_display
)

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
    """Generate daily schedule for a specific user."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            logger.warning(f"User {user_id} not found")
            return

        # Check if user has schedule enabled
        schedule_enabled = db.query(UserSetting).filter(
            UserSetting.user_id == user_id,
            UserSetting.key == "schedule_enabled"
        ).first()

        if not schedule_enabled or not schedule_enabled.value or schedule_enabled.value.lower() != "true":
            logger.debug(f"Schedule disabled for user {user_id}")
            return

        # Check if today's schedule has already been sent (prevent duplicates)
        date_str = datetime.now().strftime("%A, %B %d, %Y")
        today_header = f"## Daily Schedule - {date_str}"

        today_chat = db.query(Conversation).filter(
            Conversation.user_id == user_id,
            Conversation.title == TODAY_CHAT_TITLE
        ).first()

        if today_chat:
            existing_msg = db.query(Message).filter(
                Message.conversation_id == today_chat.id,
                Message.content.like(f"{today_header}%")
            ).first()
            if existing_msg:
                logger.info(f"Daily schedule already sent today for user {user_id}, skipping")
                return

        logger.info(f"Generating daily schedule for user {user_id}...")

        # Get today's events
        today = datetime.now().replace(hour=0, minute=0, second=0, microsecond=0)
        tomorrow = today + timedelta(days=1)

        events = get_all_user_events(user_id, today, tomorrow, db)
        events_text = format_events_for_display(events, include_description=True)

        if not events:
            events_text = "No events scheduled for today."

        # Generate AI summary
        summary = await generate_schedule_summary(db, user, events_text)

        # Get or create Today chat
        today_chat = get_or_create_today_chat(db, user_id)

        # Format the message (date_str already set above for dedup check)
        timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
        message_text = f"## Daily Schedule - {date_str}\n*{timestamp}*\n\n{summary}"

        # Add message to chat
        schedule_msg = Message(
            conversation_id=today_chat.id,
            role="assistant",
            content=message_text
        )
        db.add(schedule_msg)

        # Update conversation timestamp
        today_chat.updated_at = datetime.utcnow()
        db.commit()

        logger.info(f"Added daily schedule to Today chat for user {user_id}")

    except Exception as e:
        logger.error(f"Error in schedule scheduler for user {user_id}: {e}")
        db.rollback()
    finally:
        db.close()


async def check_and_run_schedules():
    """Check all users and run schedule for those with it enabled."""
    db = SessionLocal()
    try:
        # Get all users with schedule enabled
        enabled_users = db.query(UserSetting).filter(
            UserSetting.key == "schedule_enabled",
            UserSetting.value == "true"
        ).all()

        user_ids = [s.user_id for s in enabled_users]

        for user_id in user_ids:
            await run_daily_schedule_for_user(user_id)

    except Exception as e:
        logger.error(f"Error in schedule scheduler check: {e}")
    finally:
        db.close()


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
