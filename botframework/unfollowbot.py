#!/usr/bin/env python3

import os
import time
import datetime
import pytz
import json
import sys
import psycopg2
import logging
import threading
from urllib.parse import urlparse
from config import SQL_USER, SQL_PASS, SQL_HOST, SQL_DATABASE, OPENAI_ENDPOINT, PLEROMA_ACCESS_TOKEN, PLEROMA_ENDPOINT, PLEROMA_USERNAME, UNFOLLOW_IMAGE, AUTO_NARRATE
from tts import generate_speech_with_retries, generate_narration_video
try:
    from config import UNFOLLOW_SILENT_MODE
except ImportError:
    UNFOLLOW_SILENT_MODE = False
# Environment variable can override config
UNFOLLOW_SILENT_MODE = os.getenv("UNFOLLOW_SILENT_MODE", "").lower() in ("true", "1", "yes") or UNFOLLOW_SILENT_MODE

from ai import generate_reply
from pleroma import post_to_fediverse as pleroma_post_to_fediverse, post_image_to_fediverse as pleroma_post_image_to_fediverse
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
        logging.error(f"[UNFOLLOWBOT] Failed to fetch bot avatar: {e}")
    _bot_avatar_cache[cache_key] = avatar_url
    return avatar_url

# State file paths (relative to script location)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
# Legacy on-disk names kept: renaming resets the cursor/snapshot and would re-post history.
LAST_UNFOLLOW_ID_FILE = os.path.join(_SCRIPT_DIR, ".last_misskey_unfollow_id")
FOLLOWING_SNAPSHOT_FILE = os.path.join(_SCRIPT_DIR, ".misskey_following_snapshot.json")
PLEROMA_FOLLOWING_SNAPSHOT_FILE = os.path.join(_SCRIPT_DIR, ".pleroma_following_snapshot.json")

# Ensure PLEROMA_ENDPOINT has a scheme
if PLEROMA_ENDPOINT and not PLEROMA_ENDPOINT.startswith(('http://', 'https://')):
    PLEROMA_ENDPOINT = f"https://{PLEROMA_ENDPOINT}"

logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s"
)

logging.debug(f"Unfollowbot module loaded - PLEROMA_ENDPOINT: {PLEROMA_ENDPOINT}, SQL_HOST: {SQL_HOST}")

conn = None
conn_lock = threading.Lock()


def init_db():
    global conn

    if not all([SQL_DATABASE, SQL_USER, SQL_PASS]):
        logging.error("Database configuration is incomplete. Please set SQL_DATABASE, SQL_USER, and SQL_PASS environment variables")
        sys.exit(1)

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
        sys.exit(1)


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


def load_following_snapshot(snapshot_file=FOLLOWING_SNAPSHOT_FILE):
    """Load the snapshot of following relationships from file"""
    try:
        if os.path.exists(snapshot_file):
            with open(snapshot_file, 'r') as f:
                return json.load(f)
    except Exception as e:
        logging.error(f"Failed to load following snapshot: {e}")
    return {}


def save_following_snapshot(snapshot, snapshot_file=FOLLOWING_SNAPSHOT_FILE):
    """Save the snapshot of following relationships to file using atomic write"""
    temp_file = snapshot_file + ".tmp"
    try:
        with open(temp_file, 'w') as f:
            json.dump(snapshot, f)
        os.rename(temp_file, snapshot_file)
        logging.debug(f"Saved following snapshot with {len(snapshot)} relationships")
    except Exception as e:
        logging.error(f"Failed to save following snapshot: {e}")
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError:
                pass


def get_current_follows():
    """Get all current following relationships from the instance database"""
    instance_domain = "poster.place"

    query = '''
    SELECT
        f.id,
        follower.username AS follower_username,
        COALESCE(follower.host, %s) AS follower_host,
        followee.username AS followee_username,
        COALESCE(followee.host, %s) AS followee_host,
        COALESCE(follower.uri, 'https://' || %s || '/users/' || follower.username) AS follower_uri
    FROM following f
    JOIN "user" follower ON f."followerId" = follower.id
    JOIN "user" followee ON f."followeeId" = followee.id
    WHERE follower.host IS NOT NULL AND followee.host IS NULL;
    '''

    rows = run_psql(query, (instance_domain, instance_domain, instance_domain))

    # Build a dict: key = "follower@host->followee@host", value = row data
    follows = {}
    for row in rows:
        follow_id, follower_username, follower_host, followee_username, followee_host, follower_uri = row
        key = f"{follower_username}@{follower_host}->{followee_username}@{followee_host}"
        follows[key] = {
            'id': follow_id,
            'follower_username': follower_username,
            'follower_host': follower_host,
            'followee_username': followee_username,
            'followee_host': followee_host,
            'follower_uri': follower_uri
        }

    return follows


def get_pleroma_current_follows():
    """Get all current following relationships from Pleroma database"""
    local_domain = urlparse(PLEROMA_ENDPOINT).netloc if PLEROMA_ENDPOINT else ""

    # Query following_relationships table joined with users
    query = '''
    SELECT
        fr.id,
        follower.nickname AS follower_username,
        follower.ap_id AS follower_ap_id,
        followed.nickname AS followed_username,
        followed.ap_id AS followed_ap_id
    FROM following_relationships fr
    JOIN users follower ON fr.follower_id = follower.id
    JOIN users followed ON fr.following_id = followed.id
    WHERE fr.state = 2;
    '''

    rows = run_psql(query)

    # Build a dict: key = "follower@host->followee@host", value = row data
    follows = {}
    for row in rows:
        follow_id, follower_username, follower_ap_id, followed_username, followed_ap_id = row

        # Extract domains from ap_id URLs
        follower_domain = urlparse(follower_ap_id).netloc if follower_ap_id else local_domain
        followed_domain = urlparse(followed_ap_id).netloc if followed_ap_id else local_domain

        # Only track remote users following local users
        follower_is_local = follower_domain == local_domain or not follower_ap_id
        followed_is_local = followed_domain == local_domain or not followed_ap_id
        if follower_is_local or not followed_is_local:
            continue

        key = f"{follower_username}@{follower_domain}->{followed_username}@{followed_domain}"
        follows[key] = {
            'id': follow_id,
            'follower_username': follower_username,
            'follower_domain': follower_domain,
            'follower_ap_id': follower_ap_id or f"https://{local_domain}/users/{follower_username}",
            'followed_username': followed_username,
            'followed_domain': followed_domain,
        }

    return follows


def pleroma_unfollows(print_only=False):
    """Check for unfollows in Pleroma by comparing current state to snapshot"""
    local_domain = urlparse(PLEROMA_ENDPOINT).netloc if PLEROMA_ENDPOINT else ""

    # Load previous snapshot
    old_snapshot = load_following_snapshot(PLEROMA_FOLLOWING_SNAPSHOT_FILE)

    # Get current following relationships
    current_follows = get_pleroma_current_follows()

    # First run - just save snapshot without reporting
    if not old_snapshot:
        logging.info(f"Initialized Pleroma unfollow tracking. Saved {len(current_follows)} following relationships")
        save_following_snapshot(current_follows, PLEROMA_FOLLOWING_SNAPSHOT_FILE)
        return

    # Find unfollows (in old snapshot but not in current)
    unfollows = []
    for key, data in old_snapshot.items():
        if key not in current_follows:
            unfollows.append(data)

    # Update snapshot with current state
    save_following_snapshot(current_follows, PLEROMA_FOLLOWING_SNAPSHOT_FILE)

    if not unfollows:
        logging.debug("No new Pleroma unfollows found")
        return

    logging.info(f"Found {len(unfollows)} Pleroma unfollow(s)")

    # Build messages
    matches = []
    for data in unfollows:
        unfollow_msg = f"@{data['follower_username']}@{data['follower_domain']} unfollowed @{data['followed_username']}@{data['followed_domain']}. Profile: {data['follower_ap_id']}"
        logging.info(f"Unfollow found: {unfollow_msg}")
        matches.append(unfollow_msg)

    if matches:
        msg = "\n".join(matches)

        # Use OpenAI to generate message if configured
        if OPENAI_ENDPOINT and OPENAI_ENDPOINT.startswith(("http://", "https://")):
            print("OpenAI configured. Trying to generate reply.")
            try:
                last_unfollower = unfollows[-1]['follower_username']
                prompt = f"Generate a dramatic version of this unfollow notification. Respond only with the post: {msg}. Mock {last_unfollower} for being a coward who unfollowed someone. Keep usernames in exact format @user@domain without quotes. /no_think"
                ai_msg = generate_reply(prompt)
                if ai_msg and "None" not in ai_msg:
                    msg = ai_msg
                else:
                    print("OpenAI returned invalid response, using original message")
            except Exception as e:
                print(f"Error generating OpenAI response: {e}, using original message")
        else:
            print("OpenAI not configured, using original message")

        if not msg or "None" in msg:
            logging.error("Error, msg is null.")
        else:
            if print_only:
                logging.info(f"Print-only mode, not posting: {msg}")
                print(msg)
            else:
                logging.info(f"Preparing to post unfollow notification: {msg}")
                print(msg)

                if not PLEROMA_ENDPOINT:
                    logging.error("PLEROMA_ENDPOINT is not configured. Cannot post to Fediverse.")
                    return

                # Generate TTS video if auto_narrate is enabled
                audio_bytes = None
                video_bytes = None
                if AUTO_NARRATE:
                    logging.info("[TTS] Generating video for unfollow notification...")
                    avatar_url = get_bot_avatar_url()
                    video_bytes = generate_narration_video(msg, avatar_url)
                    if video_bytes:
                        logging.info(f"[TTS] Generated {len(video_bytes)} bytes of video")
                    else:
                        logging.warning("[TTS] Video generation failed, trying audio...")
                        audio_bytes = generate_speech_with_retries(msg)
                        if audio_bytes:
                            logging.info(f"[TTS] Generated {len(audio_bytes)} bytes of audio")

                try:
                    if UNFOLLOW_IMAGE and os.path.exists(UNFOLLOW_IMAGE):
                        logging.info("Posting with unfollow image")
                        with open(UNFOLLOW_IMAGE, 'rb') as f:
                            image_bytes = f.read()
                        pleroma_post_image_to_fediverse(msg, image_bytes, audio_bytes=audio_bytes, video_bytes=video_bytes)
                        logging.info("Successfully posted unfollow notification with image")
                    else:
                        logging.warning(f"Unfollow image not found at {UNFOLLOW_IMAGE}, posting without image")
                        pleroma_post_image_to_fediverse(msg, audio_bytes=audio_bytes, video_bytes=video_bytes)
                        logging.info("Successfully posted unfollow notification without image")
                except Exception as e:
                    logging.error(f"Failed to post: {e}")


def pleroma_unfollows_wrapper():
    """Wrapper for pleroma_unfollows to catch and log errors in thread"""
    try:
        logging.debug("Checking for new Pleroma unfollows...")
        if UNFOLLOW_SILENT_MODE:
            logging.info("Silent mode enabled - will not post unfollows")
        pleroma_unfollows(print_only=UNFOLLOW_SILENT_MODE)
        logging.debug("Finished checking Pleroma unfollows")
    except Exception as e:
        logging.error(f"Error in pleroma_unfollows thread: {e}")
        import traceback
        traceback.print_exc()


def waitToStart():
    while True:
        now = datetime.datetime.now(pytz.timezone("Atlantic/Reykjavik"))
        print(f"Waiting for Clock to be in Sync: {now}")
        if now.second == 0:
            print(f"Clock in Sync: {now}")
            break
        time.sleep(0.5)


def background():
    """Run in daemon mode for Pleroma"""
    global conn
    if conn is None:
        init_db()

    while True:
        print("Running in Daemon mode (Pleroma Unfollow)")
        now = datetime.datetime.now(pytz.timezone("Atlantic/Reykjavik"))
        logging.debug(f"Current Time: {now}")

        unfollows_thread = threading.Thread(target=pleroma_unfollows_wrapper)
        unfollows_thread.start()
        # Wait for thread to complete (with timeout) before starting next cycle
        unfollows_thread.join(timeout=290)
        if unfollows_thread.is_alive():
            logging.warning("Pleroma unfollows thread still running, waiting for next cycle")
        time.sleep(300)  # Check every 5 minutes


if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  daemon                    - Run in daemon mode (Pleroma)")
        print("  unfollows [print]         - Check Pleroma unfollows")
        sys.exit(0)

    cmd = sys.argv[1]
    arg2 = sys.argv[2] if len(sys.argv) > 2 else None

    # Check for PLEROMA_ACCESS_TOKEN only for Pleroma commands
    if cmd in ["daemon", "unfollows"]:
        if not PLEROMA_ACCESS_TOKEN:
            logging.error("Error: Set PLEROMA_ACCESS_TOKEN environment variable for Pleroma commands")
            sys.exit(1)

    # Initialize database connection for all commands that need it
    if cmd in ["daemon", "unfollows"]:
        init_db()

    if cmd == "daemon":
        waitToStart()
        background()
    elif cmd == "unfollows":
        pleroma_unfollows(print_only=(arg2 == "print"))
    else:
        print("Unknown command. Use no arguments to see usage.")

    if conn:
        conn.close()
