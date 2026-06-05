#!/usr/bin/env python3

import os
import re
import time
import datetime
import pytz
import json
import requests
import sys
import psycopg2
import logging
import threading
from urllib.parse import urlparse
from config import SQL_USER, SQL_PASS, SQL_HOST, SQL_DATABASE, OPENAI_ENDPOINT, PLEROMA_ACCESS_TOKEN, PLEROMA_ENDPOINT, PLEROMA_USERNAME, BLOCK_IMAGE, MISSKEY_SERVER, MISSKEY_ACCESS_TOKEN, BLOCK_LIMIT, AUTO_NARRATE, BLOCK_PROMPT
from ai import generate_reply
from tts import generate_speech_with_retries, generate_narration_video
from pleroma import post_to_fediverse as pleroma_post_to_fediverse, post_image_to_fediverse as pleroma_post_image_to_fediverse
import misskey
import engagement

# Cache for bot avatar URL
_bot_avatar_cache = {}

def get_bot_avatar_url():
    """Fetch the bot's avatar URL from the API"""
    global _bot_avatar_cache

    cache_key = f"{MISSKEY_SERVER or PLEROMA_ENDPOINT}"
    if cache_key in _bot_avatar_cache:
        return _bot_avatar_cache[cache_key]

    avatar_url = None
    try:
        if MISSKEY_SERVER and MISSKEY_ACCESS_TOKEN:
            # Misskey API
            response = requests.post(
                f"{MISSKEY_SERVER}/api/i",
                json={"i": MISSKEY_ACCESS_TOKEN},
                timeout=10
            )
            if response.status_code == 200:
                data = response.json()
                avatar_url = data.get("avatarUrl")
                logging.info(f"[BLOCKBOT] Fetched Misskey avatar: {avatar_url}")
        elif PLEROMA_ENDPOINT and PLEROMA_ACCESS_TOKEN:
            # Pleroma API
            response = requests.get(
                f"{PLEROMA_ENDPOINT}/api/v1/accounts/verify_credentials",
                headers={"Authorization": f"Bearer {PLEROMA_ACCESS_TOKEN}"},
                timeout=10
            )
            if response.status_code == 200:
                data = response.json()
                avatar_url = data.get("avatar")
                logging.info(f"[BLOCKBOT] Fetched Pleroma avatar: {avatar_url}")
    except Exception as e:
        logging.error(f"[BLOCKBOT] Failed to fetch bot avatar: {e}")

    _bot_avatar_cache[cache_key] = avatar_url
    return avatar_url


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


def get_misskey_active_user_stats():
    """Get Daily Active Users (DAU) and Monthly Active Users (MAU) from Misskey database"""
    dau_query = """
    SELECT COUNT(*) FROM "user"
    WHERE host IS NULL
      AND "isSuspended" = false
      AND "lastActiveDate" >= NOW() - INTERVAL '1 day';
    """
    mau_query = """
    SELECT COUNT(*) FROM "user"
    WHERE host IS NULL
      AND "isSuspended" = false
      AND "lastActiveDate" >= NOW() - INTERVAL '30 days';
    """

    try:
        dau_rows = run_psql(dau_query)
        mau_rows = run_psql(mau_query)

        dau = dau_rows[0][0] if dau_rows else 0
        mau = mau_rows[0][0] if mau_rows else 0
    except Exception as e:
        logging.error(f"Failed to get Misskey active user stats: {e}")
        dau, mau = 0, 0

    return dau, mau


def validate_block_message(ai_msg, original_msg):
    """
    Validate that AI-generated message contains the correct usernames and domains.
    Returns True if valid, False otherwise.
    """
    if not ai_msg or not original_msg:
        return False
    
    # Extract usernames and domains from original message
    # Format: "BLOCKER: @user@domain blocked @user2@domain2. Profile is available at: url"
    # Extract blocker and blockee info from original
    blocker_match = re.search(r'BLOCKER:\s*@([^@]+)@([^\s]+)', original_msg)
    blockee_match = re.search(r'blocked\s+@([^@]+)@([^\s]+)', original_msg)
    
    if not blocker_match or not blockee_match:
        logging.warning("Could not parse original block message for validation")
        return False
    
    blocker_username = blocker_match.group(1)
    blocker_domain = blocker_match.group(2)
    blockee_username = blockee_match.group(1)
    blockee_domain = blockee_match.group(2)
    
    # Check if AI message contains the correct usernames and domains
    # Allow for case-insensitive matching and different formatting
    ai_msg_lower = ai_msg.lower()
    
    blocker_found = blocker_username.lower() in ai_msg_lower and blocker_domain.lower() in ai_msg_lower
    blockee_found = blockee_username.lower() in ai_msg_lower and blockee_domain.lower() in ai_msg_lower
    
    if not blocker_found or not blockee_found:
        logging.warning(f"AI message validation failed: blocker_found={blocker_found}, blockee_found={blockee_found}")
        logging.warning(f"Expected blocker: @{blocker_username}@{blocker_domain}, blockee: @{blockee_username}@{blockee_domain}")
        logging.warning(f"AI message: {ai_msg[:200]}...")
        return False
    
    # Check for non-ASCII characters that might indicate language mixing issues
    # Allow common punctuation and emojis, but flag suspicious patterns
    non_ascii_chars = [c for c in ai_msg if ord(c) > 127 and c not in '…—–•']
    # Check if there are Chinese/Japanese/Korean characters (common issue)
    cjk_pattern = re.compile(r'[\u4e00-\u9fff\u3040-\u309f\u30a0-\u30ff\uac00-\ud7af]')
    if cjk_pattern.search(ai_msg):
        logging.warning("AI message contains CJK characters, which may indicate language mixing")
        return False
    
    return True


def validate_scalps_message(ai_msg, original_msg):
    """
    Validate that AI-generated leaderboard message contains the correct usernames.
    Returns True if valid, False otherwise.
    """
    if not ai_msg or not original_msg:
        return False

    # Extract usernames from original message (format: @username: N)
    usernames = re.findall(r'@(\S+?):', original_msg)

    if not usernames:
        return False

    ai_msg_lower = ai_msg.lower()

    for username in usernames:
        if username.lower() not in ai_msg_lower:
            logging.warning(f"AI scalps message missing username: @{username}")
            return False

    return True


LAST_BLOCK_ID_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".last_misskey_block_id")

# Ensure PLEROMA_ENDPOINT has a scheme
if PLEROMA_ENDPOINT and not PLEROMA_ENDPOINT.startswith(('http://', 'https://')):
    PLEROMA_ENDPOINT = f"https://{PLEROMA_ENDPOINT}"

logging.basicConfig(
    level=logging.DEBUG, format="%(asctime)s [%(levelname)s] %(message)s"
)

# Debug: Log configuration on import
logging.debug(f"Blockbot module loaded - PLEROMA_ENDPOINT: {PLEROMA_ENDPOINT}, MISSKEY_SERVER: {MISSKEY_SERVER}, SQL_HOST: {SQL_HOST}")

conn = None
conn_lock = threading.Lock()


def init_db():
    global conn

    # Check if database configuration is set (SQL_HOST can be empty for Unix socket)
    if not all([SQL_DATABASE, SQL_USER, SQL_PASS]):
        logging.error("Database configuration is incomplete. Please set SQL_DATABASE, SQL_USER, and SQL_PASS environment variables")
        sys.exit(1)

    logging.debug(
        f"Connecting to DB: host={SQL_HOST if SQL_HOST else 'Unix socket'}, dbname={SQL_DATABASE}, user={SQL_USER}"
    )
    try:
        # If SQL_HOST is empty, psycopg2 will use Unix socket
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
            # Connection was lost, try to reconnect
            logging.warning(f"Database connection lost: {e}. Attempting to reconnect...")
            conn = None  # Clear stale connection before reconnect attempt
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
                # Retry the query
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


def get_last_block_id():
    """Get the last processed Misskey block ID from file"""
    try:
        if os.path.exists(LAST_BLOCK_ID_FILE):
            with open(LAST_BLOCK_ID_FILE, 'r') as f:
                return f.read().strip()
    except Exception as e:
        logging.error(f"Failed to read last block ID: {e}")
    return None


def save_last_block_id(block_id):
    """Save the last processed Misskey block ID to file using atomic write"""
    temp_file = LAST_BLOCK_ID_FILE + ".tmp"
    try:
        with open(temp_file, 'w') as f:
            f.write(block_id)
        os.rename(temp_file, LAST_BLOCK_ID_FILE)
        logging.debug(f"Saved last block ID: {block_id}")
    except Exception as e:
        logging.error(f"Failed to save last block ID: {e}")
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError:
                pass


def misskey_blocks(print_only=False):
    """Check for new blocks in Misskey database"""
    # Get the instance domain (strip https:// if present)
    if MISSKEY_SERVER:
        instance_domain = MISSKEY_SERVER.replace('https://', '').replace('http://', '')
    else:
        instance_domain = "poster.place"

    # Get last processed block ID
    last_id = get_last_block_id()

    # Build query to get new blocks (using parameterized queries for safety)
    if last_id:
        query = '''
        SELECT
            b.id,
            blocker.username AS blocker_username,
            COALESCE(blocker.host, %s) AS blocker_host,
            blockee.username AS blockee_username,
            COALESCE(blockee.host, %s) AS blockee_host,
            COALESCE(blocker.uri, 'https://' || %s || '/users/' || blocker.username) AS blocker_uri
        FROM blocking b
        JOIN "user" blocker ON b."blockerId" = blocker.id
        JOIN "user" blockee ON b."blockeeId" = blockee.id
        WHERE b.id > %s
        ORDER BY b.id ASC
        LIMIT 50;
        '''
        params = (instance_domain, instance_domain, instance_domain, last_id)
    else:
        # First run - only get most recent block to establish baseline
        query = '''
        SELECT
            b.id,
            blocker.username AS blocker_username,
            COALESCE(blocker.host, %s) AS blocker_host,
            blockee.username AS blockee_username,
            COALESCE(blockee.host, %s) AS blockee_host,
            COALESCE(blocker.uri, 'https://' || %s || '/users/' || blocker.username) AS blocker_uri
        FROM blocking b
        JOIN "user" blocker ON b."blockerId" = blocker.id
        JOIN "user" blockee ON b."blockeeId" = blockee.id
        ORDER BY b.id DESC
        LIMIT 1;
        '''
        params = (instance_domain, instance_domain, instance_domain)

    logging.debug(f"Running Misskey blocks query with last_id: {last_id}")
    rows = run_psql(query, params)

    if not rows:
        logging.debug("No new blocks found")
        return

    # Update last processed ID to the most recent block
    latest_block_id = rows[-1][0] if last_id else rows[0][0]
    save_last_block_id(latest_block_id)

    # If this is the first run, don't post anything
    if not last_id:
        logging.info(f"Initialized Misskey block tracking. Last block ID: {latest_block_id}")
        return

    # Build message from new blocks
    logging.info(f"Found {len(rows)} new block(s)")
    matches = []
    for row in rows:
        block_id, blocker_username, blocker_host, blockee_username, blockee_host, blocker_uri = row
        # Format: "BLOCKER: @user blocked @user2"
        block_msg = f"BLOCKER: @{blocker_username}@{blocker_host} blocked @{blockee_username}@{blockee_host}. Profile is available at: {blocker_uri}"
        logging.info(f"Block found: {block_msg}")
        matches.append(block_msg)

    if matches:
        msg = "\n".join(matches)

        # Use OpenAI to generate message if configured
        if OPENAI_ENDPOINT and OPENAI_ENDPOINT.startswith("https://"):
            print("OpenAI configured. Trying to generate reply.")
            try:
                original_msg = msg  # Save original for validation
                prompt = BLOCK_PROMPT.format(block_details=msg) + " /no_think"
                ai_msg = generate_reply(prompt)
                if ai_msg and "None" not in ai_msg:
                    ai_msg = ai_msg.replace("/no_think", "").strip()
                    # Validate the AI message preserves correct usernames and domains
                    if validate_block_message(ai_msg, original_msg):
                        msg = ai_msg
                        msg = re.sub(r'\bBLOCKEE:\s*', '', msg)  # Never show "BLOCKEE:" in the post
                        logging.info("AI message passed validation")
                    else:
                        logging.warning("AI message failed validation, using original message")
                        print("AI message failed validation (incorrect usernames/domains or language mixing), using original message")
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
                logging.info(f"Preparing to post block notification: {msg}")
                print(msg)
                # Check if MISSKEY_ACCESS_TOKEN is configured
                if not MISSKEY_ACCESS_TOKEN:
                    logging.error("MISSKEY_ACCESS_TOKEN is not configured. Cannot post to Fediverse.")
                    return
                # Generate TTS video if auto_narrate is enabled
                audio_bytes = None
                video_bytes = None
                if AUTO_NARRATE:
                    logging.info("[TTS] Generating video for block notification...")
                    avatar_url = get_bot_avatar_url()
                    video_bytes = generate_narration_video(msg, avatar_url)
                    if video_bytes:
                        logging.info(f"[TTS] Generated {len(video_bytes)} bytes of video")
                    else:
                        logging.warning("[TTS] Video generation failed, trying audio...")
                        audio_bytes = generate_speech_with_retries(msg)
                        if audio_bytes:
                            logging.info(f"[TTS] Generated {len(audio_bytes)} bytes of audio")

                # Read the image file and post with image
                try:
                    if BLOCK_IMAGE and os.path.exists(BLOCK_IMAGE):
                        logging.info("Posting with block image")
                        with open(BLOCK_IMAGE, 'rb') as f:
                            image_bytes = f.read()
                        misskey.post_image_to_fediverse(msg, image_bytes, audio_bytes=audio_bytes, video_bytes=video_bytes)
                        logging.info("Successfully posted block notification with image")
                    else:
                        logging.warning(f"Block image not found at {BLOCK_IMAGE}, posting without image")
                        misskey.post_image_to_fediverse(msg, audio_bytes=audio_bytes, video_bytes=video_bytes)
                        logging.info("Successfully posted block notification without image")
                except Exception as e:
                    logging.error(f"Failed to post: {e}")


def blocks(print_only=False):
    # 1. Get current timestamp in %y-%m-%d %H:%M
    now = datetime.datetime.now(pytz.timezone("Atlantic/Reykjavik"))
    # Calculate the timestamp for the previous minute
    previous_minute = now - datetime.timedelta(minutes=1)
    now_str = previous_minute.strftime("%y-%m-%d %H:%M")

    # 2. Run query and store all block activity rows
    query = "SELECT * FROM activities WHERE data->>'type' = 'Block';"
    data_rows = run_psql(
        query
    )  # each row expected to have at least (id, data_dict, created_at, ...)

    # 3. Iterate and filter by timestamp matching now_str (up to minute)
    matches = []
    for row in data_rows:
        # Assuming inserted_at is index 2 or 3; adjust if needed
        inserted_at = row[3] if len(row) > 2 else None
        if not inserted_at:
            continue
        # Format inserted_at to '%y-%m-%d %H:%M' for comparison
        inserted_str = inserted_at.strftime("%y-%m-%d %H:%M")
        if inserted_str != now_str:
            continue

        data = row[1]  # data field assumed index 1
        print(f"Debug Row Data: {inserted_at}")
        actor_url = data.get("actor", "")
        object_url = data.get("object", "")
        if not actor_url or not object_url:
            continue

        # Debug: Log the raw URLs from database
        logging.debug(f"Raw actor_url from DB: {actor_url}")
        logging.debug(f"Raw object_url from DB: {object_url}")

        # Extract username from URL (last path segment)
        blocker = actor_url.rstrip("/").split("/")[-1]
        blocked = object_url.rstrip("/").split("/")[-1]

        # Skip if username extraction failed (empty or looks like a domain)
        if not blocker or not blocked or "." in blocker or "." in blocked:
            logging.warning(f"Invalid username extracted: blocker={blocker}, blocked={blocked}")
            continue

        # Extract domain from URL using urlparse for accuracy
        blocker_parsed = urlparse(actor_url)
        blocked_parsed = urlparse(object_url)
        blocker_domain = blocker_parsed.netloc
        blocked_domain = blocked_parsed.netloc

        logging.debug(f"Extracted blocker_domain: {blocker_domain}")
        logging.debug(f"Extracted blocked_domain: {blocked_domain}")

        profile = actor_url  # profile URL from object key
        # Format: "BLOCKER: @user blocked @user2"
        matches.append(
            f"BLOCKER: @{blocker}@{blocker_domain} blocked @{blocked}@{blocked_domain}. Profile is available at: {profile}"
        )
    if matches:
        msg = "\n".join(matches)
        if OPENAI_ENDPOINT and OPENAI_ENDPOINT.startswith("https://"):
            print("OpenAI configured. Trying to generate reply.")
            try:
                original_msg = msg  # Save original for validation
                prompt = BLOCK_PROMPT.format(block_details=msg) + " /no_think"
                ai_msg = generate_reply(prompt)
                if ai_msg and "None" not in ai_msg:
                    ai_msg = ai_msg.replace("/no_think", "").strip()
                    # Validate the AI message preserves correct usernames and domains
                    if validate_block_message(ai_msg, original_msg):
                        msg = ai_msg
                        msg = re.sub(r'\bBLOCKEE:\s*', '', msg)  # Never show "BLOCKEE:" in the post
                        logging.info("AI message passed validation")
                    else:
                        logging.warning("AI message failed validation, using original message")
                        print("AI message failed validation (incorrect usernames/domains or language mixing), using original message")
                else:
                    print("OpenAI returned invalid response, using original message")
            except Exception as e:
                print(f"Error generating OpenAI response: {e}, using original message")
        else:
            print("OpenAI not configured, using original message")
        if not msg or "None" in msg:
            print("Error, msg is null.")
        else:
            if print_only:
                print(msg)
            else:
                print(msg)
                # Check if PLEROMA_ENDPOINT is configured
                if not PLEROMA_ENDPOINT:
                    logging.error("PLEROMA_ENDPOINT is not configured. Cannot post to Fediverse.")
                    return

                # Generate TTS video if auto_narrate is enabled
                audio_bytes = None
                video_bytes = None
                if AUTO_NARRATE:
                    logging.info("[TTS] Generating video for block notification...")
                    avatar_url = get_bot_avatar_url()
                    video_bytes = generate_narration_video(msg, avatar_url)
                    if video_bytes:
                        logging.info(f"[TTS] Generated {len(video_bytes)} bytes of video")
                    else:
                        logging.warning("[TTS] Video generation failed, trying audio...")
                        audio_bytes = generate_speech_with_retries(msg)
                        if audio_bytes:
                            logging.info(f"[TTS] Generated {len(audio_bytes)} bytes of audio")

                # Read the image file and post with image
                try:
                    if BLOCK_IMAGE and os.path.exists(BLOCK_IMAGE):
                        with open(BLOCK_IMAGE, 'rb') as f:
                            image_bytes = f.read()
                        pleroma_post_image_to_fediverse(msg, image_bytes, audio_bytes=audio_bytes, video_bytes=video_bytes)
                    else:
                        logging.warning(f"Block image not found at {BLOCK_IMAGE}, posting without image")
                        pleroma_post_image_to_fediverse(msg, audio_bytes=audio_bytes, video_bytes=video_bytes)
                except Exception as e:
                    logging.error(f"Failed to post: {e}")
                    # Don't try to post again if it already failed

def scalps(print_only=False):
    instance = PLEROMA_ENDPOINT.replace("https://", "").replace("http://", "")
    title = "Block Leaderboard:"
    block_query = """
        SELECT u.nickname, COUNT(*) as block_count
        FROM user_relationships r
        JOIN users u ON r.target_id = u.id
        WHERE r.relationship_type = 1
        AND u.ap_id LIKE %s
        GROUP BY u.nickname
        HAVING COUNT(*) >= %s
        ORDER BY block_count DESC;
    """
    block_rows = run_psql(block_query, (f"%{instance}%", BLOCK_LIMIT,))

    output_lines = [
        f"@{row[0]}: {row[1]}"
        for row in block_rows
    ]
    # Blank line between entries so Markdown renderers (Pleroma/Misskey) don't
    # collapse single newlines into spaces and mash everything onto one line.
    output_str = "\n\n".join(output_lines)

    FINAL = f"{title} \n\n {output_str}"
    print(FINAL)

    if OPENAI_ENDPOINT and OPENAI_ENDPOINT.startswith("https://"):
        print("OpenAI configured. Trying to generate reply.")
        try:
            original_final = FINAL
            prompt = f"Format the following blocklist data as a social media post. Use EXACTLY the usernames provided below - do NOT invent or replace any usernames. Keep all @ symbols and block counts exactly as shown. Add a brief introductory sentence and congratulate the top blockers. {FINAL}"
            msg = generate_reply(prompt)
            if not msg:
                print("OpenAI Response is null")
            elif validate_scalps_message(msg, original_final):
                FINAL = msg
                print(f"Got a response from OpenAI: {FINAL}")
            else:
                print("AI scalps message failed validation (wrong usernames), using original message")
                logging.warning("AI scalps message failed validation, using original leaderboard")
        except Exception as e:
            print(f"Error generating OpenAI response: {e}")
    else:
        print("OpenAI not configured, using plain leaderboard")
    if not FINAL or "None" in FINAL:
        print("Error, FINAL is null.")
    else:
        if print_only:
            print(FINAL)
        else:
            print(FINAL)
            pleroma_post_to_fediverse(FINAL)


def misskey_scalps(print_only=False):
    """Generate a leaderboard of most blocked users in Misskey"""
    # Get the instance domain (strip https:// if present)
    if MISSKEY_SERVER:
        instance_domain = MISSKEY_SERVER.replace('https://', '').replace('http://', '')
    else:
        instance_domain = "poster.place"

    # Get all local users
    user_query = f"SELECT id, username FROM \"user\" WHERE host IS NULL;"
    user_rows = run_psql(user_query)

    users = {row[0]: row[1] for row in user_rows if row[1] and row[1] not in ("internal.fetch", "nickname", "relay")}
    user_ids = set(users.keys())

    title = "Block Leaderboard:"

    if not user_ids:
        logging.warning("No local users found")
        return

    # Count blocks for each local user (using parameterized query)
    # Counts "blockeeId" = who GOT blocked (how many blocks they received)
    block_query = '''
    SELECT "blockeeId", COUNT(*) as block_count
    FROM blocking
    WHERE "blockeeId" = ANY(%s)
    GROUP BY "blockeeId"
    HAVING COUNT(*) >= %s
    ORDER BY block_count DESC;
    '''

    block_rows = run_psql(block_query, (list(user_ids), BLOCK_LIMIT))

    output_lines = [
        f"@{users[row[0]]}: {row[1]}"
        for row in block_rows
        if row[0] in users
    ]
    # Blank line between entries so Markdown renderers (Pleroma/Misskey) don't
    # collapse single newlines into spaces and mash everything onto one line.
    output_str = "\n\n".join(output_lines)

    FINAL = f"{title} \n\n {output_str}"
    print(FINAL)

    if OPENAI_ENDPOINT and OPENAI_ENDPOINT.startswith("https://"):
        print("OpenAI configured. Trying to generate reply.")
        try:
            original_final = FINAL
            prompt = f"Format the following blocklist data as a social media post. Use EXACTLY the usernames provided below - do NOT invent or replace any usernames. Keep all @ symbols and block counts exactly as shown. Add a brief introductory sentence and congratulate the top blockers. {FINAL}"
            msg = generate_reply(prompt)
            if not msg:
                print("OpenAI Response is null")
            elif validate_scalps_message(msg, original_final):
                FINAL = msg
                print(f"Got a response from OpenAI: {FINAL}")
            else:
                print("AI scalps message failed validation (wrong usernames), using original message")
                logging.warning("AI scalps message failed validation, using original leaderboard")
        except Exception as e:
            print(f"Error generating OpenAI response: {e}")
    else:
        print("OpenAI not configured, using plain leaderboard")

    if not FINAL or "None" in FINAL:
        print("Error, FINAL is null.")
    else:
        if print_only:
            print(FINAL)
        else:
            print(FINAL)
            misskey.post_to_fediverse(FINAL)


def fba():
    url = "https://fba.ryona.agency/top?blocked=20"
    try:
        r = requests.get(url, timeout=30)
        if r.status_code != 200:
            logging.error("Failed to fetch FBA data")
            return
        data = r.json()
        # Validate response structure
        if not isinstance(data, list):
            logging.error("Unexpected FBA response format - expected list")
            return
    except requests.RequestException as e:
        logging.error(f"Failed to fetch FBA data: {e}")
        return
    except ValueError as e:
        logging.error(f"Failed to parse FBA JSON: {e}")
        return

    MESSAGE = "Top Most Defederated Instances: \n\n"
    for item in data:
        if not isinstance(item, dict):
            continue
        domain = item.get("domain", "")
        highscore = item.get("highscore", "")
        # Blank line between entries so Markdown renderers (Pleroma/Misskey)
        # don't collapse single newlines into spaces and mash onto one line.
        MESSAGE += f"{domain} : {highscore}\n\n"
    logging.info(MESSAGE)

    # Post to appropriate platform
    if MISSKEY_SERVER:
        misskey.post_to_fediverse(MESSAGE)
    elif PLEROMA_ENDPOINT:
        pleroma_post_to_fediverse(MESSAGE)
    else:
        logging.error("Neither MISSKEY_SERVER nor PLEROMA_ENDPOINT is configured")


def waitToStart():
    while True:
        now = datetime.datetime.now(pytz.timezone("Atlantic/Reykjavik"))
        print(f"Waiting for Clock to be in Sync: {now}")
        if now.second == 0:
            print(f"Clock in Sync: {now}")
            break
        time.sleep(0.5)


def background():
    # Initialize database connection if not already done
    global conn
    if conn is None:
        init_db()

    while True:
        print("Running in Daemon mode (Pleroma)")
        now = datetime.datetime.now(pytz.timezone("Atlantic/Reykjavik"))
        logging.debug(f"Current Time: {now}")
        if now.hour == 1 and now.minute == 15:
            logging.debug(f"Running FBA at {now}")
            fba()
        if now.hour == 1 and now.minute == 15:
            logging.debug(f"Running Scalps at {now}")
            scalps()
        if now.hour == 1 and now.minute == 0:
            logging.debug(f"Running Daily Top Posts at {now}")
            engagement.daily_top_posts()
            time.sleep(5)
            logging.debug(f"Running DAU/MAU Stats at {now}")
            engagement.post_active_user_stats()
        blocks_thread = threading.Thread(target=blocks)
        blocks_thread.start()
        blocks_thread.join(timeout=55)  # Wait for thread with timeout to prevent leak
        if blocks_thread.is_alive():
            logging.warning("Blocks thread still running after timeout, continuing anyway")
        time.sleep(60)


def misskey_blocks_wrapper():
    """Wrapper for misskey_blocks to catch and log errors in thread"""
    try:
        logging.debug("Checking for new Misskey blocks...")
        misskey_blocks()
        logging.debug("Finished checking Misskey blocks")
    except Exception as e:
        logging.error(f"Error in misskey_blocks thread: {e}")
        import traceback
        traceback.print_exc()


def misskey_background():
    # Initialize database connection if not already done
    global conn
    if conn is None:
        init_db()

    while True:
        print("Running in Daemon mode (Misskey)")
        now = datetime.datetime.now(pytz.timezone("Atlantic/Reykjavik"))
        logging.debug(f"Current Time: {now}")
        if now.hour == 1 and now.minute == 15:
            logging.debug(f"Running FBA at {now}")
            fba()
        if now.hour == 1 and now.minute == 30:
            logging.debug(f"Running Misskey Scalps at {now}")
            misskey_scalps()
        if now.hour == 2 and now.minute == 0:
            logging.debug(f"Running Misskey Daily Top Posts at {now}")
            engagement.misskey_daily_top_posts()
            time.sleep(5)
            logging.debug(f"Running Misskey DAU/MAU Stats at {now}")
            engagement.misskey_post_active_user_stats()
        misskey_blocks_thread = threading.Thread(target=misskey_blocks_wrapper)
        misskey_blocks_thread.start()
        misskey_blocks_thread.join(timeout=55)  # Wait for thread with timeout to prevent leak
        if misskey_blocks_thread.is_alive():
            logging.warning("Misskey blocks thread still running after timeout, continuing anyway")
        time.sleep(60)

if __name__ == "__main__":
    if len(sys.argv) < 2:
        print("Usage:")
        print("  daemon                  - Run in daemon mode (Pleroma)")
        print("  blocks [print]          - Check Pleroma blocks")
        print("  scalps [print]          - Generate Pleroma block leaderboard")
        print("  misskey-daemon          - Run in daemon mode (Misskey)")
        print("  misskey-blocks [print]  - Check Misskey blocks")
        print("  misskey-scalps [print]  - Generate Misskey block leaderboard")
        print("  fba                     - Post FBA top defederated instances")
        sys.exit(0)

    cmd = sys.argv[1]
    arg2 = sys.argv[2] if len(sys.argv) > 2 else None

    # Check for PLEROMA_ACCESS_TOKEN only for Pleroma commands
    if cmd in ["daemon", "blocks", "scalps", "fba"]:
        if not PLEROMA_ACCESS_TOKEN:
            logging.error("Error: Set PLEROMA_ACCESS_TOKEN environment variable for Pleroma commands")
            sys.exit(1)

    # Check for MISSKEY_ACCESS_TOKEN only for Misskey commands
    if cmd in ["misskey-daemon", "misskey-blocks", "misskey-scalps"]:
        if not MISSKEY_ACCESS_TOKEN:
            logging.error("Error: Set MISSKEY_ACCESS_TOKEN environment variable for Misskey commands")
            sys.exit(1)

    # Initialize database connection for all commands that need it
    if cmd in ["daemon", "blocks", "scalps", "misskey-daemon", "misskey-blocks", "misskey-scalps"]:
        init_db()

    if cmd == "daemon":
        waitToStart()
        background()
    elif cmd == "blocks":
        blocks(print_only=(arg2 == "print"))
    elif cmd == "scalps":
        scalps(print_only=(arg2 == "print"))
    elif cmd == "misskey-daemon":
        waitToStart()
        misskey_background()
    elif cmd == "misskey-blocks":
        misskey_blocks(print_only=(arg2 == "print"))
    elif cmd == "misskey-scalps":
        misskey_scalps(print_only=(arg2 == "print"))
    elif cmd == "fba":
        fba()
    else:
        print("Unknown command. Use no arguments to see usage.")

    if conn:
        conn.close()
