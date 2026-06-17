#!/usr/bin/env python3

import os
import time
import datetime
import json
import pytz
import psycopg2
import logging
import threading
import requests
from config import (
    SQL_USER, SQL_PASS, SQL_HOST, SQL_DATABASE,
    PLEROMA_ENDPOINT, MISSKEY_SERVER,
    REPORT_PROMPT, OPENAI_ENDPOINT, PLEROMA_ADMIN_TOKEN,
    BOT_BLACKLIST, AUTO_NARRATE,
    PLEROMA_ACCESS_TOKEN, MISSKEY_ACCESS_TOKEN
)
from pleroma import post_to_fediverse as pleroma_post, post_image_to_fediverse as pleroma_post_image
from ai import generate_reply
from tts import generate_speech_with_retries, generate_narration_video
import misskey

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
            response = requests.post(f"{MISSKEY_SERVER}/api/i", json={"i": MISSKEY_ACCESS_TOKEN}, timeout=10)
            if response.status_code == 200:
                avatar_url = response.json().get("avatarUrl")
        elif PLEROMA_ENDPOINT and PLEROMA_ACCESS_TOKEN:
            response = requests.get(f"{PLEROMA_ENDPOINT}/api/v1/accounts/verify_credentials", headers={"Authorization": f"Bearer {PLEROMA_ACCESS_TOKEN}"}, timeout=10)
            if response.status_code == 200:
                avatar_url = response.json().get("avatar")
    except Exception as e:
        logging.error(f"[REPORTBOT] Failed to fetch bot avatar: {e}")
    _bot_avatar_cache[cache_key] = avatar_url
    return avatar_url

# State file paths (relative to script location)
_SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
LAST_PLEROMA_REPORT_ID_FILE = os.path.join(_SCRIPT_DIR, ".last_pleroma_report_id")
LAST_MISSKEY_REPORT_ID_FILE = os.path.join(_SCRIPT_DIR, ".last_misskey_report_id")

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


def get_last_report_id(filepath):
    """Get the last processed report ID from file"""
    try:
        if os.path.exists(filepath):
            with open(filepath, 'r') as f:
                return f.read().strip()
    except Exception as e:
        logging.error(f"Failed to read last report ID: {e}")
    return None


def save_last_report_id(filepath, report_id):
    """Save the last processed report ID to file using atomic write"""
    temp_file = filepath + ".tmp"
    try:
        with open(temp_file, 'w') as f:
            f.write(str(report_id))
        os.rename(temp_file, filepath)
        logging.debug(f"Saved last report ID: {report_id}")
    except Exception as e:
        logging.error(f"Failed to save last report ID: {e}")
        if os.path.exists(temp_file):
            try:
                os.remove(temp_file)
            except OSError:
                pass


def cleanup_processed_file(filepath, max_entries=1000):
    """Keep only the last N entries to prevent file from growing indefinitely"""
    try:
        if os.path.exists(filepath):
            with open(filepath, 'r') as f:
                lines = f.readlines()
            if len(lines) > max_entries:
                # Use atomic write
                temp_file = filepath + ".tmp"
                with open(temp_file, 'w') as f:
                    f.writelines(lines[-max_entries:])
                os.rename(temp_file, filepath)
                logging.info(f"Cleaned up processed reports file, kept last {max_entries} entries")
    except OSError as e:
        logging.error(f"Failed to cleanup processed file: {e}")


def get_instance_name():
    """Get the instance name from configuration"""
    if MISSKEY_SERVER:
        return MISSKEY_SERVER.replace('https://', '').replace('http://', '')
    elif PLEROMA_ENDPOINT:
        return PLEROMA_ENDPOINT.replace('https://', '').replace('http://', '')
    return "our instance"


def generate_report_message(report_details):
    """Generate AI report message with fallback"""
    fallback_message = f"New user report: {report_details[:200]}..."

    # Check if OpenAI is configured
    if not OPENAI_ENDPOINT or not OPENAI_ENDPOINT.startswith(("http://", "https://")):
        logging.debug("OpenAI not configured, using fallback message")
        return fallback_message

    try:
        # Build the prompt with report details
        prompt = REPORT_PROMPT.format(report_details=report_details)
        prompt += " /no_think"

        logging.info(f"Generating AI report message")
        ai_message = generate_reply(prompt)

        if ai_message and "None" not in ai_message and len(ai_message) > 10:
            logging.info(f"AI generated report: {ai_message[:100]}...")
            return ai_message
        else:
            logging.warning("AI returned invalid response, using fallback message")
            return fallback_message

    except Exception as e:
        logging.error(f"Error generating AI report message: {e}, using fallback")
        return fallback_message


def fetch_pleroma_reports():
    """Fetch reports from Pleroma Admin API"""
    if not PLEROMA_ENDPOINT or not PLEROMA_ADMIN_TOKEN:
        logging.error("Pleroma endpoint or admin token not configured")
        return []

    url = f"{PLEROMA_ENDPOINT}/api/v1/pleroma/admin/reports?state=open&limit=50"
    headers = {"Authorization": f"Bearer {PLEROMA_ADMIN_TOKEN}"}

    try:
        response = requests.get(url, headers=headers, timeout=30)
        response.raise_for_status()
        data = response.json()
        return data.get('reports', [])
    except requests.RequestException as e:
        logging.error(f"Failed to fetch Pleroma reports: {e}")
        return []


def report_pleroma(print_only=False):
    """Check for new Pleroma reports and post about them"""
    # Cleanup old processed entries to prevent file growth
    cleanup_processed_file(LAST_PLEROMA_REPORT_ID_FILE + ".processed")

    last_id = get_last_report_id(LAST_PLEROMA_REPORT_ID_FILE)

    # Fetch reports from Admin API
    reports = fetch_pleroma_reports()

    if not reports:
        logging.debug("No Pleroma reports found")
        return

    # Sort by created_at to process in chronological order (oldest first)
    reports.sort(key=lambda r: r.get('created_at', ''))

    # Load processed IDs from file
    processed_ids = set()
    if os.path.exists(LAST_PLEROMA_REPORT_ID_FILE + ".processed"):
        try:
            with open(LAST_PLEROMA_REPORT_ID_FILE + ".processed", 'r') as f:
                processed_ids.update(line.strip() for line in f if line.strip())
        except OSError:
            pass

    if not last_id:
        # First run - mark ALL current reports as processed to avoid duplicates
        # Use atomic write to prevent corruption on crash
        temp_file = LAST_PLEROMA_REPORT_ID_FILE + ".processed.tmp"
        try:
            with open(temp_file, 'w') as f:
                for r in reports:
                    f.write(f"{r.get('id')}\n")
            os.rename(temp_file, LAST_PLEROMA_REPORT_ID_FILE + ".processed")
        except OSError as e:
            logging.error(f"Failed to write processed IDs: {e}")
            if os.path.exists(temp_file):
                try:
                    os.remove(temp_file)
                except OSError:
                    pass
        latest_report = max(reports, key=lambda r: r.get('created_at', ''))
        save_last_report_id(LAST_PLEROMA_REPORT_ID_FILE, latest_report.get('id'))
        logging.info(f"Initialized Pleroma report tracking. Marked {len(reports)} existing reports as processed.")
        return

    # Add last_id to processed set
    processed_ids.add(last_id)

    # Filter to only new reports
    new_reports = [r for r in reports if r.get('id') not in processed_ids]

    if not new_reports:
        logging.debug("No new Pleroma reports found")
        return

    # Get instance domain for local users
    instance_domain = PLEROMA_ENDPOINT.replace('https://', '').replace('http://', '')

    for report in new_reports:
        report_id = report.get('id')

        try:
            # Extract reporter info from actor
            actor = report.get('actor', {})
            reporter_acct = actor.get('acct', 'Unknown')
            # Add domain if local user (no @ in acct)
            if '@' not in reporter_acct and reporter_acct != 'Unknown':
                reporter_acct = f"{reporter_acct}@{instance_domain}"

            # Extract reported user info from account
            account = report.get('account', {})
            target_acct = account.get('acct', 'Unknown')
            # Add domain if local user (no @ in acct)
            if '@' not in target_acct and target_acct != 'Unknown':
                target_acct = f"{target_acct}@{instance_domain}"

            # Skip reports involving bots to prevent bot-to-bot loops
            reporter_lower = reporter_acct.lower()
            target_lower = target_acct.lower()
            if any(bot in reporter_lower or bot in target_lower for bot in BOT_BLACKLIST):
                logging.debug(f"Skipping Pleroma report {report_id} - involves bot user")
                continue

            # Get report content/reason (truncate to prevent abuse)
            content = report.get('content', 'No reason provided')
            if not content:
                content = 'No reason provided'
            if len(content) > 500:
                content = content[:500] + '...'

            # Check for reported statuses (posts)
            statuses = report.get('statuses', [])
            post_url = None
            if statuses and len(statuses) > 0:
                post_url = statuses[0].get('url') or statuses[0].get('uri')

            if post_url:
                report_details = f"Reporter: @{reporter_acct}, Reported: @{target_acct}, Post: {post_url}, Reason: {content}"
            else:
                report_details = f"Reporter: @{reporter_acct}, Reported: @{target_acct}, Reason: {content}"

            # Generate AI message
            message = generate_report_message(report_details)

            logging.info(f"Posting report: {report_id}")

            if print_only:
                print(f"Would post: {message}")
            else:
                # Generate TTS video if auto_narrate is enabled
                audio_bytes = None
                video_bytes = None
                if AUTO_NARRATE:
                    logging.info("[TTS] Generating video for report notification...")
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
                    if video_bytes or audio_bytes:
                        pleroma_post_image(message, audio_bytes=audio_bytes, video_bytes=video_bytes)
                    else:
                        pleroma_post(message)
                    logging.info(f"Successfully posted report {report_id}")
                except Exception as e:
                    logging.error(f"Failed to post report {report_id}: {e}")

            # Mark this report as processed
            try:
                with open(LAST_PLEROMA_REPORT_ID_FILE + ".processed", 'a') as f:
                    f.write(f"{report_id}\n")
            except Exception as e:
                logging.error(f"Failed to save processed report ID: {e}")
            save_last_report_id(LAST_PLEROMA_REPORT_ID_FILE, report_id)

        except Exception as e:
            logging.error(f"Failed to process report {report_id}: {e}")


def report_misskey(print_only=False):
    """Check for new Misskey reports and post about them"""
    last_id = get_last_report_id(LAST_MISSKEY_REPORT_ID_FILE)

    if last_id:
        query = """
        SELECT r.id, r.comment,
               u1.username as reporter_username,
               u2.username as target_username,
               r."reporterHost",
               r."targetUserHost"
        FROM abuse_user_report r
        LEFT JOIN "user" u1 ON r."reporterId" = u1.id
        LEFT JOIN "user" u2 ON r."targetUserId" = u2.id
        WHERE r.id > %s
        ORDER BY r.id ASC
        LIMIT 50;
        """
        rows = run_psql(query, (last_id,))
    else:
        # First run - get the most recent report to establish baseline
        query = """
        SELECT r.id, r.comment,
               u1.username as reporter_username,
               u2.username as target_username,
               r."reporterHost",
               r."targetUserHost"
        FROM abuse_user_report r
        LEFT JOIN "user" u1 ON r."reporterId" = u1.id
        LEFT JOIN "user" u2 ON r."targetUserId" = u2.id
        ORDER BY r.id DESC
        LIMIT 1;
        """
        rows = run_psql(query)

    if not rows:
        logging.debug("No new Misskey reports found")
        return

    # Update last processed ID
    latest_report_id = rows[-1][0] if last_id else rows[0][0]
    save_last_report_id(LAST_MISSKEY_REPORT_ID_FILE, latest_report_id)

    # If first run, don't post anything - just set baseline
    if not last_id:
        logging.info(f"Initialized Misskey report tracking. Last report ID: {latest_report_id}")
        return

    for row in rows:
        report_id, comment, reporter_username, target_username, reporter_host, target_host = row

        # Format usernames with host for remote users
        reporter = reporter_username or "Unknown"
        if reporter_host:
            reporter = f"{reporter}@{reporter_host}"

        target = target_username or "Unknown"
        if target_host:
            target = f"{target}@{target_host}"

        # Skip reports involving bots to prevent bot-to-bot loops
        reporter_lower = reporter.lower()
        target_lower = target.lower()
        if any(bot in reporter_lower or bot in target_lower for bot in BOT_BLACKLIST):
            logging.debug(f"Skipping Misskey report {report_id} - involves bot user")
            continue

        reason = comment or "No reason provided"
        post_url = None

        # Check if comment contains JSON with post URLs (for post reports)
        if reason and isinstance(reason, str) and reason.strip().startswith('['):
            try:
                urls = json.loads(reason)
                if isinstance(urls, list):
                    # Find the note/post URL (contains /notes/)
                    for url in urls:
                        if isinstance(url, str) and '/notes/' in url:
                            post_url = url
                            break
                    # Use first non-null item as reason if no text
                    reason = "Reported post"
            except (json.JSONDecodeError, TypeError):
                pass  # Keep original reason if not valid JSON

        if post_url:
            report_details = f"Reporter: @{reporter}, Reported: @{target}, Post: {post_url}, Reason: {reason}"
        else:
            report_details = f"Reporter: @{reporter}, Reported: @{target}, Reason: {reason}"

        # Generate AI message
        message = generate_report_message(report_details)

        logging.info(f"Posting report: {report_id}")

        if print_only:
            print(f"Would post: {message}")
        else:
            # Generate TTS video if auto_narrate is enabled
            audio_bytes = None
            video_bytes = None
            if AUTO_NARRATE:
                logging.info("[TTS] Generating video for report notification...")
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
                if video_bytes or audio_bytes:
                    misskey.post_image_to_fediverse(message, audio_bytes=audio_bytes, video_bytes=video_bytes)
                else:
                    misskey.post_to_fediverse(message)
                logging.info(f"Successfully posted report {report_id}")
            except Exception as e:
                logging.error(f"Failed to post report {report_id}: {e}")


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
    """Pleroma report bot daemon loop"""
    global conn
    if conn is None:
        init_db()

    while True:
        print("Running Report Bot (Pleroma)")
        try:
            report_pleroma()
        except Exception as e:
            logging.error(f"Error in report_pleroma: {e}")
            import traceback
            traceback.print_exc()
        time.sleep(60)


def misskey_background():
    """Misskey report bot daemon loop"""
    global conn
    if conn is None:
        init_db()

    while True:
        print("Running Report Bot (Misskey)")
        try:
            report_misskey()
        except Exception as e:
            logging.error(f"Error in report_misskey: {e}")
            import traceback
            traceback.print_exc()
        time.sleep(60)


if __name__ == "__main__":
    import sys

    if len(sys.argv) < 2:
        print("Usage:")
        print("  daemon          - Run Pleroma report bot daemon")
        print("  misskey-daemon  - Run Misskey report bot daemon")
        print("  pleroma [print] - Check Pleroma reports")
        print("  misskey [print] - Check Misskey reports")
        sys.exit(0)

    cmd = sys.argv[1]
    arg2 = sys.argv[2] if len(sys.argv) > 2 else None

    init_db()

    if cmd == "daemon":
        waitToStart()
        background()
    elif cmd == "misskey-daemon":
        waitToStart()
        misskey_background()
    elif cmd == "pleroma":
        report_pleroma(print_only=(arg2 == "print"))
    elif cmd == "misskey":
        report_misskey(print_only=(arg2 == "print"))
    else:
        print("Unknown command. Use no arguments to see usage.")

    if conn:
        conn.close()
