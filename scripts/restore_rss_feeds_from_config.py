#!/usr/bin/env python3
"""
Restore RSS feeds for bot accounts from a config file, similar to restore_bots_from_config.py

This script fetches bot configuration from the remote server and restores RSS feeds
for each bot account based on the configuration.

Usage:
    python scripts/restore_rss_feeds_from_config.py
"""
import sys
import os
import asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal, init_db
from app.models import User
from plugins.rss.models import RssFeed
from plugins.rss.service import RssService

# Remote server details
REMOTE_HOST = "192.168.0.1"
REMOTE_CONFIG_PATH = "~/posterchan/bots_config.py"

# Default RSS feeds for each bot (you can modify this or load from config)
DEFAULT_BOT_FEEDS = {
    'news_rss': [
        # Add default news RSS feeds here
        # Example: {'url': 'https://example.com/news/feed.xml', 'name': 'News Feed'},
    ],
    'anime_rss': [
        # Add default anime RSS feeds here
    ],
    'jeet_rss': [
        # Add default jeet RSS feeds here
    ],
    'tonesha_rss': [
        # Add default tonesha RSS feeds here
    ],
    'judge_rss': [
        # Add default judge RSS feeds here
    ],
    'candy_rss': [
        # Add default candy RSS feeds here
    ],
}

# Username mapping from config to database
USERNAME_MAP = {
    'anime': 'anime_rss',
    'news': 'news_rss',
    'jeet': 'jeet_rss',
    'tonesha': 'tonesha_rss',
    'judgedread': 'judge_rss',
    'judge': 'judge_rss',
    'candy': 'candy_rss',
}


async def add_feed_to_user(db, user_id: int, feed_url: str, feed_name: str = None):
    """Add an RSS feed to a user."""
    try:
        rss_service = RssService(db)
        feed = await rss_service.add_feed(user_id, feed_url, feed_name)
        return feed
    except Exception as e:
        print(f"  ❌ Error adding feed {feed_url}: {e}")
        return None


def restore_feeds_for_bot(db, username: str, feeds_list: list):
    """Restore RSS feeds for a bot account."""
    user = db.query(User).filter(User.username == username).first()
    if not user:
        print(f"⚠️  User '{username}' not found, skipping...")
        return 0
    
    # Ensure RSS is enabled
    if not user.rss_enabled:
        user.rss_enabled = True
        db.commit()
        print(f"✓ Enabled RSS for {username}")
    
    restored = 0
    skipped = 0
    
    print(f"\n{username} (ID: {user.id}):")
    
    for feed_config in feeds_list:
        feed_url = feed_config.get('url')
        feed_name = feed_config.get('name')
        
        if not feed_url:
            print(f"  ⚠️  Skipping feed with no URL")
            continue
        
        # Check if feed already exists
        existing = db.query(RssFeed).filter(
            RssFeed.user_id == user.id,
            RssFeed.url == feed_url
        ).first()
        
        if existing:
            print(f"  ✓ Feed already exists: {existing.display_name}")
            skipped += 1
            continue
        
        feed = asyncio.run(add_feed_to_user(db, user.id, feed_url, feed_name))
        if feed:
            db.commit()
            print(f"  ✓ Added: {feed.display_name}")
            restored += 1
        else:
            skipped += 1
    
    return restored, skipped


def main():
    print("=" * 70)
    print("RESTORING RSS FEEDS FOR BOT ACCOUNTS")
    print("=" * 70)
    print()
    
    # Initialize database
    init_db()
    db = SessionLocal()
    
    try:
        # Get all bot accounts
        bot_users = db.query(User).filter(User.rss_enabled == True).all()
        
        if not bot_users:
            print("No bot accounts found (users with rss_enabled=True)")
            return
        
        print(f"Found {len(bot_users)} bot accounts")
        print()
        
        total_restored = 0
        total_skipped = 0
        
        # Restore feeds for each bot using DEFAULT_BOT_FEEDS
        for user in bot_users:
            feeds_list = DEFAULT_BOT_FEEDS.get(user.username, [])
            
            if not feeds_list:
                print(f"⚠️  No feeds configured for {user.username}")
                continue
            
            restored, skipped = restore_feeds_for_bot(db, user.username, feeds_list)
            total_restored += restored
            total_skipped += skipped
        
        print("\n" + "=" * 70)
        print(f"✓ Restored {total_restored} feeds")
        if total_skipped > 0:
            print(f"⚠️  Skipped {total_skipped} feeds (already exist or errors)")
        print("=" * 70)
        
        if total_restored == 0:
            print("\n⚠️  No feeds were restored.")
            print("Please edit DEFAULT_BOT_FEEDS in this script with the RSS feed URLs")
            print("or use the interactive restore script:")
            print("  python scripts/restore_bot_rss_feeds.py")
    
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        db.rollback()
    finally:
        db.close()


if __name__ == '__main__':
    main()
