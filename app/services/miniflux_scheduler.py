"""
Miniflux News Scheduler

Runs at :00 and :30 every hour to:
1. Fetch unread articles from Miniflux
2. Get or create a "Miniflux" conversation for each user
3. Summarize each article using AI and add to the conversation
4. Mark articles as read in Miniflux after summarization
"""
import logging
import re
from datetime import datetime
from typing import Optional, List, Dict, Any
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from sqlalchemy.orm import Session

from app.database import SessionLocal
from app.models import User, Conversation, Message, Setting
from app.services.miniflux_service import MinifluxService
from app.services.chat_service import ChatService

logger = logging.getLogger(__name__)

# Global scheduler instance
miniflux_scheduler: Optional[AsyncIOScheduler] = None

# Name of the Miniflux conversation
MINIFLUX_CHAT_TITLE = "Miniflux"


def html_to_text(html: str) -> str:
    """Convert HTML content to plain text"""
    if not html:
        return ""
    # Remove script and style elements
    text = re.sub(r'<script[^>]*>.*?</script>', '', html, flags=re.DOTALL | re.IGNORECASE)
    text = re.sub(r'<style[^>]*>.*?</style>', '', text, flags=re.DOTALL | re.IGNORECASE)
    # Convert common elements
    text = re.sub(r'<br\s*/?>', '\n', text, flags=re.IGNORECASE)
    text = re.sub(r'<p[^>]*>', '\n\n', text, flags=re.IGNORECASE)
    text = re.sub(r'</p>', '', text, flags=re.IGNORECASE)
    # Remove remaining HTML tags
    text = re.sub(r'<[^>]+>', '', text)
    # Clean up whitespace
    text = re.sub(r'\n\s*\n', '\n\n', text)
    text = text.strip()
    return text


def get_or_create_miniflux_chat(db: Session, user_id: int) -> Conversation:
    """
    Get the Miniflux chat for a user, creating it if it doesn't exist.
    """
    # Look for existing Miniflux chat
    miniflux_chat = db.query(Conversation).filter(
        Conversation.user_id == user_id,
        Conversation.title == MINIFLUX_CHAT_TITLE
    ).first()

    if miniflux_chat:
        return miniflux_chat

    # Create new Miniflux chat
    miniflux_chat = Conversation(
        user_id=user_id,
        title=MINIFLUX_CHAT_TITLE
    )
    db.add(miniflux_chat)
    db.commit()
    db.refresh(miniflux_chat)
    logger.info(f"Created Miniflux chat for user {user_id}")
    return miniflux_chat


async def summarize_article(db: Session, user: User, title: str, content: str, url: str) -> str:
    """
    Use AI to summarize an article.

    Args:
        db: Database session
        user: The user (for custom AI settings)
        title: Article title
        content: Article content (plain text)
        url: Article URL

    Returns:
        AI-generated summary
    """
    # Truncate content if too long
    max_content_len = 8000
    if len(content) > max_content_len:
        content = content[:max_content_len] + "..."

    # Create summarization prompt
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


async def process_miniflux_news_for_user(user_id: int):
    """
    Process Miniflux news for a specific user.

    1. Fetch unread articles from Miniflux
    2. Get or create the "Miniflux" conversation
    3. For each article, generate an AI summary and add to the conversation
    4. Mark article as read in Miniflux
    """
    db = SessionLocal()
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            logger.warning(f"User {user_id} not found")
            return

        # Check if user has Miniflux enabled
        if not user.miniflux_enabled:
            logger.debug(f"Miniflux disabled for user {user.username}")
            return

        # Get Miniflux service
        miniflux = MinifluxService.from_settings(db, user)
        if not miniflux:
            logger.debug(f"Miniflux not configured for user {user.username}")
            return

        # Fetch unread entries
        entries = await miniflux.get_unread_entries(limit=50)
        if not entries:
            logger.debug(f"No unread entries for user {user.username}")
            return

        logger.info(f"Processing {len(entries)} unread entries for user {user.username}")

        # Get or create the Miniflux chat
        miniflux_chat = get_or_create_miniflux_chat(db, user.id)

        # Process each entry
        for entry in entries:
            entry_id = entry.get("id")
            title = entry.get("title", "Untitled")
            url = entry.get("url", "")
            content = entry.get("content", "")
            feed_title = entry.get("feed", {}).get("title", "Unknown Feed")

            # Convert HTML content to text
            text_content = html_to_text(content)

            # If content is too short, try fetching from URL
            if len(text_content) < 100 and url:
                fetched = await miniflux.fetch_entry_url_content(url)
                if fetched:
                    text_content = fetched

            # Generate summary
            summary = await summarize_article(db, user, title, text_content, url)

            # Format with clickable markdown link and source info
            if url:
                summary_text = f"**[{title}]({url})**\n*Source: {feed_title}*\n\n{summary}"
            else:
                summary_text = f"**{title}**\n*Source: {feed_title}*\n\n{summary}"

            # Add the summary to the Miniflux chat
            news_msg = Message(
                conversation_id=miniflux_chat.id,
                role="assistant",
                content=summary_text
            )
            db.add(news_msg)

            # Update conversation timestamp
            miniflux_chat.updated_at = datetime.utcnow()
            db.commit()

            # Mark this entry as read in Miniflux immediately
            await miniflux.mark_entries_as_read([entry_id])
            logger.info(f"Added summary for '{title[:50]}...' to Miniflux chat")

    except Exception as e:
        logger.error(f"Error processing Miniflux news for user {user_id}: {e}")
        db.rollback()
    finally:
        db.close()


async def check_and_run_miniflux_news():
    """
    Check all users and run Miniflux news processing for enabled users.
    """
    db = SessionLocal()
    try:
        # Check if Miniflux is enabled globally
        miniflux_enabled = db.query(Setting).filter(Setting.key == "miniflux_enabled").first()
        if not miniflux_enabled or miniflux_enabled.value.lower() != "true":
            return

        # Find all users with Miniflux enabled
        users = db.query(User).filter(User.miniflux_enabled == True).all()

        for user in users:
            logger.debug(f"Running Miniflux news check for user {user.username}")
            await process_miniflux_news_for_user(user.id)

    except Exception as e:
        logger.error(f"Error in Miniflux news scheduler: {e}")
    finally:
        db.close()


def start_miniflux_scheduler():
    """Start the Miniflux news scheduler"""
    global miniflux_scheduler

    if miniflux_scheduler is not None:
        logger.warning("Miniflux scheduler already running")
        return

    # Check if enabled
    db = SessionLocal()
    try:
        enabled_setting = db.query(Setting).filter(Setting.key == "miniflux_enabled").first()
        if not enabled_setting or enabled_setting.value.lower() != "true":
            logger.info("Miniflux scheduler disabled")
            return
    finally:
        db.close()

    miniflux_scheduler = AsyncIOScheduler()

    # Run at :00 and :30 every hour (cron-based, not affected by restarts)
    miniflux_scheduler.add_job(
        check_and_run_miniflux_news,
        CronTrigger(minute="0,30"),
        id="miniflux_scheduler",
        name="Miniflux News Scheduler",
        replace_existing=True
    )

    miniflux_scheduler.start()
    logger.info("Miniflux scheduler started - running at :00 and :30 every hour")


def stop_miniflux_scheduler():
    """Stop the Miniflux news scheduler"""
    global miniflux_scheduler

    if miniflux_scheduler is not None:
        miniflux_scheduler.shutdown()
        miniflux_scheduler = None
        logger.info("Miniflux scheduler stopped")
