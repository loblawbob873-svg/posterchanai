#!/usr/bin/env python3
"""
Restore RSS feeds for bot accounts.

This script helps restore RSS feeds that were accidentally deleted from bot accounts.
You can either:
1. Use the interactive mode to add feeds one by one
2. Use a configuration file to restore multiple feeds at once
3. Use command-line arguments to add a single feed

Usage:
    # Interactive mode
    python scripts/restore_bot_rss_feeds.py

    # Add a single feed
    python scripts/restore_bot_rss_feeds.py --user-id 3 --feed-url "https://example.com/feed.xml" --feed-name "My Feed"

    # Restore from config file
    python scripts/restore_bot_rss_feeds.py --config-file bot_feeds_config.py
"""
import sys
import os
import argparse
import asyncio
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal, init_db
from app.models import User
from plugins.rss.models import RssFeed
from plugins.rss.service import RssService


def list_bot_accounts(db):
    """List all bot accounts (users with rss_enabled=True)."""
    bot_users = db.query(User).filter(User.rss_enabled == True).all()
    return bot_users


def show_bot_status(db):
    """Show current status of all bot accounts and their RSS feeds."""
    bot_users = list_bot_accounts(db)
    
    print("=" * 70)
    print("BOT ACCOUNTS RSS FEED STATUS")
    print("=" * 70)
    print()
    
    if not bot_users:
        print("No bot accounts found (users with rss_enabled=True)")
        return
    
    for user in bot_users:
        feeds = db.query(RssFeed).filter(RssFeed.user_id == user.id).all()
        status = "✓" if feeds else "⚠️  NO FEEDS"
        print(f"{status} {user.username} (ID: {user.id})")
        if feeds:
            for feed in feeds:
                print(f"    - {feed.display_name}")
                print(f"      URL: {feed.url}")
        else:
            print("    No RSS feeds configured")
        print()
    
    print("=" * 70)


async def add_feed_to_user(db, user_id: int, feed_url: str, feed_name: str = None):
    """Add an RSS feed to a user."""
    user = db.query(User).filter(User.id == user_id).first()
    if not user:
        print(f"❌ Error: User ID {user_id} not found")
        return False
    
    # Ensure RSS is enabled for the user
    if not user.rss_enabled:
        user.rss_enabled = True
        db.commit()
        print(f"✓ Enabled RSS for user {user.username}")
    
    try:
        rss_service = RssService(db)
        feed = await rss_service.add_feed(user_id, feed_url, feed_name)
        
        print(f"✓ Added RSS feed for {user.username} (ID: {user_id})")
        print(f"  Feed: {feed.display_name}")
        print(f"  URL: {feed_url}")
        return True
    except Exception as e:
        print(f"❌ Error adding RSS feed: {e}")
        db.rollback()
        return False


def interactive_mode(db):
    """Interactive mode to add feeds to bot accounts."""
    bot_users = list_bot_accounts(db)
    
    if not bot_users:
        print("No bot accounts found. Exiting.")
        return
    
    print("\n" + "=" * 70)
    print("INTERACTIVE RSS FEED RESTORATION")
    print("=" * 70)
    print()
    
    while True:
        print("\nBot accounts:")
        for i, user in enumerate(bot_users, 1):
            feeds_count = db.query(RssFeed).filter(RssFeed.user_id == user.id).count()
            print(f"  {i}. {user.username} (ID: {user.id}) - {feeds_count} feed(s)")
        print(f"  {len(bot_users) + 1}. Show status")
        print(f"  {len(bot_users) + 2}. Exit")
        
        try:
            choice = input("\nSelect bot account (number): ").strip()
            
            if choice == str(len(bot_users) + 2):
                break
            elif choice == str(len(bot_users) + 1):
                show_bot_status(db)
                continue
            
            choice_num = int(choice)
            if choice_num < 1 or choice_num > len(bot_users):
                print("Invalid choice")
                continue
            
            selected_user = bot_users[choice_num - 1]
            
            print(f"\nAdding feed for {selected_user.username} (ID: {selected_user.id})")
            feed_url = input("Feed URL: ").strip()
            if not feed_url:
                print("URL cannot be empty")
                continue
            
            feed_name = input("Feed name (optional, press Enter to skip): ").strip()
            if not feed_name:
                feed_name = None
            
            success = asyncio.run(add_feed_to_user(db, selected_user.id, feed_url, feed_name))
            if success:
                db.commit()
                print("✓ Feed added successfully!")
            
        except ValueError:
            print("Please enter a valid number")
        except KeyboardInterrupt:
            print("\n\nExiting...")
            break
        except Exception as e:
            print(f"Error: {e}")


def restore_from_config(db, config_file: str):
    """Restore feeds from a Python config file."""
    if not os.path.exists(config_file):
        print(f"❌ Error: Config file not found: {config_file}")
        return
    
    # Load the config file
    config_globals = {}
    config_locals = {}
    try:
        with open(config_file, 'r') as f:
            exec(f.read(), config_globals, config_locals)
    except Exception as e:
        print(f"❌ Error loading config file: {e}")
        return
    
    # Expected format: BOT_FEEDS = {username: [{"url": "...", "name": "..."}, ...]}
    bot_feeds = config_locals.get('BOT_FEEDS', {})
    
    if not bot_feeds:
        print("❌ Error: No BOT_FEEDS dictionary found in config file")
        print("\nExpected format:")
        print("BOT_FEEDS = {")
        print("    'news_rss': [")
        print("        {'url': 'https://example.com/feed.xml', 'name': 'Example Feed'}")
        print("    ],")
        print("    'anime_rss': [...]")
        print("}")
        return
    
    print(f"\nRestoring feeds from {config_file}...")
    print("=" * 70)
    
    restored = 0
    errors = 0
    
    for username, feeds_list in bot_feeds.items():
        user = db.query(User).filter(User.username == username).first()
        if not user:
            print(f"⚠️  User '{username}' not found, skipping...")
            continue
        
        print(f"\n{user.username} (ID: {user.id}):")
        for feed_config in feeds_list:
            feed_url = feed_config.get('url')
            feed_name = feed_config.get('name')
            
            if not feed_url:
                print(f"  ⚠️  Skipping feed with no URL")
                continue
            
            success = asyncio.run(add_feed_to_user(db, user.id, feed_url, feed_name))
            if success:
                db.commit()
                restored += 1
            else:
                errors += 1
    
    print("\n" + "=" * 70)
    print(f"✓ Restored {restored} feeds")
    if errors > 0:
        print(f"⚠️  {errors} errors occurred")
    print("=" * 70)


def create_example_config():
    """Create an example configuration file."""
    example_config = '''# Bot RSS Feeds Configuration
# This file defines RSS feeds for each bot account
# Format: BOT_FEEDS = {username: [{"url": "...", "name": "..."}, ...]}

BOT_FEEDS = {
    'news_rss': [
        {'url': 'https://example.com/news/feed.xml', 'name': 'News Feed'},
        # Add more feeds for news_rss here
    ],
    'anime_rss': [
        {'url': 'https://example.com/anime/feed.xml', 'name': 'Anime Feed'},
        # Add more feeds for anime_rss here
    ],
    'jeet_rss': [
        # Add feeds for jeet_rss here
    ],
    'tonesha_rss': [
        # Add feeds for tonesha_rss here
    ],
    'judge_rss': [
        # Add feeds for judge_rss here
    ],
    'candy_rss': [
        # Add feeds for candy_rss here
    ],
}
'''
    
    config_path = 'bot_feeds_config.py.example'
    with open(config_path, 'w') as f:
        f.write(example_config)
    
    print(f"✓ Created example config file: {config_path}")
    print("Edit this file with your RSS feed URLs, then run:")
    print(f"  python scripts/restore_bot_rss_feeds.py --config-file {config_path}")


def main():
    parser = argparse.ArgumentParser(
        description='Restore RSS feeds for bot accounts',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog='''
Examples:
  # Interactive mode
  python scripts/restore_bot_rss_feeds.py

  # Add a single feed
  python scripts/restore_bot_rss_feeds.py --user-id 3 --feed-url "https://example.com/feed.xml" --feed-name "My Feed"

  # Restore from config file
  python scripts/restore_bot_rss_feeds.py --config-file bot_feeds_config.py

  # Show current status
  python scripts/restore_bot_rss_feeds.py --status

  # Create example config file
  python scripts/restore_bot_rss_feeds.py --create-example-config
        '''
    )
    
    parser.add_argument('--user-id', type=int, help='User ID to add feed to')
    parser.add_argument('--feed-url', type=str, help='RSS feed URL')
    parser.add_argument('--feed-name', type=str, help='Custom name for RSS feed')
    parser.add_argument('--config-file', type=str, help='Path to config file with feeds')
    parser.add_argument('--status', action='store_true', help='Show current status of all bot accounts')
    parser.add_argument('--create-example-config', action='store_true', help='Create an example config file')
    
    args = parser.parse_args()
    
    # Initialize database
    init_db()
    db = SessionLocal()
    
    try:
        if args.create_example_config:
            create_example_config()
            return
        
        if args.status:
            show_bot_status(db)
            return
        
        if args.config_file:
            restore_from_config(db, args.config_file)
            return
        
        if args.user_id and args.feed_url:
            success = asyncio.run(add_feed_to_user(db, args.user_id, args.feed_url, args.feed_name))
            if success:
                db.commit()
            return
        
        # Default to interactive mode
        show_bot_status(db)
        interactive_mode(db)
    
    finally:
        db.close()


if __name__ == '__main__':
    main()
