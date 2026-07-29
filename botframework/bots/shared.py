"""
Shared utilities for all bot modules.
Provides common functionality for database access, state management,
avatar caching, clock synchronization, and media posting.
"""

import os
import time
import datetime
import json
import logging
import threading
import requests
import pytz
import psycopg2

from config import (
    SQL_USER, SQL_PASS, SQL_HOST, SQL_DATABASE,
    PLEROMA_ENDPOINT, PLEROMA_ACCESS_TOKEN,
)

# Try to import optional configs
try:
    from config import AUTO_NARRATE
except ImportError:
    AUTO_NARRATE = False

logger = logging.getLogger(__name__)

# Script directory for state files
_SCRIPT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


# =============================================================================
# DATABASE UTILITIES
# =============================================================================

# Global database connection and lock
_db_conn = None
_db_lock = threading.Lock()


def get_db_connection():
    """Get the global database connection."""
    global _db_conn
    return _db_conn


def init_db():
    """Initialize database connection."""
    global _db_conn

    try:
        with _db_lock:
            if _db_conn is None or _db_conn.closed:
                if SQL_HOST:
                    _db_conn = psycopg2.connect(
                        host=SQL_HOST,
                        database=SQL_DATABASE,
                        user=SQL_USER,
                        password=SQL_PASS,
                        connect_timeout=10
                    )
                else:
                    # Unix socket connection
                    _db_conn = psycopg2.connect(
                        database=SQL_DATABASE,
                        user=SQL_USER,
                        password=SQL_PASS,
                        connect_timeout=10
                    )
                logger.info("Database connection established")
                return True
    except Exception as e:
        # Log error without potentially exposing credentials
        error_type = type(e).__name__
        logger.error(f"Failed to connect to database: {error_type}")
        logger.debug(f"Database connection error details: {e}")
        _db_conn = None
        return False

    return True


def run_psql(query, params=None, bot_name="BOT"):
    """Execute SQL query with automatic reconnection on failure.

    Args:
        query: SQL query string with %s placeholders
        params: Tuple of query parameters
        bot_name: Name for logging purposes

    Returns:
        List of result rows, or empty list on failure
    """
    global _db_conn

    with _db_lock:
        if _db_conn is None or _db_conn.closed:
            if not init_db():
                return []

        try:
            with _db_conn.cursor() as cur:
                cur.execute(query, params)
                try:
                    return cur.fetchall()
                except psycopg2.ProgrammingError:
                    # Query didn't return results (e.g., UPDATE)
                    return []
        except (psycopg2.OperationalError, psycopg2.InterfaceError) as e:
            logger.warning(f"[{bot_name}] Database connection lost: {e}. Reconnecting...")
            _db_conn = None
            if init_db():
                try:
                    with _db_conn.cursor() as cur:
                        cur.execute(query, params)
                        try:
                            return cur.fetchall()
                        except psycopg2.ProgrammingError:
                            return []
                except Exception as retry_error:
                    logger.error(f"[{bot_name}] Query failed after reconnect: {retry_error}")
                    return []
            return []
        except Exception as e:
            logger.error(f"[{bot_name}] SQL query failed: {e}")
            return []


# =============================================================================
# AVATAR CACHING
# =============================================================================

_avatar_cache = {}


def get_bot_avatar_url(bot_name="BOT"):
    """Fetch the bot's avatar URL from the API with caching.

    Args:
        bot_name: Name for logging purposes

    Returns:
        Avatar URL string or None
    """
    global _avatar_cache

    cache_key = f"{PLEROMA_ENDPOINT}"
    if cache_key in _avatar_cache:
        return _avatar_cache[cache_key]

    avatar_url = None
    try:
        if PLEROMA_ENDPOINT and PLEROMA_ACCESS_TOKEN:
            response = requests.get(
                f"{PLEROMA_ENDPOINT}/api/v1/accounts/verify_credentials",
                headers={"Authorization": f"Bearer {PLEROMA_ACCESS_TOKEN}"},
                timeout=10
            )
            if response.status_code == 200:
                avatar_url = response.json().get("avatar")
    except Exception as e:
        logger.error(f"[{bot_name}] Failed to fetch bot avatar: {e}")

    _avatar_cache[cache_key] = avatar_url
    return avatar_url


# =============================================================================
# STATE FILE MANAGEMENT
# =============================================================================

def load_state_file(filename, default=None):
    """Load state from a file.

    Args:
        filename: Filename (relative to script dir) or absolute path
        default: Default value if file doesn't exist

    Returns:
        File contents as string, or default value
    """
    if not os.path.isabs(filename):
        filepath = os.path.join(_SCRIPT_DIR, filename)
    else:
        filepath = filename

    if os.path.exists(filepath):
        try:
            with open(filepath, 'r') as f:
                return f.read().strip()
        except Exception as e:
            logger.error(f"Failed to read state file {filepath}: {e}")

    return default


def save_state_file(filename, content):
    """Save state to a file atomically.

    Args:
        filename: Filename (relative to script dir) or absolute path
        content: Content to write (will be converted to string)
    """
    if not os.path.isabs(filename):
        filepath = os.path.join(_SCRIPT_DIR, filename)
    else:
        filepath = filename

    tmp_path = filepath + ".tmp"
    try:
        with open(tmp_path, 'w') as f:
            f.write(str(content))
        os.replace(tmp_path, filepath)
    except Exception as e:
        logger.error(f"Failed to save state file {filepath}: {e}")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError as cleanup_err:
                logger.debug(f"Failed to cleanup temp file {tmp_path}: {cleanup_err}")


def cleanup_state_file(filename, max_entries=1000):
    """Keep only the last N entries in a state file.

    Args:
        filename: Filename (relative to script dir) or absolute path
        max_entries: Maximum number of lines to keep
    """
    if not os.path.isabs(filename):
        filepath = os.path.join(_SCRIPT_DIR, filename)
    else:
        filepath = filename

    if not os.path.exists(filepath):
        return

    try:
        with open(filepath, 'r') as f:
            lines = f.readlines()

        if len(lines) > max_entries:
            with open(filepath, 'w') as f:
                f.writelines(lines[-max_entries:])
            logger.debug(f"Cleaned up {filepath}: kept last {max_entries} entries")
    except Exception as e:
        logger.error(f"Failed to cleanup state file {filepath}: {e}")


def load_json_state(filename, default=None):
    """Load JSON state from a file.

    Args:
        filename: Filename (relative to script dir) or absolute path
        default: Default value if file doesn't exist or is invalid

    Returns:
        Parsed JSON data, or default value
    """
    if not os.path.isabs(filename):
        filepath = os.path.join(_SCRIPT_DIR, filename)
    else:
        filepath = filename

    if os.path.exists(filepath):
        try:
            with open(filepath, 'r') as f:
                return json.load(f)
        except Exception as e:
            logger.error(f"Failed to read JSON state file {filepath}: {e}")

    return default if default is not None else {}


def save_json_state(filename, data):
    """Save JSON state to a file atomically.

    Args:
        filename: Filename (relative to script dir) or absolute path
        data: Data to serialize as JSON
    """
    if not os.path.isabs(filename):
        filepath = os.path.join(_SCRIPT_DIR, filename)
    else:
        filepath = filename

    tmp_path = filepath + ".tmp"
    try:
        with open(tmp_path, 'w') as f:
            json.dump(data, f)
        os.replace(tmp_path, filepath)
    except Exception as e:
        logger.error(f"Failed to save JSON state file {filepath}: {e}")
        if os.path.exists(tmp_path):
            try:
                os.remove(tmp_path)
            except OSError as cleanup_err:
                logger.debug(f"Failed to cleanup temp file {tmp_path}: {cleanup_err}")


# =============================================================================
# CLOCK SYNCHRONIZATION
# =============================================================================

def wait_to_start(timezone="Atlantic/Reykjavik"):
    """Wait until the start of a new minute.

    Useful for synchronizing bot runs to minute boundaries.

    Args:
        timezone: Timezone string for the clock check
    """
    tz = pytz.timezone(timezone)
    while True:
        now = datetime.datetime.now(tz)
        if now.second == 0:
            break
        time.sleep(0.5)


# =============================================================================
# INSTANCE UTILITIES
# =============================================================================

def get_instance_name():
    """Get the instance domain name from config.

    Returns:
        Domain name string (e.g., 'poster.place')
    """
    if PLEROMA_ENDPOINT:
        return PLEROMA_ENDPOINT.replace("https://", "").replace("http://", "").split("/")[0]
    return "instance"


# =============================================================================
# MEDIA POSTING UTILITIES
# =============================================================================

def prepare_media_for_post(message, bot_name="BOT"):
    """Prepare media (TTS audio/video) for a post.

    If AUTO_NARRATE is enabled, generates TTS video or audio.

    Args:
        message: Message text to narrate
        bot_name: Name for logging purposes

    Returns:
        Tuple of (audio_bytes, video_bytes) - one or both may be None
    """
    audio_bytes = None
    video_bytes = None

    if not AUTO_NARRATE:
        return audio_bytes, video_bytes

    try:
        from tts import generate_speech_with_retries, generate_narration_video

        avatar_url = get_bot_avatar_url(bot_name)
        video_bytes = generate_narration_video(message, avatar_url)

        if not video_bytes:
            audio_bytes = generate_speech_with_retries(message)

    except ImportError:
        logger.warning(f"[{bot_name}] TTS module not available")
    except Exception as e:
        logger.error(f"[{bot_name}] Failed to generate TTS: {e}")

    return audio_bytes, video_bytes


def load_image_file(image_path, bot_name="BOT"):
    """Load an image file for posting.

    Args:
        image_path: Path to the image file
        bot_name: Name for logging purposes

    Returns:
        Image bytes or None if file doesn't exist/can't be read
    """
    if not image_path or not os.path.exists(image_path):
        return None

    try:
        with open(image_path, 'rb') as f:
            return f.read()
    except Exception as e:
        logger.error(f"[{bot_name}] Failed to load image {image_path}: {e}")
        return None
