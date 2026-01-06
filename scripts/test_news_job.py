#!/usr/bin/env python3
"""
Test script to manually run the automatic news job for a specific user.

This script directly calls the generate_daily_news_for_user() function,
bypassing the scheduler. This allows you to test the news fetching and
summarization for a user without waiting for their scheduled time.

Usage (from project root, with venv activated):
    source venv/bin/activate
    python scripts/test_news_job.py <username_or_id>
    python scripts/test_news_job.py --list

Examples:
    python scripts/test_news_job.py admin
    python scripts/test_news_job.py 1
    python scripts/test_news_job.py verita84@poster.place
    python scripts/test_news_job.py --list
"""
import asyncio
import sys
import os

# Add parent directory to path so we can import app modules
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.database import SessionLocal
from app.models import User
from app.services.news_scheduler import generate_daily_news_for_user


def list_users_with_news():
    """List all users and their news schedule settings"""
    db = SessionLocal()
    try:
        users = db.query(User).all()
        print("\nUsers and their news settings:")
        print("-" * 70)
        print(f"{'ID':<5} {'Username':<20} {'Enabled':<10} {'Time':<8} {'Sources'}")
        print("-" * 70)
        for user in users:
            sources = "Custom" if user.news_sources else "Default"
            enabled = "Yes" if user.news_schedule_enabled else "No"
            print(f"{user.id:<5} {user.username:<20} {enabled:<10} {user.news_schedule_time:<8} {sources}")
        print("-" * 70)
    finally:
        db.close()


def get_user(identifier: str) -> User:
    """Get user by username or ID"""
    db = SessionLocal()
    try:
        # Try as ID first
        if identifier.isdigit():
            user = db.query(User).filter(User.id == int(identifier)).first()
            if user:
                return user

        # Try as username
        user = db.query(User).filter(User.username == identifier).first()
        return user
    finally:
        db.close()


async def run_news_for_user(user_id: int, username: str):
    """Run the news job for a specific user"""
    print(f"\nRunning news job for user: {username} (ID: {user_id})")
    print("-" * 50)

    await generate_daily_news_for_user(user_id)

    print("\nNews job completed!")
    print("Check the user's conversations for a new 'Daily News' conversation.")


def main():
    if len(sys.argv) < 2:
        print(__doc__)
        sys.exit(1)

    arg = sys.argv[1]

    if arg == "--list":
        list_users_with_news()
        return

    if arg in ("--help", "-h"):
        print(__doc__)
        return

    # Find the user
    user = get_user(arg)
    if not user:
        print(f"Error: User '{arg}' not found.")
        sys.exit(1)

    # Check if news is enabled
    was_disabled = False
    if not user.news_schedule_enabled:
        print(f"Warning: News schedule is not enabled for user '{user.username}'.")
        print("The job will still run, but normally it would be skipped.")

        # Temporarily enable it for testing
        db = SessionLocal()
        try:
            db_user = db.query(User).filter(User.id == user.id).first()
            db_user.news_schedule_enabled = True
            db.commit()
            print("Temporarily enabled news schedule for this test run.")
            was_disabled = True
        finally:
            db.close()

    try:
        # Run the news job
        asyncio.run(run_news_for_user(user.id, user.username))
    finally:
        # Restore original setting if we changed it
        if was_disabled:
            db = SessionLocal()
            try:
                db_user = db.query(User).filter(User.id == user.id).first()
                db_user.news_schedule_enabled = False
                db.commit()
                print("Restored news schedule to disabled.")
            finally:
                db.close()


if __name__ == "__main__":
    main()
