#!/usr/bin/env python3
"""
Helper script to restore API keys for bot accounts and optionally RSS feeds.

Usage:
    python scripts/restore_bot_accounts.py --user-id 3 --api-key-name "Bot Key"
    python scripts/restore_bot_accounts.py --all-bots
    python scripts/restore_bot_accounts.py --user-id 3 --rss-url "https://example.com/feed.xml" --rss-name "My Feed"
"""
import sys
import os
import secrets
import argparse
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal, init_db
from app.models import User, APIKey
from plugins.rss.models import RssFeed
from plugins.rss.service import RssService
import asyncio

def create_api_key_for_user(user_id: int, key_name: str = "Bot API Key", db=None):
    """Create an API key for a user."""
    if db is None:
        db = SessionLocal()
        should_close = True
    else:
        should_close = False
    
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            print(f"Error: User ID {user_id} not found")
            return None
        
        # Generate a secure random key
        raw_key = secrets.token_hex(32)
        api_key = f"sk-{raw_key}"
        
        new_key = APIKey(
            user_id=user_id,
            key=api_key,
            name=key_name,
            is_active=True
        )
        db.add(new_key)
        db.commit()
        db.refresh(new_key)
        
        print(f"✓ Created API key for user {user.username} (ID: {user_id})")
        print(f"  Key: {api_key}")
        print(f"  Name: {key_name}")
        print(f"  ⚠️  Save this key now - it won't be shown again!")
        
        return api_key
    except Exception as e:
        print(f"Error creating API key: {e}")
        db.rollback()
        return None
    finally:
        if should_close:
            db.close()

async def add_rss_feed_for_user(user_id: int, feed_url: str, feed_name: str = None, db=None):
    """Add an RSS feed for a user."""
    if db is None:
        db = SessionLocal()
        should_close = True
    else:
        should_close = False
    
    try:
        user = db.query(User).filter(User.id == user_id).first()
        if not user:
            print(f"Error: User ID {user_id} not found")
            return None
        
        # Enable RSS for user if not already enabled
        if not getattr(user, 'rss_enabled', False):
            user.rss_enabled = True
            db.commit()
            print(f"✓ Enabled RSS for user {user.username}")
        
        rss_service = RssService(db)
        feed = await rss_service.add_feed(user_id, feed_url, feed_name)
        
        print(f"✓ Added RSS feed for user {user.username} (ID: {user_id})")
        print(f"  Feed: {feed.display_name}")
        print(f"  URL: {feed_url}")
        
        return feed
    except Exception as e:
        print(f"Error adding RSS feed: {e}")
        db.rollback()
        return None
    finally:
        if should_close:
            db.close()

def main():
    parser = argparse.ArgumentParser(description='Restore bot account API keys and RSS feeds')
    parser.add_argument('--user-id', type=int, help='User ID to restore')
    parser.add_argument('--all-bots', action='store_true', help='Restore API keys for all bot accounts (users with rss_enabled=1)')
    parser.add_argument('--api-key-name', type=str, default='Bot API Key', help='Name for the API key')
    parser.add_argument('--rss-url', type=str, help='RSS feed URL to add')
    parser.add_argument('--rss-name', type=str, help='Custom name for RSS feed')
    
    args = parser.parse_args()
    
    # Initialize database
    init_db()
    db = SessionLocal()
    
    try:
        if args.all_bots:
            # Get all bot accounts (users with rss_enabled=1)
            bot_users = db.query(User).filter(User.rss_enabled == True).all()
            print(f"Found {len(bot_users)} bot accounts")
            
            for user in bot_users:
                # Check if user already has API keys
                existing_keys = db.query(APIKey).filter(APIKey.user_id == user.id).count()
                if existing_keys > 0:
                    print(f"⚠️  User {user.username} (ID: {user.id}) already has {existing_keys} API key(s), skipping...")
                    continue
                
                create_api_key_for_user(user.id, args.api_key_name, db)
                print()
        
        elif args.user_id:
            user = db.query(User).filter(User.id == args.user_id).first()
            if not user:
                print(f"Error: User ID {args.user_id} not found")
                return
            
            # Create API key if user doesn't have one
            existing_keys = db.query(APIKey).filter(APIKey.user_id == args.user_id).count()
            if existing_keys == 0:
                create_api_key_for_user(args.user_id, args.api_key_name, db)
            else:
                print(f"⚠️  User {user.username} already has {existing_keys} API key(s)")
            
            # Add RSS feed if URL provided
            if args.rss_url:
                asyncio.run(add_rss_feed_for_user(args.user_id, args.rss_url, args.rss_name, db))
        
        else:
            parser.print_help()
            print("\nExamples:")
            print("  # Create API key for user ID 3:")
            print("  python scripts/restore_bot_accounts.py --user-id 3")
            print("\n  # Create API keys for all bot accounts:")
            print("  python scripts/restore_bot_accounts.py --all-bots")
            print("\n  # Add RSS feed for user ID 3:")
            print("  python scripts/restore_bot_accounts.py --user-id 3 --rss-url 'https://example.com/feed.xml' --rss-name 'My Feed'")
    
    finally:
        db.close()

if __name__ == '__main__':
    main()
