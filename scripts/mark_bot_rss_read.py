#!/usr/bin/env python3
"""
Mark all RSS entries as read for bot accounts to prevent spam from old articles.

Usage:
    python scripts/mark_bot_rss_read.py
"""
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal, init_db
from app.models import User
from plugins.rss.models import RssFeed, RssEntry


def mark_all_bot_feeds_read():
    """Mark all RSS entries as read for all bot accounts."""
    init_db()
    db = SessionLocal()
    
    try:
        # Get all bot accounts (users with rss_enabled=True)
        bot_users = db.query(User).filter(User.rss_enabled == True).all()
        
        if not bot_users:
            print("No bot accounts found (users with rss_enabled=True)")
            return
        
        print("=" * 70)
        print("MARKING RSS ENTRIES AS READ FOR BOT ACCOUNTS")
        print("=" * 70)
        print()
        
        total_marked = 0
        
        for user in bot_users:
            # Get all feeds for this user
            feeds = db.query(RssFeed).filter(RssFeed.user_id == user.id).all()
            if not feeds:
                print(f"⚠️  {user.username} (ID: {user.id}): No RSS feeds")
                continue
            
            feed_ids = [feed.id for feed in feeds]
            
            # Count unread entries
            unread_count = db.query(RssEntry).filter(
                RssEntry.feed_id.in_(feed_ids),
                RssEntry.is_read == False
            ).count()
            
            total_count = db.query(RssEntry).filter(
                RssEntry.feed_id.in_(feed_ids)
            ).count()
            
            if unread_count == 0:
                print(f"✓ {user.username} (ID: {user.id}): All {total_count} entries already marked as read")
                continue
            
            # Mark all entries as read
            marked = db.query(RssEntry).filter(
                RssEntry.feed_id.in_(feed_ids),
                RssEntry.is_read == False
            ).update({"is_read": True}, synchronize_session=False)
            
            db.commit()
            
            print(f"✓ {user.username} (ID: {user.id}): Marked {marked} entries as read (out of {total_count} total)")
            total_marked += marked
        
        print()
        print("=" * 70)
        print(f"✓ Total: Marked {total_marked} RSS entries as read")
        print("=" * 70)
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        db.rollback()
    finally:
        db.close()


if __name__ == '__main__':
    mark_all_bot_feeds_read()
