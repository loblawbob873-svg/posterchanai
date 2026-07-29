#!/usr/bin/env python3

import os
import time
import datetime
import pytz
import re
import psycopg2
import logging
import signal
import sys
import threading
from config import (
    SQL_USER, SQL_PASS, SQL_HOST, SQL_DATABASE,
    OPENAI_ENDPOINT,
    PLEROMA_ENDPOINT, TIMEZONE
)
from ai import generate_reply
from pleroma import post_to_fediverse as pleroma_post_to_fediverse
from core.utils import strip_html

# Handle ctrl+c gracefully
def signal_handler(sig, frame):
    print('\n\nShutting down...')
    sys.exit(0)

signal.signal(signal.SIGINT, signal_handler)

logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s"
)

# Database connection
conn = None
conn_lock = threading.Lock()

def init_db():
    """Initialize database connection"""
    global conn

    try:
        with conn_lock:
            if conn is None or conn.closed:
                # Support Unix socket when SQL_HOST is empty
                if SQL_HOST:
                    conn = psycopg2.connect(
                        host=SQL_HOST,
                        database=SQL_DATABASE,
                        user=SQL_USER,
                        password=SQL_PASS,
                        connect_timeout=10
                    )
                else:
                    conn = psycopg2.connect(
                        database=SQL_DATABASE,
                        user=SQL_USER,
                        password=SQL_PASS,
                        connect_timeout=10
                    )
                # Set statement timeout to 30 seconds
                cursor = conn.cursor()
                cursor.execute("SET statement_timeout = '30s'")
                conn.commit()
                cursor.close()
                logging.info("Database connection established")
    except Exception as e:
        logging.error(f"Failed to connect to database: {e}")
        conn = None

def run_psql(query, params=None):
    """Execute SQL query and return results"""
    global conn

    with conn_lock:
        # Initialize if needed within the lock to prevent race conditions
        if conn is None or conn.closed:
            try:
                if SQL_HOST:
                    conn = psycopg2.connect(
                        dbname=SQL_DATABASE, user=SQL_USER, password=SQL_PASS, host=SQL_HOST,
                        connect_timeout=10
                    )
                else:
                    conn = psycopg2.connect(
                        dbname=SQL_DATABASE, user=SQL_USER, password=SQL_PASS,
                        connect_timeout=10
                    )
                logging.debug("Database connection established in run_psql")
            except Exception as e:
                logging.error(f"Failed to connect to database: {e}")
                conn = None
                return []

        if conn is None:
            logging.error("No database connection available")
            return []

        cursor = None
        try:
            cursor = conn.cursor()
            cursor.execute(query, params)
            rows = cursor.fetchall()
            return rows
        except Exception as e:
            logging.error(f"Database query failed: {e}")
            # Mark connection as needing reconnection (already inside lock)
            try:
                if conn:
                    conn.close()
                conn = None
            except Exception:
                pass
            return []
        finally:
            if cursor:
                try:
                    cursor.close()
                except Exception:
                    pass


def extract_username(actor_url):
    """Extract username from ActivityPub actor URL"""
    if not actor_url:
        return "unknown"
    # URL format: https://domain.com/users/username
    return actor_url.rstrip("/").split("/")[-1]

def build_top_posts_query_pleroma(date_str):
    """Build SQL query for Pleroma database. Returns (query, params) tuple."""
    query = """
    SELECT
      a.data->>'object' AS post_id,
      a.data->>'actor' AS actor_url,
      COALESCE(o.data->>'content', o.data->>'summary', '') AS content,
      COALESCE((o.data->>'like_count')::int, 0) AS like_count,
      COALESCE((o.data->>'announcement_count')::int, 0) AS boost_count,
      COALESCE((o.data->>'like_count')::int, 0) + (COALESCE((o.data->>'announcement_count')::int, 0) * 2) AS engagement_score
    FROM activities a
    JOIN objects o ON o.data->>'id' = a.data->>'object'
    WHERE a.data->>'type' = 'Create'
      AND DATE(a.inserted_at) = %s::date
      AND a.data->>'actor' LIKE %s
      AND (o.data->'to' ? 'https://www.w3.org/ns/activitystreams#Public'
           OR o.data->'cc' ? 'https://www.w3.org/ns/activitystreams#Public')
    ORDER BY engagement_score DESC
    LIMIT 10;
    """
    endpoint = PLEROMA_ENDPOINT or ""
    params = (date_str, f"%{endpoint}%")
    return query, params

def _is_ai_top_posts_response_complete(ai_msg, num_posts):
    """
    Return True if we should use the AI response for the daily top posts report.
    Requires a non-empty response and at least num_posts '[View post]' links
    so truncated AI output (intro only, no posts) is rejected.
    """
    if not ai_msg or "None" in ai_msg or num_posts <= 0:
        return False
    return ai_msg.count("[View post]") >= num_posts


def daily_top_posts(print_only=False):
    """
    Generate a daily report of top engaging posts for Pleroma
    Runs at 8:00 PM daily
    """
    logging.info("Running daily_top_posts for Pleroma")

    # 1. Get current date for query (use server timezone from config)
    server_tz = pytz.timezone(TIMEZONE) if TIMEZONE else pytz.UTC
    now = datetime.datetime.now(server_tz)
    today_str = now.strftime("%Y-%m-%d")

    # 2. Execute platform-specific query
    query, params = build_top_posts_query_pleroma(today_str)
    rows = run_psql(query, params)

    if not rows or len(rows) == 0:
        logging.info("No posts found for today")
        if print_only:
            print(f"Top Posts of the Day - {today_str}\n\nNo posts found for today.")
            print("(Pleroma query returned 0 rows. Top posts use activities+objects; DAU/MAU use users table.)")
        return

    # Check if all posts have zero engagement
    if all(row[5] == 0 for row in rows):
        logging.info("No engagement today, skipping report")
        if print_only:
            print(f"Top Posts of the Day - {today_str}\n\nNo engagement today, skipping report.")
        return

    # 3. Format results (use markdown links so posts are clickable)
    output_lines = []
    for idx, row in enumerate(rows, 1):
        post_id, actor, content, likes, boosts, score = row
        username = extract_username(actor)
        # Strip HTML and truncate content to first 100 chars
        clean_content = strip_html(content)
        preview = clean_content[:100] + "..." if len(clean_content) > 100 else clean_content
        if not preview:
            preview = "[No text content]"
        # Ensure post_id is a full URL (Pleroma object id is typically the IRI)
        post_url = post_id if (post_id and "://" in str(post_id)) else (f"{PLEROMA_ENDPOINT.rstrip('/')}/objects/{post_id}" if PLEROMA_ENDPOINT and post_id else "")

        output_lines.append(
            f"{idx}. @{username} - {score} pts ({likes} likes, {boosts} boosts)\n   {preview}\n   🔗 [View post]({post_url})"
        )

    # 4. Build base message
    title = f"Top Posts of the Day - {today_str}"
    output_str = "\n\n".join(output_lines)
    FINAL = f"{title}\n\n{output_str}"

    # 5. Generate AI commentary (skip in print mode for speed)
    if OPENAI_ENDPOINT and OPENAI_ENDPOINT.startswith(("http://", "https://")):
        logging.info("Generating AI commentary for top posts")
        try:
            prompt = f"""Generate an entertaining daily social media report highlighting the top posts.

Posts are ranked by engagement: (likes/reactions) + (boosts/renotes × 2)

Requirements:
- Write ONLY in English.
- Create fun introduction (1-2 sentences)
- Present ALL {len(rows)} posts with rankings and scores
- Add BRIEF witty commentary (keep each post's description short)
- Use emojis (🏆 for top, 🔥 for high engagement)
- End with brief encouragement (1 sentence)
- Keep tone positive and fun
- Don't change the usernames 
- Don't add * or any other characters before it

IMPORTANT:
- Keep all engagement metrics visible (X likes, Y boosts)
- Maintain ranking order (1st, 2nd, 3rd)
- Include all posts in the list
- MUST include a clickable link for each post: use markdown format [View post](url) and preserve the exact URL for each post
- KEEP IT CONCISE - total output must be under 2500 characters

Data:
{FINAL}

/no_think"""

            ai_msg = generate_reply(prompt)
            if _is_ai_top_posts_response_complete(ai_msg, len(rows)):
                FINAL = ai_msg
                logging.info(f"AI generated report: {FINAL[:200]}...")
            else:
                if ai_msg and "None" not in ai_msg:
                    link_count = ai_msg.count("[View post]")
                    logging.warning(
                        f"AI response incomplete (has {link_count} links, need {len(rows)}), using original post list"
                    )
                else:
                    logging.warning("AI returned invalid response, using original")
        except Exception as e:
            logging.error(f"Error generating AI response: {e}, using original")

    # 6. Post or print
    if not FINAL or "None" in FINAL:
        logging.error("Error, FINAL is null.")
        return

    if print_only:
        print(FINAL)
    else:
        logging.info("Posting daily top posts to Pleroma")
        pleroma_post_to_fediverse(FINAL)

def get_active_user_stats():
    """Get Daily Active Users (DAU) and Monthly Active Users (MAU) from Pleroma database"""
    dau_query = """
    SELECT COUNT(*) FROM users
    WHERE local = true
      AND is_active = true
      AND last_active_at >= NOW() - INTERVAL '1 day';
    """
    mau_query = """
    SELECT COUNT(*) FROM users
    WHERE local = true
      AND is_active = true
      AND last_active_at >= NOW() - INTERVAL '30 days';
    """

    dau_rows = run_psql(dau_query)
    mau_rows = run_psql(mau_query)

    dau = dau_rows[0][0] if dau_rows else 0
    mau = mau_rows[0][0] if mau_rows else 0

    return dau, mau


def post_active_user_stats(print_only=False):
    """
    Post Daily Active Users and Monthly Active Users stats for Pleroma
    Uses AI to generate an engaging post, falls back to generic if AI fails
    """
    logging.info("Generating active user stats post for Pleroma")

    # Get stats
    dau, mau = get_active_user_stats()

    if dau == 0 and mau == 0:
        logging.warning("No active user stats available, skipping post")
        return

    # Get current date
    server_tz = pytz.timezone(TIMEZONE) if TIMEZONE else pytz.UTC
    now = datetime.datetime.now(server_tz)
    today_str = now.strftime("%Y-%m-%d")

    # Generic fallback message
    generic_post = f"📊 Instance Activity - {today_str}\n\n🔥 {dau} users active today\n📈 {mau} users active this month\n\nThanks for being part of our community!"

    FINAL = generic_post

    # Try to generate AI response
    if OPENAI_ENDPOINT and OPENAI_ENDPOINT.startswith(("http://", "https://")):
        logging.info("Generating AI commentary for user stats")
        try:
            prompt = f"""Generate an engaging social media post about our instance's user activity.

Stats for {today_str}:
- {dau} users were active today
- {mau} users were active this month

Requirements:
- Keep it short and punchy (2-4 sentences max)
- Use relevant emojis (📊, 🔥, 📈, 🎉, etc.)
- Make it sound exciting and community-focused
- Use simple language like "X users active today" and "X users active this month"
- Do NOT use acronyms like DAU or MAU
- Add a brief thank you or encouragement to users
- Keep tone positive and celebratory
- Do not use hashtags
- Respond ONLY with the social media post

/no_think"""

            ai_msg = generate_reply(prompt)
            if ai_msg and "None" not in ai_msg and len(ai_msg) > 20:
                FINAL = ai_msg
                logging.info(f"AI generated stats post: {FINAL[:100]}...")
            else:
                logging.warning("AI returned invalid response, using generic post")
        except Exception as e:
            logging.error(f"Error generating AI response: {e}, using generic post")

    # Post or print
    if print_only:
        print(FINAL)
    else:
        logging.info("Posting active user stats to Pleroma")
        pleroma_post_to_fediverse(FINAL)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  python3 engagement.py pleroma [print]  - Generate Pleroma daily top posts")
        print("  python3 engagement.py stats-pleroma [print]  - Generate Pleroma user stats")
        sys.exit(0)

    cmd = sys.argv[1]
    arg2 = sys.argv[2] if len(sys.argv) > 2 else None
    print_mode = (arg2 == "print")

    # Initialize database
    init_db()

    try:
        if cmd == "pleroma":
            daily_top_posts(print_only=print_mode)
        elif cmd == "stats-pleroma":
            post_active_user_stats(print_only=print_mode)
        else:
            print(f"Unknown command: {cmd}")
            sys.exit(1)
    except KeyboardInterrupt:
        print("\n\nShutting down...")
        sys.exit(0)
