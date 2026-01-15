"""
RSS Plugin Scheduler

Runs every 30 minutes to:
1. Fetch new entries from all enabled RSS feeds
2. Generate AI summaries for new entries
3. Add summaries to the user's "RSS News" conversation
"""
import logging
from datetime import datetime
from typing import Optional
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import User, Conversation, Message, Setting
from plugins.rss.models import RssFeed, RssEntry
from plugins.rss.service import RssService
from app.services.chat_service import ChatService

logger = logging.getLogger(__name__)

# Global scheduler instance
rss_scheduler: Optional[AsyncIOScheduler] = None

RSS_CHAT_TITLE = "RSS News"


def get_or_create_rss_chat(db: Session, user_id: int) -> Conversation:
    """Get the RSS chat for a user, creating it if it doesn't exist."""
    rss_chat = db.query(Conversation).filter(
        Conversation.user_id == user_id,
        Conversation.title == RSS_CHAT_TITLE
    ).first()

    if rss_chat:
        return rss_chat

    rss_chat = Conversation(user_id=user_id, title=RSS_CHAT_TITLE)
    db.add(rss_chat)
    db.commit()
    db.refresh(rss_chat)
    logger.info(f"Created RSS chat for user {user_id}")
    return rss_chat


async def summarize_article(db: Session, user: User, title: str, content: str, url: str) -> str:
    """Use AI to summarize an article."""
    max_content_len = 8000
    if len(content) > max_content_len:
        content = content[:max_content_len] + "..."

    messages = [
        {
            "role": "system",
            "content": "You are a news summarizer. Provide concise, informative summaries of news articles. Focus on the key facts and main points. Keep summaries to 2-4 sentences."
        },
        {
            "role": "user",
            "content": f"Summarize this news article:\n\nTitle: {title}\nURL: {url}\n\nContent:\n{content}"
        }
    ]

    try:
        chat_service = ChatService(db, user)
        summary = await chat_service.chat(messages)
        return summary
    except Exception as e:
        logger.error(f"Error summarizing article: {e}")
        return f"Error summarizing article: {str(e)}"


async def process_rss_for_user(user_id: int):
    """Process RSS feeds for a specific user."""
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            return

        if not getattr(user, 'rss_enabled', False):
            return

        rss_service = RssService(db)
        feeds = rss_service.get_user_feeds(user_id)
        if not feeds:
            return

        # Sync all feeds
        total_new = 0
        for feed in feeds:
            if feed.enabled:
                new_count = await rss_service.sync_feed(feed)
                total_new += new_count

        if total_new == 0:
            return

        logger.info(f"Found {total_new} new RSS entries for user {user.username}")

        # Get unsummarized entries
        entries = rss_service.get_unsummarized_entries(user_id)
        if not entries:
            return

        rss_chat = get_or_create_rss_chat(db, user.id)

        for entry in entries:
            text_content = rss_service.get_entry_content_text(entry)

            if len(text_content) < 100 and entry.url:
                fetched = await rss_service.fetch_full_article(entry.url)
                if fetched:
                    text_content = fetched

            summary = await summarize_article(db, user, entry.title, text_content, entry.url or "")

            entry.summary = summary
            entry.is_summarized = True
            entry.is_read = True  # Mark as read after summarizing

            feed = entry.feed
            if entry.url:
                summary_text = f"**[{entry.title}]({entry.url})**\n*Source: {feed.display_name}*\n\n{summary}"
            else:
                summary_text = f"**{entry.title}**\n*Source: {feed.display_name}*\n\n{summary}"

            news_msg = Message(
                conversation_id=rss_chat.id,
                role="assistant",
                content=summary_text
            )
            db.add(news_msg)
            rss_chat.updated_at = datetime.utcnow()
            db.commit()

            logger.info(f"Added summary for '{entry.title[:50]}...' to RSS chat")

    except Exception as e:
        logger.error(f"Error processing RSS for user {user_id}: {e}")
        db.rollback()
    finally:
        db.close()


async def check_and_run_rss():
    """Check all users and run RSS processing for enabled users."""
    db = SessionLocal()
    try:
        rss_enabled = db.query(Setting).filter(Setting.key == "rss_enabled").first()
        if not rss_enabled or rss_enabled.value.lower() != "true":
            return

        users = db.query(User).all()
        for user in users:
            if getattr(user, 'rss_enabled', False):
                await process_rss_for_user(user.id)

    except Exception as e:
        logger.error(f"Error in RSS scheduler: {e}")
    finally:
        db.close()


def mark_old_entries_as_read():
    """Mark all existing unsummarized entries as read to prevent summarizing old articles."""
    db = SessionLocal()
    try:
        # Mark all unsummarized entries as summarized and read
        count = db.query(RssEntry).filter(
            RssEntry.is_summarized == False
        ).update({"is_summarized": True, "is_read": True}, synchronize_session=False)
        
        if count > 0:
            db.commit()
            logger.info(f"Marked {count} old RSS entries as read (skipping summarization)")
    except Exception as e:
        logger.error(f"Error marking old entries as read: {e}")
        db.rollback()
    finally:
        db.close()


def start_rss_scheduler():
    """Start the RSS scheduler."""
    global rss_scheduler

    if rss_scheduler is not None:
        return

    db = SessionLocal()
    try:
        enabled_setting = db.query(Setting).filter(Setting.key == "rss_enabled").first()
        if not enabled_setting or enabled_setting.value.lower() != "true":
            logger.info("RSS plugin disabled")
            return
    finally:
        db.close()

    # Mark any existing old entries as read before starting
    mark_old_entries_as_read()

    rss_scheduler = AsyncIOScheduler()
    rss_scheduler.add_job(
        check_and_run_rss,
        CronTrigger(minute="0,30"),
        id="rss_scheduler",
        name="RSS News Scheduler",
        replace_existing=True
    )
    rss_scheduler.start()
    logger.info("RSS plugin scheduler started")


def stop_rss_scheduler():
    """Stop the RSS scheduler."""
    global rss_scheduler
    if rss_scheduler is not None:
        rss_scheduler.shutdown()
        rss_scheduler = None
        logger.info("RSS plugin scheduler stopped")
