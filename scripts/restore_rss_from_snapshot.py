#!/usr/bin/env python3
"""
Restore RSS feeds from a database snapshot.

This script reads RSS feed URLs from a snapshot database and restores them
to the current database.

Usage:
    python scripts/restore_rss_from_snapshot.py
"""
import sys
import os
import sqlite3
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal, init_db
from app.models import User
from plugins.rss.models import RssFeed

# Snapshot database path (on remote server 192.168.0.85)
SNAPSHOT_DB_REMOTE = "192.168.0.85:/raid/snapshots/router.lan-2026-01-16-00/home/verita84/posterchanai/posterchanai.db"
SNAPSHOT_DB_LOCAL = "/tmp/posterchanai_snapshot.db"


def copy_snapshot_from_remote():
    """Copy snapshot database from remote server."""
    import subprocess
    import shutil
    
    # Check if local copy already exists
    if os.path.exists(SNAPSHOT_DB_LOCAL):
        print(f"Using existing local copy: {SNAPSHOT_DB_LOCAL}")
        return True
    
    print(f"Copying snapshot from {SNAPSHOT_DB_REMOTE}...")
    try:
        # Use scp to copy the file
        result = subprocess.run(
            ['scp', SNAPSHOT_DB_REMOTE, SNAPSHOT_DB_LOCAL],
            capture_output=True,
            text=True,
            timeout=60
        )
        
        if result.returncode == 0:
            print(f"✓ Copied snapshot to {SNAPSHOT_DB_LOCAL}")
            return True
        else:
            print(f"❌ Error copying snapshot: {result.stderr}")
            return False
    except subprocess.TimeoutExpired:
        print("❌ Timeout copying snapshot")
        return False
    except Exception as e:
        print(f"❌ Error: {e}")
        return False


def get_feeds_from_snapshot(snapshot_path: str):
    """Extract RSS feeds from snapshot database."""
    if not os.path.exists(snapshot_path):
        print(f"❌ Snapshot database not found: {snapshot_path}")
        return {}
    
    feeds_by_username = {}
    
    try:
        conn = sqlite3.connect(snapshot_path)
        cursor = conn.cursor()
        
        # Get all RSS feeds with their user information
        query = """
        SELECT u.username, rf.id, rf.url, rf.title, rf.custom_name, rf.enabled
        FROM rss_feeds rf
        JOIN users u ON rf.user_id = u.id
        WHERE u.rss_enabled = 1
        ORDER BY u.username, rf.id
        """
        
        cursor.execute(query)
        results = cursor.fetchall()
        
        for username, feed_id, url, title, custom_name, enabled in results:
            if username not in feeds_by_username:
                feeds_by_username[username] = []
            
            feed_name = custom_name or title or url
            feeds_by_username[username].append({
                'url': url,
                'name': feed_name,
                'enabled': enabled
            })
        
        conn.close()
        
    except sqlite3.Error as e:
        print(f"❌ Error reading snapshot database: {e}")
        return {}
    except Exception as e:
        print(f"❌ Error: {e}")
        return {}
    
    return feeds_by_username


def restore_feed_direct(db, user_id: int, feed_url: str, feed_name: str = None, enabled: bool = True):
    """Restore a single RSS feed directly to database without validation."""
    try:
        # Check if feed already exists
        existing = db.query(RssFeed).filter(
            RssFeed.user_id == user_id,
            RssFeed.url == feed_url
        ).first()
        
        if existing:
            return existing, 'exists'
        
        # Create feed directly without fetching/validating
        feed = RssFeed(
            user_id=user_id,
            url=feed_url,
            custom_name=feed_name,
            enabled=enabled
        )
        db.add(feed)
        db.commit()
        db.refresh(feed)
        return feed, 'added'
    except Exception as e:
        print(f"  ❌ Error: {e}")
        db.rollback()
        return None, 'error'


def main():
    print("=" * 70)
    print("RESTORING RSS FEEDS FROM DATABASE SNAPSHOT")
    print("=" * 70)
    print()
    print(f"Remote snapshot: {SNAPSHOT_DB_REMOTE}")
    print()
    
    # Copy snapshot from remote server
    if not copy_snapshot_from_remote():
        print("❌ Failed to access snapshot database")
        return
    
    # Extract feeds from snapshot
    print("Reading feeds from snapshot...")
    feeds_by_username = get_feeds_from_snapshot(SNAPSHOT_DB_LOCAL)
    
    if not feeds_by_username:
        print("❌ No feeds found in snapshot or snapshot not accessible")
        return
    
    print(f"✓ Found feeds for {len(feeds_by_username)} users")
    print()
    
    # Initialize current database
    init_db()
    db = SessionLocal()
    
    try:
        total_restored = 0
        total_existing = 0
        total_errors = 0
        
        for username, feeds_list in feeds_by_username.items():
            user = db.query(User).filter(User.username == username).first()
            if not user:
                print(f"⚠️  User '{username}' not found in current database, skipping...")
                continue
            
            # Ensure RSS is enabled
            if not user.rss_enabled:
                user.rss_enabled = True
                db.commit()
                print(f"✓ Enabled RSS for {username}")
            
            print(f"\n{username} (ID: {user.id}):")
            print(f"  Found {len(feeds_list)} feed(s) in snapshot")
            
            for feed_config in feeds_list:
                feed_url = feed_config['url']
                feed_name = feed_config.get('name')
                feed_enabled = feed_config.get('enabled', True)
                
                feed, status = restore_feed_direct(db, user.id, feed_url, feed_name, feed_enabled)
                
                if status == 'added':
                    print(f"  ✓ Added: {feed.display_name if feed else feed_url}")
                    total_restored += 1
                elif status == 'exists':
                    print(f"  ⊙ Already exists: {feed.display_name if feed else feed_url}")
                    total_existing += 1
                else:
                    print(f"  ❌ Failed: {feed_url}")
                    total_errors += 1
            
            db.commit()
        
        print("\n" + "=" * 70)
        print("SUMMARY")
        print("=" * 70)
        print(f"✓ Restored: {total_restored} feeds")
        if total_existing > 0:
            print(f"⊙ Already existed: {total_existing} feeds")
        if total_errors > 0:
            print(f"❌ Errors: {total_errors} feeds")
        print("=" * 70)
        
    except Exception as e:
        print(f"❌ Error: {e}")
        import traceback
        traceback.print_exc()
        db.rollback()
    finally:
        db.close()


if __name__ == '__main__':
    main()
