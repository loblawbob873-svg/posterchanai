#!/usr/bin/env python3

import os
import time
import datetime
import pytz
import psycopg2
import logging
import threading
from config import (
    SQL_USER, SQL_PASS, SQL_HOST, SQL_DATABASE,
    PLEROMA_ENDPOINT,
    WELCOME_IMAGE, WELCOME_MESSAGE, WELCOME_LOOKBACK_MINUTES,
    WELCOME_PROMPT, OPENAI_ENDPOINT, AUTO_NARRATE,
    PLEROMA_ACCESS_TOKEN
)
from pleroma import post_image_to_fediverse as pleroma_post_image, post_to_fediverse as pleroma_post
from ai import generate_reply
from tts import generate_speech_with_retries, generate_narration_video
import requests

# Cache for bot avatar URL
_bot_avatar_cache = {}

def get_bot_avatar_url():
    """Fetch the bot's avatar URL from the API"""
    global _bot_avatar_cache
    cache_key = f"{PLEROMA_ENDPOINT}"
    if cache_key in _bot_avatar_cache:
        return _bot_avatar_cache[cache_key]
    avatar_url = None
    try:
        if PLEROMA_ENDPOINT and PLEROMA_ACCESS_TOKEN:
            response = requests.get(f"{PLEROMA_ENDPOINT}/api/v1/accounts/verify_credentials", headers={"Authorization": f"Bearer {PLEROMA_ACCESS_TOKEN}"}, timeout=10)
            if response.status_code == 200:
                avatar_url = response.json().get("avatar")
    except Exception as e:
        logging.error(f"[WELCOMEBOT] Failed to fetch bot avatar: {e}")
    _bot_avatar_cache[cache_key] = avatar_url
    return avatar_url

# State file paths (relative to script location)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
WELCOMED_PLEROMA_FILE = os.path.join(_SCRIPT_DIR, ".welcomed_pleroma_users")
# Legacy on-disk names kept: renaming resets the cursor and would re-welcome every user.
WELCOMED_USERS_FILE = os.path.join(_SCRIPT_DIR, ".welcomed_misskey_users")
LAST_USER_ID_FILE = os.path.join(_SCRIPT_DIR, ".last_welcomed_misskey_user_id")

logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s"
)

conn = None
conn_lock = threading.Lock()


def init_db():
    global conn

    if not all([SQL_DATABASE, SQL_USER, SQL_PASS]):
        logging.error("Database configuration is incomplete. Please set SQL_DATABASE, SQL_USER, and SQL_PASS environment variables")
        return

    logging.debug(
        f"Connecting to DB: host={SQL_HOST if SQL_HOST else 'Unix socket'}, dbname={SQL_DATABASE}, user={SQL_USER}"
    )
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
        logging.debug("Database connection established")
    except Exception as e:
        logging.error(f"Failed to connect to database: {e}")


def run_psql(query, params=None):
    global conn
    logging.debug(f"Executing SQL query: {query}")

    with conn_lock:
        if conn is None:
            logging.error("SQL query failed: Database connection is not initialized")
            return []

        try:
            with conn.cursor() as cur:
                cur.execute(query, params)
                try:
                    records = cur.fetchall()
                    logging.debug(f"Query returned {len(records)} rows")
                except psycopg2.ProgrammingError:
                    records = []
                    logging.debug("Query returned no rows")
                return records
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            logging.warning(f"Database connection lost: {e}. Attempting to reconnect...")
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
                logging.info("Database reconnection successful")
                with conn.cursor() as cur:
                    cur.execute(query, params)
                    try:
                        records = cur.fetchall()
                        logging.debug(f"Query returned {len(records)} rows")
                        return records
                    except psycopg2.ProgrammingError:
                        logging.debug("Query returned no rows")
                        return []
            except Exception as reconnect_error:
                logging.error(f"Failed to reconnect to database: {reconnect_error}")
                conn = None
                return []
        except Exception as e:
            logging.error(f"SQL query failed: {e}")
        return []


def load_welcomed_users(filepath):
    """Load set of already welcomed user IDs from file"""
    try:
        if os.path.exists(filepath):
            with open(filepath, 'r') as f:
                return set(line.strip() for line in f if line.strip())
    except Exception as e:
        logging.error(f"Failed to load welcomed users: {e}")
    return set()


def save_welcomed_user(filepath, user_id):
    """Append a user ID to the welcomed users file"""
    try:
        with open(filepath, 'a') as f:
            f.write(f"{user_id}\n")
        logging.debug(f"Saved welcomed user: {user_id}")
    except Exception as e:
        logging.error(f"Failed to save welcomed user: {e}")


def cleanup_welcomed_users(filepath, max_entries=1000):
    """Keep only the last N entries to prevent file from growing indefinitely"""
    try:
        if os.path.exists(filepath):
            with open(filepath, 'r') as f:
                lines = f.readlines()
            if len(lines) > max_entries:
                # Use atomic write pattern to prevent corruption
                temp_file = filepath + ".tmp"
                with open(temp_file, 'w') as f:
                    f.writelines(lines[-max_entries:])
                os.rename(temp_file, filepath)
                logging.info(f"Cleaned up welcomed users file, kept last {max_entries} entries")
    except Exception as e:
        logging.error(f"Failed to cleanup welcomed users: {e}")
        # Clean up temp file if it exists (avoid TOCTOU by just trying to remove)
        try:
            temp_file = filepath + ".tmp"
            os.remove(temp_file)
        except (OSError, FileNotFoundError):
            pass


def get_instance_name():
    """Get the instance name from configuration"""
    if PLEROMA_ENDPOINT:
        return PLEROMA_ENDPOINT.replace('https://', '').replace('http://', '')
    return "our instance"


def generate_welcome_message(username, instance_name):
    """Generate AI welcome message with fallback to simple message"""
    fallback_message = f"@{username} {WELCOME_MESSAGE.format(instance_name=instance_name)}"

    # Check if OpenAI is configured
    if not OPENAI_ENDPOINT or not OPENAI_ENDPOINT.startswith(("http://", "https://")):
        logging.debug("OpenAI not configured, using fallback message")
        return fallback_message

    try:
        # Build the prompt with username and instance name
        prompt = WELCOME_PROMPT.format(username=username, instance_name=instance_name)
        prompt += " /no_think"

        logging.info(f"Generating AI welcome message for {username}")
        ai_message = generate_reply(prompt)

        if ai_message and "None" not in ai_message and len(ai_message) > 10:
            # Ensure message contains @username for notification (only add if not present)
            if f"@{username}" not in ai_message:
                ai_message = f"@{username} {ai_message}"
            logging.info(f"AI generated welcome: {ai_message[:100]}...")
            return ai_message
        else:
            logging.warning("AI returned invalid response, using fallback message")
            return fallback_message

    except Exception as e:
        logging.error(f"Error generating AI welcome message: {e}, using fallback")
        return fallback_message


def welcome_pleroma(print_only=False):
    """Check for new Pleroma users and send welcome messages"""
    welcomed = load_welcomed_users(WELCOMED_PLEROMA_FILE)

    # Use parameterized query with make_interval for safe interval construction
    query = """
    SELECT id, nickname, inserted_at
    FROM users
    WHERE local = true
      AND inserted_at >= NOW() - make_interval(mins => %s)
      AND nickname NOT IN ('internal.fetch', 'relay')
    ORDER BY inserted_at ASC;
    """

    rows = run_psql(query, (int(WELCOME_LOOKBACK_MINUTES),))

    if not rows:
        logging.debug("No new Pleroma users found")
        return

    instance_name = get_instance_name()

    for row in rows:
        user_id, nickname, created_at = row
        user_id_str = str(user_id)

        if user_id_str in welcomed:
            logging.debug(f"User {nickname} already welcomed, skipping")
            continue

        # Generate AI welcome message with fallback
        message = generate_welcome_message(nickname, instance_name)

        logging.info(f"Welcoming new user: {nickname}")

        if print_only:
            print(f"Would post: {message}")
        else:
            # Generate TTS video if auto_narrate is enabled
            audio_bytes = None
            video_bytes = None
            if AUTO_NARRATE:
                logging.info(f"[TTS] Generating video for welcome message...")
                avatar_url = get_bot_avatar_url()
                video_bytes = generate_narration_video(message, avatar_url)
                if video_bytes:
                    logging.info(f"[TTS] Generated {len(video_bytes)} bytes of video")
                else:
                    logging.warning("[TTS] Video generation failed, trying audio...")
                    audio_bytes = generate_speech_with_retries(message)
                    if audio_bytes:
                        logging.info(f"[TTS] Generated {len(audio_bytes)} bytes of audio")

            try:
                if WELCOME_IMAGE and os.path.exists(WELCOME_IMAGE):
                    with open(WELCOME_IMAGE, 'rb') as f:
                        image_bytes = f.read()
                    pleroma_post_image(message, image_bytes, audio_bytes=audio_bytes, video_bytes=video_bytes)
                else:
                    logging.warning(f"Welcome image not found at {WELCOME_IMAGE}, posting without image")
                    pleroma_post_image(message, audio_bytes=audio_bytes, video_bytes=video_bytes)

                save_welcomed_user(WELCOMED_PLEROMA_FILE, user_id_str)
                welcomed.add(user_id_str)
                logging.info(f"Successfully welcomed {nickname}")

            except Exception as e:
                logging.error(f"Failed to welcome {nickname}: {e}")

    cleanup_welcomed_users(WELCOMED_PLEROMA_FILE)


def get_last_user_id():
    """Get the last processed user ID from file"""
    try:
        if os.path.exists(LAST_USER_ID_FILE):
            with open(LAST_USER_ID_FILE, 'r') as f:
                return f.read().strip()
    except Exception as e:
        logging.error(f"Failed to read last user ID: {e}")
    return None


def save_last_user_id(user_id):
    """Save the last processed user ID to file using atomic write"""
    temp_file = LAST_USER_ID_FILE + ".tmp"
    try:
        with open(temp_file, 'w') as f:
            f.write(user_id)
        os.rename(temp_file, LAST_USER_ID_FILE)
        logging.debug(f"Saved last user ID: {user_id}")
    except Exception as e:
        logging.error(f"Failed to save last user ID: {e}")
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError:
                pass


def waitToStart():
    """Sync to clock minute"""
    while True:
        now = datetime.datetime.now(pytz.timezone("Atlantic/Reykjavik"))
        print(f"Waiting for Clock to be in Sync: {now}")
        if now.second == 0:
            print(f"Clock in Sync: {now}")
            break
        time.sleep(0.5)


def background():
    """Pleroma welcome bot daemon loop"""
    global conn
    if conn is None:
        init_db()

    while True:
        print("Running Welcome Bot (Pleroma)")
        try:
            welcome_pleroma()
        except Exception as e:
            logging.error(f"Error in welcome_pleroma: {e}")
            import traceback
            traceback.print_exc()
        time.sleep(60)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  daemon          - Run Pleroma welcome bot daemon")
        print("  pleroma [print] - Check Pleroma new users")
        sys.exit(0)

    cmd = sys.argv[1]
    arg2 = sys.argv[2] if len(sys.argv) > 2 else None

    init_db()

    if cmd == "daemon":
        waitToStart()
        background()
    elif cmd == "pleroma":
        welcome_pleroma(print_only=(arg2 == "print"))
    else:
        print("Unknown command. Use no arguments to see usage.")

    if conn:
        conn.close()
