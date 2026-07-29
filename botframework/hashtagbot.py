#!/usr/bin/env python3

import json
import os
import time
import datetime
import pytz
import requests
import logging
import signal
import sys
import re
from urllib.parse import unquote

# Import BLOCK_PHRASE from main config
try:
    from config import BLOCK_PHRASE
except ImportError:
    BLOCK_PHRASE = None

# Handle ctrl+c gracefully
def signal_handler(sig, frame):
    print('\n\nShutting down...')
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s"
)

# Configuration - can be overridden by environment or external config
TIMEZONE = os.getenv("TIMEZONE", "MST")
try:
    HASHTAG_LIMIT = int(os.getenv("HASHTAG_LIMIT", "20"))
except ValueError:
    HASHTAG_LIMIT = 20

# Posting times (list of hours to post at)
HASHTAG_POST_HOURS = [6, 18]  # 6am and 6pm

# Source for trending hashtags
FEDIBUZZ_URL = "https://fedi.buzz/"
FEDIBUZZ_ENGLISH_URL = "https://fedi.buzz/in/en"  # English only
MASTODON_TRENDING_URL = "https://mastodon.social/api/v1/trends/tags"

# Platform Configuration (can be overridden by external config)
PLATFORM_TYPE = os.getenv("PLATFORM_TYPE", "pleroma")  # "pleroma"
PLEROMA_ENDPOINT = os.getenv("PLEROMA_ENDPOINT", "")
PLEROMA_ACCESS_TOKEN = os.getenv("PLEROMA_ACCESS_TOKEN", "")


def get_config():
    """Validate platform configuration.

    All settings come from environment variables, set by botctl.py / the
    installer from bots_config.py.
    """
    if os.getenv("PLATFORM_TYPE") and os.getenv("PLEROMA_ENDPOINT"):
        logging.info(f"Using environment variables (PLATFORM_TYPE={PLATFORM_TYPE})")
    else:
        logging.warning("No platform configuration found; set PLATFORM_TYPE and an endpoint")


def is_english_hashtag(tag):
    """
    Check if a hashtag is English (ASCII letters only).
    Filters out Japanese, Korean, Chinese, Arabic, etc.
    """
    # Remove the # if present
    tag = tag.lstrip('#')

    # Check if the tag contains only ASCII letters, numbers, and underscores
    # This filters out non-Latin characters (Japanese, Chinese, Korean, Arabic, etc.)
    if not re.match(r'^[a-zA-Z0-9_]+$', tag):
        return False

    # Additional filter: must have at least one letter (not just numbers)
    if not re.search(r'[a-zA-Z]', tag):
        return False

    return True


def fetch_fedibuzz_hashtags(limit=10, english_only=True):
    """
    Fetch trending hashtags from fedi.buzz by scraping the HTML.
    Returns a list of English-only hashtag names.
    """
    url = FEDIBUZZ_ENGLISH_URL if english_only else FEDIBUZZ_URL

    try:
        response = requests.get(url, timeout=30, headers={
            'User-Agent': 'HashtagBot/1.0 (Fediverse bot)'
        })
        response.raise_for_status()
        html = response.text

        # Extract hashtags from href="/tags/..." or href="https://domain/tags/..."
        pattern = r'href="[^"]+/tags/([^"]+)"'
        matches = re.findall(pattern, html)

        # URL decode and count unique hashtags
        hashtag_counts = {}
        for match in matches:
            # URL decode the hashtag
            tag = unquote(match)

            # Skip non-English hashtags
            if not is_english_hashtag(tag):
                continue

            # Normalize to lowercase
            tag = tag.lower()

            # Skip very long hashtags (likely spam)
            if len(tag) > 30:
                continue

            # Skip very short hashtags
            if len(tag) < 3:
                continue

            hashtag_counts[tag] = hashtag_counts.get(tag, 0) + 1

        # Sort by count and return top N
        sorted_tags = sorted(hashtag_counts.items(), key=lambda x: x[1], reverse=True)
        return [tag for tag, count in sorted_tags[:limit]]

    except Exception as e:
        logging.error(f"Failed to fetch from fedi.buzz: {e}")
        return []


def fetch_mastodon_trending_hashtags(limit=10):
    """
    Fetch trending hashtags from Mastodon API (mastodon.social as fallback).
    Returns only English hashtag names.
    """
    try:
        response = requests.get(
            MASTODON_TRENDING_URL,
            timeout=30,
            params={'limit': limit * 2},  # Get extra to filter
            headers={'User-Agent': 'HashtagBot/1.0 (Fediverse bot)'}
        )
        response.raise_for_status()
        try:
            data = response.json()
        except json.JSONDecodeError:
            logging.error(f"Mastodon trending API returned invalid JSON: {response.text[:200]}")
            return []

        hashtags = []
        for tag in data:
            name = tag.get('name', '')
            if name and is_english_hashtag(name):
                hashtags.append(name.lower())
                if len(hashtags) >= limit:
                    break

        return hashtags

    except Exception as e:
        logging.error(f"Failed to fetch from Mastodon API: {e}")
        return []


def get_trending_hashtags(limit=10):
    """
    Get trending English hashtags from fedi.buzz, falling back to Mastodon API.
    """
    # Try fedi.buzz English page first
    hashtags = fetch_fedibuzz_hashtags(limit, english_only=True)

    if len(hashtags) < limit:
        # Try all languages and filter to English
        logging.info("Not enough English hashtags, trying all languages with filter")
        more_hashtags = fetch_fedibuzz_hashtags(limit, english_only=False)
        for tag in more_hashtags:
            if tag not in hashtags:
                hashtags.append(tag)
            if len(hashtags) >= limit:
                break

    if len(hashtags) < limit:
        # Fallback to Mastodon API
        logging.info("Falling back to Mastodon API")
        more_hashtags = fetch_mastodon_trending_hashtags(limit)
        for tag in more_hashtags:
            if tag not in hashtags:
                hashtags.append(tag)
            if len(hashtags) >= limit:
                break

    return hashtags[:limit]


def format_hashtag_post(hashtags):
    """
    Format the trending hashtags into a post.
    Each hashtag is formatted as #hashtag to be clickable.
    """
    if not hashtags:
        return None

    server_tz = pytz.timezone(TIMEZONE) if TIMEZONE else pytz.UTC
    now = datetime.datetime.now(server_tz)
    date_str = now.strftime("%Y-%m-%d")

    lines = [f"Trending Hashtags on the Fediverse - {date_str}", ""]

    for tag in hashtags:
        # Ensure hashtag starts with #
        if not tag.startswith("#"):
            tag = f"#{tag}"
        lines.append(tag)

    lines.append("")
    lines.append("Click on any hashtag to explore!")

    return "\n".join(lines)


def post_to_pleroma(message):
    """Post a message to Pleroma"""
    if not PLEROMA_ENDPOINT or not PLEROMA_ACCESS_TOKEN:
        logging.error("Pleroma endpoint or access token not configured")
        return False

    # Prevent sending any message that contains the BLOCK_PHRASE
    if BLOCK_PHRASE and BLOCK_PHRASE in message:
        logging.warning("Message contains blocked phrase; not sending to Pleroma.")
        return False

    endpoint = PLEROMA_ENDPOINT.rstrip('/')
    if not endpoint.startswith(('http://', 'https://')):
        endpoint = f"https://{endpoint}"

    url = f"{endpoint}/api/v1/statuses"
    headers = {"Authorization": f"Bearer {PLEROMA_ACCESS_TOKEN}"}
    data = {"status": message, "visibility": "public"}

    try:
        response = requests.post(url, headers=headers, data=data, timeout=30)
        if response.status_code in (200, 202):
            logging.info("Successfully posted trending hashtags to Pleroma")
            return True
        else:
            logging.error(f"Failed to post to Pleroma: {response.status_code} - {response.text[:200]}")
            return False
    except Exception as e:
        logging.error(f"Error posting to Pleroma: {e}")
        return False


def post_trending_hashtags(print_only=False):
    """Fetch and post trending hashtags"""
    logging.info("Fetching trending hashtags from Fediverse")

    hashtags = get_trending_hashtags(HASHTAG_LIMIT)
    if not hashtags:
        logging.warning("No trending hashtags found")
        return False

    logging.info(f"Found {len(hashtags)} trending hashtags: {hashtags[:5]}...")

    message = format_hashtag_post(hashtags)
    if not message:
        logging.error("Failed to format hashtag message")
        return False

    if print_only:
        print(message)
        return True
    else:
        # Post to configured platform
        if PLEROMA_ENDPOINT:
            return post_to_pleroma(message)
        else:
            logging.error("No platform configured for posting")
            return False


def should_post_now():
    """Check if current time is a posting time"""
    server_tz = pytz.timezone(TIMEZONE) if TIMEZONE else pytz.UTC
    now = datetime.datetime.now(server_tz)
    return now.hour in HASHTAG_POST_HOURS and now.minute == 0


def waitToStart():
    """Wait until clock is at second 00 for sync"""
    server_tz = pytz.timezone(TIMEZONE) if TIMEZONE else pytz.UTC
    while True:
        now = datetime.datetime.now(server_tz)
        print(f"Waiting for Clock to be in Sync: {now}")
        if now.second == 0:
            print(f"Clock in Sync: {now}")
            break
        time.sleep(0.5)


def background():
    """Run hashtag bot in daemon mode - posts at 6am and 6pm"""
    logging.info(f"Starting hashtag bot daemon (posting at hours: {HASHTAG_POST_HOURS})")

    while True:
        server_tz = pytz.timezone(TIMEZONE) if TIMEZONE else pytz.UTC
        now = datetime.datetime.now(server_tz)
        logging.debug(f"Current Time: {now}")

        if should_post_now():
            logging.info(f"Posting trending hashtags at {now}")
            try:
                post_trending_hashtags()
            except Exception as e:
                logging.error(f"Failed to post hashtags: {e}")

        time.sleep(60)


if __name__ == "__main__":
    # Load external config if available
    get_config()

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 hashtagbot.py post [print]  - Post trending hashtags now")
        print("  python3 hashtagbot.py daemon        - Run in daemon mode (posts at 6am and 6pm)")
        print("  python3 hashtagbot.py test          - Test fetching hashtags without posting")
        print("")
        print("Configuration:")
        print("  Set environment variables:")
        print("    PLEROMA_ENDPOINT, PLEROMA_ACCESS_TOKEN  - For Pleroma")
        print("    PLATFORM_TYPE                           - 'pleroma'")
        sys.exit(0)

    cmd = sys.argv[1]
    arg2 = sys.argv[2] if len(sys.argv) > 2 else None
    print_mode = (arg2 == "print")

    try:
        if cmd == "post":
            post_trending_hashtags(print_only=print_mode)
        elif cmd == "test":
            hashtags = get_trending_hashtags(HASHTAG_LIMIT)
            print(f"Found {len(hashtags)} trending English hashtags:")
            for i, tag in enumerate(hashtags, 1):
                print(f"  {i}. #{tag}")
        elif cmd == "daemon":
            waitToStart()
            background()
        else:
            print(f"Unknown command: {cmd}")
            sys.exit(1)
    except KeyboardInterrupt:
        print("\n\nShutting down...")
        sys.exit(0)
