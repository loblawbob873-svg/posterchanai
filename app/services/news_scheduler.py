import logging
from datetime import datetime
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import User, Conversation, Message
from app.routers.news import fetch_news_from_source, get_user_news_sources

logger = logging.getLogger(__name__)

# Global scheduler instance
scheduler: AsyncIOScheduler = None


async def generate_daily_news_for_user(user_id: int):
    """Generate daily news conversation for a user"""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            logger.warning(f"User {user_id} not found for daily news generation")
            return
        if not user.news_schedule_enabled:
            logger.debug(f"Daily news disabled for user {user.username}")
            return

        logger.info(f"Generating daily news for user {user.username} (scheduled time: {user.news_schedule_time})")

        # Get news sources (user's custom sources or admin defaults)
        sources = get_user_news_sources(user, db)

        if not sources:
            logger.warning(f"No news sources configured for user {user.username}")
            return

        # Check if a conversation already exists for today
        today = datetime.now().strftime("%B %d, %Y")
        title = f"Daily News - {today}"
        existing = db.query(Conversation).filter(
            Conversation.user_id == user.id,
            Conversation.title == title
        ).first()
        if existing:
            logger.info(f"Daily news already exists for user {user.username} on {today}")
            return

        # Create a new conversation titled "Daily News - [date]"
        conversation = Conversation(
            user_id=user.id,
            title=title
        )
        db.add(conversation)
        db.flush()

        # Fetch news from all sources first
        results = []
        for source in sources:
            try:
                markdown = await fetch_news_from_source(source["url"], source["name"], db)
                results.append(markdown)
            except Exception as e:
                logger.error(f"Error fetching news from {source['name']}: {e}")
                results.append(f"**{source['name']}:** Error fetching headlines")

        # Create single message with all news
        news_content = f"**Daily News Summary for {today}**\n\n" + "\n\n---\n\n".join(results)
        news_msg = Message(
            conversation_id=conversation.id,
            role="assistant",
            content=news_content
        )
        db.add(news_msg)
        db.commit()

        logger.info(f"Successfully generated daily news for user {user.username}")

    except Exception as e:
        logger.error(f"Error generating daily news for user {user_id}: {e}")
        db.rollback()
    finally:
        db.close()


async def check_and_run_scheduled_news():
    """Check all users and run scheduled news for those whose time matches"""
    now = datetime.now()
    current_time = now.strftime("%H:%M")

    db = SessionLocal()
    try:
        # Find users with news_schedule_enabled and matching time
        users = db.query(User).filter(
            User.news_schedule_enabled == True,
            User.news_schedule_time == current_time
        ).all()

        if users:
            logger.info(f"Daily News scheduler: Found {len(users)} user(s) with scheduled time {current_time}")
            for user in users:
                logger.info(f"Running scheduled news for user {user.username} at {current_time}")
                try:
                    await generate_daily_news_for_user(user.id)
                except Exception as e:
                    logger.error(f"Error generating daily news for user {user.username}: {e}", exc_info=True)
        else:
            # Log debug info every 10 minutes to help diagnose issues
            if now.minute % 10 == 0:
                enabled_users = db.query(User).filter(User.news_schedule_enabled == True).all()
                if enabled_users:
                    times = [u.news_schedule_time for u in enabled_users if u.news_schedule_time]
                    logger.debug(f"Daily News scheduler: Current time {current_time}, enabled users: {len(enabled_users)}, scheduled times: {times}")

    except Exception as e:
        logger.error(f"Error checking scheduled news: {e}", exc_info=True)
    finally:
        db.close()


def start_scheduler():
    """Start the news scheduler"""
    global scheduler

    if scheduler is not None:
        logger.warning("Daily News scheduler already running")
        return

    try:
        scheduler = AsyncIOScheduler()

        # Run every minute to check for users whose scheduled time matches
        scheduler.add_job(
            check_and_run_scheduled_news,
            CronTrigger(minute="*"),  # Every minute
            id="news_scheduler",
            name="Daily News Scheduler",
            replace_existing=True
        )

        scheduler.start()
        
        # Log enabled users on startup
        db = SessionLocal()
        try:
            enabled_users = db.query(User).filter(User.news_schedule_enabled == True).all()
            if enabled_users:
                user_info = [(u.username, u.news_schedule_time or "not set") for u in enabled_users]
                logger.info(f"Daily News scheduler started - {len(enabled_users)} user(s) enabled: {user_info}")
            else:
                logger.info("Daily News scheduler started - no users currently enabled")
        finally:
            db.close()
            
    except Exception as e:
        logger.error(f"Failed to start Daily News scheduler: {e}", exc_info=True)
        scheduler = None


def stop_scheduler():
    """Stop the news scheduler"""
    global scheduler

    if scheduler is not None:
        scheduler.shutdown()
        scheduler = None
        logger.info("News scheduler stopped")
