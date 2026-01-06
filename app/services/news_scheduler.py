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
        if not user or not user.news_schedule_enabled:
            return

        logger.info(f"Generating daily news for user {user.username}")

        # Get news sources (user's custom sources or admin defaults)
        sources = get_user_news_sources(user, db)

        if not sources:
            logger.warning(f"No news sources configured for user {user.username}")
            return

        # Create a new conversation titled "Daily News - [date]"
        today = datetime.now().strftime("%B %d, %Y")
        conversation = Conversation(
            user_id=user.id,
            title=f"Daily News - {today}"
        )
        db.add(conversation)
        db.flush()

        # Add system message
        system_msg = Message(
            conversation_id=conversation.id,
            role="assistant",
            content=f"**Daily News Summary for {today}**\n\nFetching headlines from all sources..."
        )
        db.add(system_msg)
        db.commit()

        # Fetch news from all sources
        results = []
        for source in sources:
            try:
                markdown = await fetch_news_from_source(source["url"], source["name"], db)
                results.append(markdown)
            except Exception as e:
                logger.error(f"Error fetching news from {source['name']}: {e}")
                results.append(f"**{source['name']}:** Error fetching headlines")

        # Update the message with all news
        news_content = f"**Daily News Summary for {today}**\n\n" + "\n\n---\n\n".join(results)
        system_msg.content = news_content
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

        for user in users:
            logger.info(f"Running scheduled news for user {user.username} at {current_time}")
            await generate_daily_news_for_user(user.id)

    except Exception as e:
        logger.error(f"Error checking scheduled news: {e}")
    finally:
        db.close()


def start_scheduler():
    """Start the news scheduler"""
    global scheduler

    if scheduler is not None:
        logger.warning("Scheduler already running")
        return

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
    logger.info("News scheduler started - checking every minute for scheduled news")


def stop_scheduler():
    """Stop the news scheduler"""
    global scheduler

    if scheduler is not None:
        scheduler.shutdown()
        scheduler = None
        logger.info("News scheduler stopped")
