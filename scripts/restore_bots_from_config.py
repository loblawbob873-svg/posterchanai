#!/usr/bin/env python3
"""
Restore bot API keys from bots_config.py and mark news RSS items as read.

Usage:
    python scripts/restore_bots_from_config.py
"""
import sys
import os
import ast
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal, init_db
from app.models import User, APIKey
from plugins.rss.models import RssEntry, RssFeed
import subprocess

# Remote server details
REMOTE_HOST = "192.168.0.1"
REMOTE_DB_PATH = "~/posterchanai/posterchanai.db"
REMOTE_CONFIG_PATH = "~/posterchan/bots_config.py"

def fetch_bots_config():
    """Fetch bots_config.py from remote server."""
    try:
        result = subprocess.run(
            ['ssh', '-o', 'ConnectTimeout=5', REMOTE_HOST, f'cat {REMOTE_CONFIG_PATH}'],
            capture_output=True,
            text=True,
            timeout=10
        )
        if result.returncode == 0:
            return result.stdout
        else:
            print(f"Error fetching config: {result.stderr}")
            return None
    except Exception as e:
        print(f"Error fetching config: {e}")
        return None

def get_remote_db_session():
    """Get database session for remote database."""
    # Set environment variable to use remote database
    # We'll need to copy the database locally or use SSH to run commands
    # For now, let's run the script on the remote server
    return None

def parse_bots_config(config_text):
    """Parse bots_config.py to extract API keys."""
    # Extract TEXT_BOTS dictionary
    bots = {}
    
    # Find TEXT_BOTS section
    text_bots_start = config_text.find('TEXT_BOTS = {')
    if text_bots_start == -1:
        return bots
    
    # Find the matching closing brace
    brace_count = 0
    start_pos = text_bots_start
    in_string = False
    string_char = None
    
    for i in range(text_bots_start, len(config_text)):
        char = config_text[i]
        
        if not in_string:
            if char in ('"', "'"):
                in_string = True
                string_char = char
            elif char == '{':
                brace_count += 1
            elif char == '}':
                brace_count -= 1
                if brace_count == 0:
                    # Found the end of TEXT_BOTS dict
                    text_bots_section = config_text[text_bots_start:i+1]
                    break
        else:
            if char == string_char and config_text[i-1] != '\\':
                in_string = False
                string_char = None
    else:
        print("Could not find end of TEXT_BOTS dictionary")
        return bots
    
    # Parse the dictionary using ast.literal_eval
    try:
        # Extract just the dictionary content
        dict_content = text_bots_section[text_bots_section.find('{'):]
        bots_dict = ast.literal_eval(dict_content)
        
        # Extract username and rss_api_key for each bot
        for bot_name, bot_config in bots_dict.items():
            username = bot_config.get('username')
            rss_api_key = bot_config.get('rss_api_key')
            if username and rss_api_key:
                bots[username] = {
                    'api_key': rss_api_key,
                    'bot_name': bot_name
                }
    except Exception as e:
        print(f"Error parsing config: {e}")
        # Fallback: manual extraction
        import re
        for match in re.finditer(r'"username":\s*"([^"]+)".*?"rss_api_key":\s*"([^"]+)"', config_text, re.DOTALL):
            username = match.group(1)
            api_key = match.group(2)
            bots[username] = {'api_key': api_key}
    
    return bots

def restore_api_keys(db, bots_config):
    """Restore API keys for bot accounts."""
    restored = 0
    skipped = 0
    
    # Map config usernames to database usernames
    username_map = {
        'anime': 'anime_rss',
        'news': 'news_rss',
        'jeet': 'jeet_rss',
        'tonesha': 'tonesha_rss',
        'judgedread': 'judge_rss',
        'judge': 'judge_rss',
        'candy': 'candy_rss',
    }
    
    for config_username, config in bots_config.items():
        # Try to find user with mapped username or original
        db_username = username_map.get(config_username, config_username)
        user = db.query(User).filter(User.username == db_username).first()
        
        if not user:
            # Try original username as fallback
            user = db.query(User).filter(User.username == config_username).first()
        
        if not user:
            print(f"⚠️  User '{config_username}' (mapped: '{db_username}') not found in database, skipping...")
            continue
        
        username = user.username
        
        api_key_value = config['api_key']
        
        # Check if this API key already exists
        existing_key = db.query(APIKey).filter(
            APIKey.user_id == user.id,
            APIKey.key == api_key_value
        ).first()
        
        if existing_key:
            print(f"✓ User {username} (ID: {user.id}) already has API key, skipping...")
            skipped += 1
            continue
        
        # Check if user has any API keys
        existing_keys = db.query(APIKey).filter(APIKey.user_id == user.id).all()
        if existing_keys:
            print(f"⚠️  User {username} (ID: {user.id}) has {len(existing_keys)} existing API key(s), but not the one from config")
            print(f"   Adding new key from config...")
        
        # Create the API key
        new_key = APIKey(
            user_id=user.id,
            key=api_key_value,
            name=f"RSS Bot Key ({config.get('bot_name', username)})",
            is_active=True
        )
        db.add(new_key)
        db.commit()
        
        print(f"✓ Restored API key for {username} (ID: {user.id}, config: {config_username})")
        print(f"  Key: {api_key_value}")
        restored += 1
    
    return restored, skipped

def mark_news_rss_read(db):
    """Mark all RSS entries for news_rss user as read."""
    user = db.query(User).filter(User.username == 'news_rss').first()
    if not user:
        print("⚠️  User 'news_rss' not found")
        return 0
    
    # Get all feeds for this user
    feeds = db.query(RssFeed).filter(RssFeed.user_id == user.id).all()
    if not feeds:
        print(f"⚠️  No RSS feeds found for user 'news_rss'")
        return 0
    
    feed_ids = [feed.id for feed in feeds]
    
    # Mark all entries as read
    count = db.query(RssEntry).filter(
        RssEntry.feed_id.in_(feed_ids),
        RssEntry.is_read == False
    ).update({"is_read": True}, synchronize_session=False)
    
    db.commit()
    
    print(f"✓ Marked {count} RSS entries as read for user 'news_rss' (ID: {user.id})")
    return count

def run_on_remote_server():
    """Run the restoration script on the remote server."""
    script_content = '''
import sys
import os
sys.path.insert(0, os.path.expanduser("~/posterchanai"))

from app.database import SessionLocal, init_db
from app.models import User, APIKey
from plugins.rss.models import RssEntry, RssFeed
import ast

def parse_bots_config(config_text):
    """Parse bots_config.py to extract API keys."""
    bots = {}
    import re
    for match in re.finditer(r'"username":\s*"([^"]+)".*?"rss_api_key":\s*"([^"]+)"', config_text, re.DOTALL):
        username = match.group(1)
        api_key = match.group(2)
        bots[username] = {'api_key': api_key}
    return bots

def restore_api_keys(db, bots_config):
    """Restore API keys for bot accounts."""
    restored = 0
    skipped = 0
    
    username_map = {
        'anime': 'anime_rss',
        'news': 'news_rss',
        'jeet': 'jeet_rss',
        'tonesha': 'tonesha_rss',
        'judgedread': 'judge_rss',
        'judge': 'judge_rss',
        'candy': 'candy_rss',
    }
    
    for config_username, config in bots_config.items():
        db_username = username_map.get(config_username, config_username)
        user = db.query(User).filter(User.username == db_username).first()
        
        if not user:
            user = db.query(User).filter(User.username == config_username).first()
        
        if not user:
            print(f"⚠️  User '{config_username}' (mapped: '{db_username}') not found")
            continue
        
        username = user.username
        api_key_value = config['api_key']
        
        existing_key = db.query(APIKey).filter(
            APIKey.user_id == user.id,
            APIKey.key == api_key_value
        ).first()
        
        if existing_key:
            print(f"✓ User {username} already has API key")
            skipped += 1
            continue
        
        existing_keys = db.query(APIKey).filter(APIKey.user_id == user.id).all()
        if existing_keys:
            print(f"⚠️  User {username} has {len(existing_keys)} existing key(s), adding new one")
        
        new_key = APIKey(
            user_id=user.id,
            key=api_key_value,
            name=f"RSS Bot Key ({config_username})",
            is_active=True
        )
        db.add(new_key)
        db.commit()
        
        print(f"✓ Restored API key for {username} (ID: {user.id})")
        restored += 1
    
    return restored, skipped

def mark_news_rss_read(db):
    """Mark all RSS entries for news_rss user as read."""
    user = db.query(User).filter(User.username == 'news_rss').first()
    if not user:
        print("⚠️  User 'news_rss' not found")
        return 0
    
    feeds = db.query(RssFeed).filter(RssFeed.user_id == user.id).all()
    if not feeds:
        print(f"⚠️  No RSS feeds found for user 'news_rss' (ID: {user.id})")
        # Check if there are any entries at all
        total_entries = db.query(RssEntry).count()
        if total_entries == 0:
            print("   No RSS entries exist in database at all")
        else:
            print(f"   Total RSS entries in database: {total_entries}")
            # Try to mark all entries for this user's feeds (in case feeds were deleted but entries remain)
            # This shouldn't happen with CASCADE, but let's be safe
            all_feed_ids = db.query(RssFeed.id).filter(RssFeed.user_id == user.id).all()
            if all_feed_ids:
                feed_ids = [f[0] for f in all_feed_ids]
                count = db.query(RssEntry).filter(
                    RssEntry.feed_id.in_(feed_ids)
                ).update({"is_read": True}, synchronize_session=False)
                db.commit()
                print(f"   Marked {count} orphaned entries as read")
                return count
        return 0
    
    feed_ids = [feed.id for feed in feeds]
    
    # Count unread entries first
    unread_count = db.query(RssEntry).filter(
        RssEntry.feed_id.in_(feed_ids),
        RssEntry.is_read == False
    ).count()
    
    total_count = db.query(RssEntry).filter(
        RssEntry.feed_id.in_(feed_ids)
    ).count()
    
    if unread_count == 0 and total_count > 0:
        print(f"✓ All {total_count} RSS entries already marked as read for user 'news_rss'")
        return total_count
    
    if total_count == 0:
        print(f"⚠️  No RSS entries found for user 'news_rss' feeds")
        return 0
    
    # Mark ALL entries as read (including already read ones to ensure everything is marked)
    count = db.query(RssEntry).filter(
        RssEntry.feed_id.in_(feed_ids)
    ).update({"is_read": True}, synchronize_session=False)
    
    db.commit()
    
    print(f"✓ Marked {count} RSS entries as read for user 'news_rss' (ID: {user.id})")
    print(f"  Feeds: {len(feeds)}")
    print(f"  Total entries: {total_count}")
    print(f"  Unread entries that were marked: {unread_count}")
    return count

# Main execution
init_db()
db = SessionLocal()

try:
    with open(os.path.expanduser("~/posterchan/bots_config.py"), 'r') as f:
        config_text = f.read()
    
    bots_config = parse_bots_config(config_text)
    print(f"Found {len(bots_config)} bots with RSS API keys")
    
    restored, skipped = restore_api_keys(db, bots_config)
    print(f"Restored {restored} API keys, skipped {skipped}")
    
    count = mark_news_rss_read(db)
    print(f"Marked {count} news RSS items as read")
    
except Exception as e:
    print(f"Error: {e}")
    import traceback
    traceback.print_exc()
    db.rollback()
finally:
    db.close()
'''
    
    # Write script to remote server and execute
    try:
        # Create a temporary script file
        import tempfile
        with tempfile.NamedTemporaryFile(mode='w', suffix='.py', delete=False) as f:
            f.write(script_content)
            temp_script = f.name
        
        # Copy script to remote server
        subprocess.run(['scp', temp_script, f'{REMOTE_HOST}:~/restore_bots_temp.py'], check=True)
        
        # Execute on remote server
        result = subprocess.run(
            ['ssh', REMOTE_HOST, 'cd ~/posterchanai && source venv/bin/activate && python ~/restore_bots_temp.py'],
            capture_output=True,
            text=True,
            timeout=60
        )
        
        # Clean up
        os.unlink(temp_script)
        subprocess.run(['ssh', REMOTE_HOST, 'rm -f ~/restore_bots_temp.py'], check=False)
        
        print(result.stdout)
        if result.stderr:
            print("Errors:", result.stderr)
        
        return result.returncode == 0
    except Exception as e:
        print(f"Error running on remote server: {e}")
        return False

def main():
    print("=" * 60)
    print("RESTORING BOT API KEYS FROM CONFIG (REMOTE DATABASE)")
    print("=" * 60)
    print()
    print(f"Remote server: {REMOTE_HOST}")
    print(f"Database: {REMOTE_DB_PATH}")
    print()
    
    # Run on remote server
    success = run_on_remote_server()
    
    if success:
        print()
        print("=" * 60)
        print("✓ COMPLETED SUCCESSFULLY")
        print("=" * 60)
    else:
        print()
        print("=" * 60)
        print("❌ FAILED")
        print("=" * 60)

if __name__ == '__main__':
    main()
