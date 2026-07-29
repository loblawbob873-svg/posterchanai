import time
import re
import random
import os
import sys
from datetime import datetime, timedelta

# Ensure the script directory is in the Python path
script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)
from config import (
    MISSKEY_ACCESS_TOKEN,
    MISSKEY_SERVER,
    MISSKEY_USERNAME,
    BOT_BLACKLIST,
    BAD_WORDS,
    IMAGE_POSTER_FREQ,
    IMAGE_POSTER_PROMPT,
    IMAGE_POSTER_TEXT,
    IMAGE_POSTER_RANDOM_SCENES,
    AUTO_NARRATE
)
from random_scenes import RANDOM_SCENE_ELEMENTS

def imageposter():
    print("Generating Image...................")
    from image_backend import generate_image_bytes
    prompt = IMAGE_POSTER_PROMPT
    if IMAGE_POSTER_RANDOM_SCENES:
        random_scene = random.choice(RANDOM_SCENE_ELEMENTS)
        prompt = f"{IMAGE_POSTER_PROMPT}, {random_scene}"
        print(f"Using random scene: {random_scene}")
    try:
        image_bytes = generate_image_bytes(prompt)
        if image_bytes:
            print(f"Image Generation Complete ({len(image_bytes)} bytes)")
            time.sleep(60)
            post_image_to_fediverse(IMAGE_POSTER_TEXT, image_bytes=image_bytes)
        else:
            print("ERROR: imageposter - Image generation returned None")
    except Exception as e:
        print(f"ERROR: imageposter - Exception during image generation: {e}")
        import traceback
        traceback.print_exc()
        
from ai import generate_reply, is_ai_configured, ai_ping
# Route Misskey network ops through the app's shared misskey_service (Phase 4 dedup) when
# MISSKEY_USE_APP_SERVICE is truthy; default keeps the standalone misskey client. The shim
# exposes the identical surface, so the rest of this module is unchanged either way.
if os.getenv("MISSKEY_USE_APP_SERVICE", "").strip().lower() in ("true", "1", "yes"):
    import misskey_shim as _mk
    print("[misskeyListener] using app.services.misskey_service via shim")
else:
    import misskey as _mk
get_mentions = _mk.get_mentions
get_note = _mk.get_note
get_own_account = _mk.get_own_account
send_reply = _mk.send_reply
post_image_to_fediverse = _mk.post_image_to_fediverse
get_thread_history = _mk.get_thread_history
get_thread_images = _mk.get_thread_images
download_image_from_url = _mk.download_image_from_url
from searxng import search_web, smart_search, search_images, summarize_search_results, format_image_results, search_and_download_images
from tts import generate_speech_with_retries, generate_narration_video, create_avatar_video, fetch_avatar
from news import fetch_news_from_source
from core.utils import strip_html, contains_bad_words
from posterchanai_api import process_media, capture_screenshot, fetch_ytdl_media, parse_ytdl_postaction
import pytz


def _gather_note_media(note):
    """Download any files attached to a Misskey note (or its parent) for a media
    command. Returns a list of (filename, data_bytes, content_type) tuples.

    When the note has no attachment of its own (the user replied to an image post
    with e.g. `meme foo` instead of re-uploading), fall back to the parent note's
    files — using the embedded `reply` if present, otherwise fetching the parent by
    `replyId` (mentions/notifications don't always embed the parent's files)."""
    files = list(note.get("files") or [])
    # embedded parent first (cheap), then walk UP the reply chain to the nearest ancestor with
    # files — the user often replies to the bot's own (file-less) message, not directly to the
    # image note, so checking only the immediate parent missed it (the "curb" bug). The
    # immediate-parent case still resolves on the first hop, so direct replies keep working.
    if not files and isinstance(note.get("reply"), dict):
        files = list(note["reply"].get("files") or [])
    cur = note
    hops = 0
    while not files and cur.get("replyId") and hops < 5:
        parent = get_note(cur["replyId"])
        if not parent:
            break
        files = list(parent.get("files") or [])
        cur = parent
        hops += 1
    media = []
    for f in files:
        url = f.get("url")
        if not url:
            continue
        data = download_image_from_url(url, timeout=300)  # generic downloader, any file
        if data:
            media.append((f.get("name") or "file", data, f.get("type") or ""))
    return media


# Per-user ytdl cooldown to prevent download spam (mirrors the other listeners'
# _ytdl_last_request). Keyed by sender username; per-process is fine since the
# poller is single-process.
_ytdl_last_request: dict = {}
_YTDL_COOLDOWN_SECONDS = 30

_BOT_HELP_TEXT = (
    "🤖 Poster-Chan — @mention me with any of these:\n\n"
    "🔎 Info:  search <q> · images <q> · news <source> · geni <prompt> · screenshot <url>\n"
    "📥 Media (attach a file):  compress · clip <start> <end> · convert (img↔PDF)\n"
    "     ytdl <url> = audio, ytdl video <url> = video — add clip 0:10 0:30 and/or compress\n"
    "🖼 Image stamps (attach an image):  meme <text> · dildo · poo · cum · blood ·\n"
    "     bullethole · fire · nakedman · gay · blacked · kosher · blue · barked · consider · chimp · clay · uwu\n"
    "🎬 Effects (image → music/clip video):  hava · indian · yakety · yamete · curb ·\n"
    "     depressing · fahh · helpme · gong · fbi · redeem · gigity · beavis · smell · hood ·\n"
    "     akbar · retard · whoabuddy · diarrhea · seth · robocop · titan · terminator · reze · vibe · rebecca · makima · sopranos · cheers · munsters · happydays ·\n"
    "     dontwanttowait · strangerthings · adamsfamily · xmen · futurama · charliesangles ·\n"
    "     differentstroke · seinfeld · onepiece · overtaken · freebird · kanye · darkness ·\n"
    "     bike · jobs · ree · liberal · moving · harlem · wasteland · mixalot · thug · feltedtables · prayer · feliz · sleepwell · horse\n"
    "🌟 Glow:  glow (on an image) · glow <text> (a glowing neon text post)\n"
    "✨ Add motion to any effect:  zoom · shake · medshake · beginshake · pulse,\n"
    "     and/or trippy colours — e.g.  dildo zoom trippy\n"
    "🗣 /narrate <message> — reply as a short TTS video\n\n"
    "Or just talk to me and I'll reply. 💕"
)


def _sender_brand(note):
    """The fediverse poster's identity for the effect outro end-card: (handle, avatar).
    `handle` is username (bare for local, user@host for remote); `avatar` is
    (bytes, content_type) or None."""
    user = note.get("user", {}) or {}
    username = user.get("username")
    host = user.get("host")
    handle = (f"{username}@{host}" if host else username) if username else None
    avatar = None
    av_url = user.get("avatarUrl")
    if av_url:
        try:
            data = download_image_from_url(av_url, timeout=30)
            if data:
                avatar = (data, "")
        except Exception:
            avatar = None
    return handle, avatar


def _handle_media_command(note, command, arg, own_acct, visibility):
    """Run compress/clip/convert/meme on a note's attachment(s) via the backend and
    post the result file(s) back. Shared shape with the Pleroma listener."""
    brand_handle, brand_avatar = _sender_brand(note)
    media = _gather_note_media(note)
    if not media:
        # `glow <text>` with no attachment → a glowing neon text-card post. (No bad-word
        # gate — that's CSAM protection for image *generation*, not rendered text.)
        if command == "glow" and arg.strip():
            _gsum, _gout = process_media("glow", arg, [], brand_handle, brand_avatar)
            _gimgs = [(f["data"], f["content_type"]) for f in _gout if f["content_type"].startswith("image/")]
            if _gimgs:
                send_reply(note, "", own_acct=own_acct, visibility=visibility, image_bytes=_gimgs)
            else:
                send_reply(note, _gsum or "Couldn't make that glow post.",
                           own_acct=own_acct, visibility=visibility)
            return
        send_reply(note, "📎 Attach a file to your post, then add `compress`, `clip 0:10 0:30`, `convert`, `meme <text>` or `dildo`. Or `glow <text>` for a glowing text post.",
                   own_acct=own_acct, visibility=visibility)
        return
    print(f"→ Forwarding {len(media)} file(s) for '{command}'")
    summary, out_files = process_media(command, arg, media, brand_handle, brand_avatar)
    # Fediverse media is images/video only — route ALL outputs by type; flag the rest.
    image_outs = [(f["data"], f["content_type"]) for f in out_files if f["content_type"].startswith("image/")]
    video_outs = [f["data"] for f in out_files if f["content_type"].startswith("video/")]
    skipped = [f["filename"] for f in out_files
               if not f["content_type"].startswith(("image/", "video/"))]
    # meme's result IS the image — reply with just the image, no summary caption.
    text = "" if command in ("meme", "dildo", "poo", "cum", "blood", "bullethole", "fire", "nakedman", "glow", "gay", "blacked", "kosher", "blue", "barked", "hava", "indian", "yakety", "yamete", "curb", "depressing", "fahh", "helpme", "gong", "fbi", "redeem", "gigity", "beavis", "smell", "hood", "akbar", "retard", "whoabuddy", "diarrhea", "seth", "robocop", "titan", "terminator", "reze", "vibe", "rebecca", "makima", "sopranos", "cheers", "munsters", "happydays", "dontwanttowait", "strangerthings", "adamsfamily", "xmen", "futurama", "charliesangles", "differentstroke", "seinfeld", "onepiece", "overtaken", "freebird", "kanye", "darkness", "bike", "jobs", "ree", "liberal", "moving", "harlem", "chimp", "consider", "clay", "uwu", "wasteland", "mixalot", "thug", "feltedtables", "prayer", "feliz", "sleepwell", "horse", "knightrider") else (summary or "Done.")
    if skipped:
        text += f"\n\n(Couldn't attach {', '.join(skipped)} here — fediverse posts only take images/video.)"
    # Main reply carries the summary + all images + the first video; any further
    # videos follow as their own replies (one post can't cleanly hold several videos).
    send_reply(note, text, own_acct=own_acct, visibility=visibility,
               image_bytes=image_outs or None, video_bytes=(video_outs[0] if video_outs else None))
    for _v in video_outs[1:]:
        send_reply(note, "", own_acct=own_acct, visibility=visibility, video_bytes=_v)

# Unified codebase: image generation always goes through image_backend → the posterchanai
# server (native diffusers — the one image backend).
from image_backend import generate_image_bytes

# Persistent sets to track processed mentions and replied notes
_processed_mention_ids = set()
# Restart guard — how much pre-start history a freshly spawned listener will still answer.
# 2 min covers the restart gap without re-answering what the previous process handled.
try:
    _STARTUP_CATCHUP = max(0, int(os.getenv("BOT_STARTUP_CATCHUP_SECS", "120")))
except ValueError:
    _STARTUP_CATCHUP = 120
_PROCESS_START = None   # set on the first poll, when the tz is known
_replied_note_ids = set()
def _state_suffix() -> str:
    """Per-account suffix so multiple bot ACCOUNTS don't share (and clobber) one dedup file.
    Keyed by the access token (unique per account); multiple PROCESSES of the SAME account still
    share that account's file + flock. Assumes a deployment may run several Misskey bots."""
    import hashlib
    key = (MISSKEY_ACCESS_TOKEN or MISSKEY_USERNAME or "").strip()
    return hashlib.sha1(key.encode()).hexdigest()[:10] if key else "default"

_PROCESSED_IDS_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                                   f".processed_misskey_ids_{_state_suffix()}")
_MAX_TRACKED_IDS = 5000  # Limit set sizes to prevent memory growth

import fcntl
_LOCK_FILE = _PROCESSED_IDS_FILE + ".lock"

def _load_processed_ids():
    """Load processed IDs from file on startup"""
    global _processed_mention_ids, _replied_note_ids
    path = _PROCESSED_IDS_FILE
    if not os.path.exists(path):
        # One-time migration: seed from the legacy shared file (pre per-account split) so the first
        # poll after the upgrade doesn't re-reply to recent mentions.
        legacy = os.path.join(os.path.dirname(path), ".processed_misskey_ids")
        if os.path.exists(legacy):
            path = legacy
    try:
        with open(path, "r") as f:
            for line in f:
                line = line.strip()
                if line.startswith("m:"):
                    _processed_mention_ids.add(line[2:])
                elif line.startswith("n:"):
                    _replied_note_ids.add(line[2:])
    except FileNotFoundError:
        pass

def _save_processed_ids():
    """Save processed IDs to file using atomic write"""
    global _processed_mention_ids, _replied_note_ids

    # Trim sets if they exceed max size (keep most recent IDs)
    if len(_processed_mention_ids) > _MAX_TRACKED_IDS:
        _processed_mention_ids = set(sorted(_processed_mention_ids)[-_MAX_TRACKED_IDS:])
    if len(_replied_note_ids) > _MAX_TRACKED_IDS:
        _replied_note_ids = set(sorted(_replied_note_ids)[-_MAX_TRACKED_IDS:])

    # Atomic write via a UNIQUE temp file (mkstemp) → os.replace, so concurrent writers (multiple
    # processes of this account) never collide on a shared ".tmp".
    import tempfile
    temp_file = None
    try:
        fd, temp_file = tempfile.mkstemp(dir=os.path.dirname(_PROCESSED_IDS_FILE), prefix=".mids_tmp_")
        with os.fdopen(fd, "w") as f:
            for mid in _processed_mention_ids:
                f.write(f"m:{mid}\n")
            for nid in _replied_note_ids:
                f.write(f"n:{nid}\n")
        os.replace(temp_file, _PROCESSED_IDS_FILE)  # atomic on POSIX
    except Exception as e:
        print(f"[ERROR] Failed to save processed IDs: {e}", flush=True)
        if temp_file:
            try:
                os.remove(temp_file)
            except OSError:
                pass

def _try_claim_note(note_id):
    """
    Atomically try to claim a note ID for processing.
    Returns True if we claimed it, False if another process already has it.
    Uses file locking to prevent race conditions across multiple processes.
    """
    global _replied_note_ids

    try:
        with open(_LOCK_FILE, "w") as lock_f:
            # Get exclusive lock (blocks until available)
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                # Reload from file to get latest state from other processes
                _load_processed_ids()

                # Check if already processed
                if note_id in _replied_note_ids:
                    return False

                # Claim it
                _replied_note_ids.add(note_id)
                _save_processed_ids()
                return True
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        print(f"[ERROR] Failed to claim note {note_id}: {e}")
        return False

# Load on module import
_load_processed_ids()


def process_mentions():
    global _processed_mention_ids, _replied_note_ids

    # Reload processed IDs from file to get updates from other processes
    # This prevents race conditions when multiple bot instances run in parallel
    _load_processed_ids()

    own = get_own_account()
    own_acct = own.get("username") if own else None

    # Create a local copy to avoid mutating the config
    bot_blacklist = [bot for bot in BOT_BLACKLIST if bot != MISSKEY_USERNAME]

    all_mentions = get_mentions(limit=40)
    print(f"[DEBUG] Fetched {len(all_mentions)} total mentions from API", flush=True)

    # Filter to mentions from the last 10 minutes (we have persistent tracking to prevent duplicates)
    tz = pytz.timezone("UTC")
    # Restart guard: a freshly spawned listener must not re-answer mentions the previous
    # process already handled (this service restarts constantly in development).
    # No-op once the process has been up longer than its own window.
    cutoff_time = datetime.now(tz) - timedelta(minutes=10)
    global _PROCESS_START
    if _PROCESS_START is None:
        _PROCESS_START = datetime.now(tz)
    cutoff_time = max(cutoff_time, _PROCESS_START - timedelta(seconds=_STARTUP_CATCHUP))
    print(f"[DEBUG] Current time: {datetime.now(tz)}, cutoff: {cutoff_time}", flush=True)
    mentions = []
    for mention in all_mentions[:5]:  # Log first 5 for debugging
        created_at = mention.get("createdAt")
        print(f"[DEBUG] Mention createdAt: {created_at}", flush=True)
    for mention in all_mentions:
        created_at = mention.get("createdAt")
        if not created_at:
            continue
        try:
            dt = datetime.fromisoformat(created_at.replace("Z", "+00:00")).astimezone(tz)
            if dt > cutoff_time:
                mentions.append(mention)
        except Exception as e:
            print(f"[DEBUG] Failed to parse date {created_at}: {e}", flush=True)
            continue

    print(f"[DEBUG] Found {len(mentions)} recent mentions (last 10 min), {len(_processed_mention_ids)} already processed", flush=True)

    for mention in mentions:
        nid = mention.get("id")
        if not nid or nid in _processed_mention_ids:
            continue
        _processed_mention_ids.add(nid)

        # Check if we've already replied to this note
        if nid in _replied_note_ids:
            print(f"[DEBUG] Skipping mention {nid} - already replied", flush=True)
            continue

        note = get_note(nid)
        if not note:
            continue

        # Check if sender is in bot blacklist (prevents bot-to-bot loops)
        sender_user = note.get("user", {})
        sender_username = sender_user.get("username", "").lower()
        sender_host = sender_user.get("host", "")
        sender_acct = f"{sender_username}@{sender_host}" if sender_host else sender_username
        if any(bot in sender_acct for bot in bot_blacklist):
            print(f"[DEBUG] Skipping mention - sender {sender_acct} is in bot_blacklist", flush=True)
            continue

        # Skip if sender is this bot (prevents replying to own posts, e.g. daily block report)
        own_username = (own.get("username") or "").lower() if own else ""
        own_host = (own.get("host") or "") if own else ""
        own_acct_normalized = f"{own_username}@{own_host}" if own_host else own_username
        if own_acct_normalized and sender_acct == own_acct_normalized.lower():
            print(f"[DEBUG] Skipping mention - sender is self (own account)", flush=True)
            continue

        user_content = strip_html(note.get("text") or note.get("content", ""))

        # Truncate user content to prevent abuse (max 4000 chars)
        MAX_CONTENT_LENGTH = 4000
        if len(user_content) > MAX_CONTENT_LENGTH:
            print(f"[DEBUG] Truncating user content from {len(user_content)} to {MAX_CONTENT_LENGTH} chars", flush=True)
            user_content = user_content[:MAX_CONTENT_LENGTH]

        lower_content = user_content.lower()

        # Check for other bot @mentions in content (exclude this bot's own username)
        # Only skip if content has @botname or botname@ pattern (actual mentions, not just words)
        own_username = MISSKEY_USERNAME.lstrip("@").lower() if MISSKEY_USERNAME else ""
        other_bots = [b for b in bot_blacklist if b.lower() != own_username]
        if any(f"@{b}" in lower_content or f"{b}@" in lower_content for b in other_bots):
            print(f"[DEBUG] Skipping mention due to bot_blacklist @mention", flush=True)
            continue

        # Fetch full thread history for context
        thread_history = get_thread_history(nid)
        print(f"[DEBUG] Processing mention {nid}, thread history: {len(thread_history)} messages", flush=True)

        # Get visibility from the note to reply with same visibility
        visibility = note.get("visibility", "public")

        # Check if bot is the FIRST mention in the content
        # Only reply if the bot is directly addressed (first @mention)
        # Also respond to DMs (specified visibility) even without explicit @-mention
        is_dm = visibility == "specified"
        is_mentioned = False
        if MISSKEY_USERNAME:
            username_bare = MISSKEY_USERNAME.lstrip("@").lower()
            # Find the first @mention in the content
            first_mention_match = re.search(r'(^|\s)@([\w.]+(?:@[\w.]+)?)', user_content)
            if first_mention_match:
                first_mention = first_mention_match.group(2).lower()
                # Check if the first mention is this bot (with or without domain)
                is_mentioned = first_mention == username_bare or first_mention.startswith(username_bare + "@")
            if not is_mentioned and not is_dm:
                print(f"[DEBUG] Bot not first mention. Username: {MISSKEY_USERNAME}, Content: {user_content[:100]}", flush=True)

        if is_mentioned or is_dm:
            # Remove all @mentions to get the actual prompt text
            prompt_text = re.sub(r'@[\w@.]+', '', user_content).strip()
            match_type = "DM" if is_dm else "mention"
            print(f"[DEBUG] Matched {match_type}, prompt: {prompt_text[:100]}..., visibility: {visibility}", flush=True)

            if not prompt_text:
                print("[DEBUG] Empty prompt after removing mentions, skipping", flush=True)
                continue

            # Atomically try to claim this note - skip if another process got it first
            if not _try_claim_note(nid):
                print(f"[DEBUG] Note {nid} already claimed by another process, skipping")
                continue

            # Handle media commands (compress/clip/convert/meme) on an attached file.
            lower_prompt = prompt_text.lower()
            _media_cmd = None
            for _c in ("compress", "clip", "convert", "meme", "dildo", "poo", "cum", "blood", "bullethole", "fire", "nakedman", "glow", "gay", "blacked", "kosher", "blue", "barked", "hava", "indian", "yakety", "yamete", "curb", "depressing", "fahh", "helpme", "gong", "fbi", "redeem", "gigity", "beavis", "smell", "hood", "akbar", "retard", "whoabuddy", "diarrhea", "seth", "robocop", "titan", "terminator", "reze", "vibe", "rebecca", "makima", "sopranos", "cheers", "munsters", "happydays", "dontwanttowait", "strangerthings", "adamsfamily", "xmen", "futurama", "charliesangles", "differentstroke", "seinfeld", "onepiece", "overtaken", "freebird", "kanye", "darkness", "bike", "jobs", "ree", "liberal", "moving", "harlem", "chimp", "consider", "clay", "uwu", "wasteland", "mixalot", "thug", "feltedtables", "prayer", "feliz", "sleepwell", "horse", "knightrider"):
                if lower_prompt == _c or lower_prompt.startswith(_c + " "):
                    _media_cmd = _c
                    break
            if _media_cmd:
                _media_arg = prompt_text[len(_media_cmd):].strip()
                # meme bakes the user's caption into a publicly-posted image, so it
                # gets the same bad-word gate as geni (compress/clip/convert add no text).
                if _media_cmd == "meme" and contains_bad_words(_media_arg.lower()):
                    print(f"[DEBUG] BLOCKED: meme caption contains bad words")
                    send_reply(note, "I cannot add that text to an image.",
                               own_acct=own_acct, visibility=visibility)
                else:
                    _handle_media_command(note, _media_cmd, _media_arg, own_acct, visibility)

            # Handle screenshot command: the backend captures the page and returns a
            # PNG, which the bot posts back as an image attachment.
            elif lower_prompt in ("screenshot", "shot", "ss") \
                    or lower_prompt.startswith(("screenshot ", "shot ", "ss ")):
                _ss_url = prompt_text.split(None, 1)[1].strip() if len(prompt_text.split(None, 1)) > 1 else ""
                if not _ss_url:
                    send_reply(note, "Usage: screenshot <url> — e.g. screenshot example.com",
                               own_acct=own_acct, visibility=visibility)
                else:
                    print(f"→ Screenshot request: {_ss_url[:80]}")
                    png, err = capture_screenshot(_ss_url)
                    if png:
                        send_reply(note, f"📸 {_ss_url}", own_acct=own_acct, visibility=visibility, image_bytes=[png])
                    else:
                        send_reply(note, err or "❌ Screenshot failed.", own_acct=own_acct, visibility=visibility)

            # Handle ytdl command: download YouTube/X media on the backend and post
            # it back as an audio (default) or video attachment.
            elif lower_prompt == "ytdl" or lower_prompt.startswith("ytdl "):
                _yt_arg = prompt_text[4:].strip()  # after "ytdl"
                _as_video = False
                if _yt_arg.lower().startswith("video"):
                    _as_video = True
                    _yt_arg = _yt_arg[5:].strip()
                elif _yt_arg.lower().startswith("mp3"):
                    _yt_arg = _yt_arg[3:].strip()
                # Optional `clip <start> <end>` / `compress` modifiers — these only apply
                # to video, so their presence implies `video` even without the keyword.
                _yt_url, _yt_clip, _yt_compress = parse_ytdl_postaction(_yt_arg)
                if _yt_clip or _yt_compress:
                    _as_video = True
                _yt_elapsed = time.monotonic() - _ytdl_last_request.get(sender_username, 0.0)
                if not _yt_url:
                    send_reply(note, "Usage: ytdl <url> (audio), ytdl video <url>, or ytdl video <url> clip 0:10 0:30 compress",
                               own_acct=own_acct, visibility=visibility)
                elif _yt_elapsed < _YTDL_COOLDOWN_SECONDS:
                    send_reply(note, f"⏳ Please wait {int(_YTDL_COOLDOWN_SECONDS - _yt_elapsed)}s before another download.",
                               own_acct=own_acct, visibility=visibility)
                else:
                    _ytdl_last_request[sender_username] = time.monotonic()
                    print(f"→ ytdl request ({'video' if _as_video else 'audio'}): {_yt_url[:80]}")
                    _media, _mime, _err = fetch_ytdl_media(_yt_url, video=_as_video, clip=_yt_clip, compress=_yt_compress)
                    if _media and (_as_video or (_mime or '').startswith('video/')):
                        send_reply(note, f"🎬 {_yt_url}", own_acct=own_acct, visibility=visibility, video_bytes=_media)
                    elif _media:
                        send_reply(note, f"🎵 {_yt_url}", own_acct=own_acct, visibility=visibility, audio_bytes=_media)
                    else:
                        send_reply(note, f"❌ Download failed: {_err or 'unknown error'}",
                                   own_acct=own_acct, visibility=visibility)

            # Handle help command: list available commands.
            elif lower_prompt.strip() in ("help", "/help", "commands", "?"):
                send_reply(note, _BOT_HELP_TEXT, own_acct=own_acct, visibility=visibility)

            # Handle search command: search <query>
            elif lower_prompt.startswith("search ") or " search " in lower_prompt:
                # Extract query after "search"
                search_match = re.search(r'\bsearch\s+(.+)', prompt_text, re.IGNORECASE)
                if search_match:
                    query = search_match.group(1).strip()
                    if query:
                        print(f"[DEBUG] Web search for: {query}")
                        results, categories = smart_search(query)
                        if results:
                            reply_text = summarize_search_results(results, query, categories)
                            send_reply(note, reply_text, own_acct=own_acct, visibility=visibility)
                        else:
                            send_reply(note, f'No results found for "{query}".', own_acct=own_acct, visibility=visibility)
                    else:
                        send_reply(note, "Please provide a search query. Usage: search <query>", own_acct=own_acct, visibility=visibility)
                else:
                    send_reply(note, "Please provide a search query. Usage: search <query>", own_acct=own_acct, visibility=visibility)

            # Handle images command: images <query>
            elif lower_prompt.startswith("images ") or " images " in lower_prompt:
                # Extract query after "images"
                images_match = re.search(r'\bimages\s+(.+)', prompt_text, re.IGNORECASE)
                if images_match:
                    query = images_match.group(1).strip()
                    if query:
                        # Check for bad words in the query
                        if contains_bad_words(query.lower()):
                            print(f"[DEBUG] BLOCKED: Image search query contains bad words: {query}")
                            send_reply(note, "I cannot search for images with that content.", own_acct=own_acct, visibility=visibility)
                            continue
                        print(f"[DEBUG] Image search for: {query}")
                        reply_text, image_list = search_and_download_images(query, max_images=4)
                        if image_list:
                            send_reply(note, reply_text, own_acct=own_acct, visibility=visibility, image_bytes=image_list)
                        else:
                            # Fallback to text links if download failed
                            send_reply(note, reply_text, own_acct=own_acct, visibility=visibility)
                    else:
                        send_reply(note, "Please provide a search query. Usage: images <query>", own_acct=own_acct, visibility=visibility)
                else:
                    send_reply(note, "Please provide a search query. Usage: images <query>", own_acct=own_acct, visibility=visibility)

            # Handle news command: news <source>
            elif lower_prompt.startswith("news ") or " news " in lower_prompt:
                news_match = re.search(r'\bnews\s+(.+)', prompt_text, re.IGNORECASE)
                if news_match:
                    source = news_match.group(1).strip()
                    if source:
                        print(f"[DEBUG] News request for: {source}")
                        try:
                            reply_text = fetch_news_from_source(source, max_headlines=10)
                            print(f"[DEBUG] News result length: {len(reply_text)} chars")
                            print(f"[DEBUG] News result lines: {len([l for l in reply_text.split(chr(10)) if l.strip()])}")
                            print(f"[DEBUG] News result preview (first 500 chars):\n{reply_text[:500]}")
                            print(f"[DEBUG] News result has links: {'http' in reply_text or '[' in reply_text}")
                            print(f"[DEBUG] News fetched, waiting 60 seconds before posting...")
                            time.sleep(60)
                            send_reply(note, reply_text, own_acct=own_acct, visibility=visibility)
                        except Exception as e:
                            print(f"[DEBUG] News error: {e}")
                            import traceback
                            traceback.print_exc()
                            send_reply(note, f"Sorry, there was an error fetching news: {str(e)}", own_acct=own_acct, visibility=visibility)
                    else:
                        send_reply(note, "Please provide a news source. Usage: news <source> (e.g., news drudge)", own_acct=own_acct, visibility=visibility)
                else:
                    send_reply(note, "Please provide a news source. Usage: news <source> (e.g., news drudge)", own_acct=own_acct, visibility=visibility)

            elif "geni" in lower_content:
                # Use pre-compiled patterns for performance
                if contains_bad_words(lower_content):
                    print("Image generation blocked due to BAD_WORDS.")
                    send_reply(note, "I cannot generate images for that content.", own_acct=own_acct, visibility=visibility)
                else:
                    try:
                        print(f"Starting image generation for prompt: {prompt_text[:100]}...")
                        # Use retry version to ensure requests aren't missed
                        from image_backend import generate_image_bytes_with_retries
                        image_bytes = generate_image_bytes_with_retries(prompt_text, max_retries=10, retry_delay=30)
                        if image_bytes:
                            print(f"Image generation successful ({len(image_bytes)} bytes)")
                            send_reply(note, "Here is your image. Hope you like it.", own_acct=own_acct, visibility=visibility, image_bytes=image_bytes)
                        else:
                            # Just log to console, don't spam user with failure messages
                            print("ERROR: Image generation returned None after all retries")
                    except Exception as e:
                        # Just log to console, don't spam user with failure messages
                        print(f"ERROR: Image generation exception: {e}")
                        import traceback
                        traceback.print_exc()
            # Handle /narrate command - generate reply with TTS video
            elif "/narrate" in lower_prompt:
                # Check if AI is configured
                if not is_ai_configured():
                    print("[TTS] AI not configured, skipping /narrate")
                    continue
                # Remove /narrate from prompt
                narrate_prompt = re.sub(r'/narrate\s*', '', prompt_text, flags=re.IGNORECASE).strip()
                if not narrate_prompt:
                    send_reply(note, "Please provide a message to narrate. Usage: /narrate <your message>", own_acct=own_acct, visibility=visibility)
                else:
                    reply_text = generate_reply(narrate_prompt, thread_history=thread_history, ping=False, narrate_mode=True)
                    if reply_text:
                        # Get avatar URL for video
                        avatar_url = own.get("avatarUrl") if own else None
                        if avatar_url:
                            print(f"[TTS] Generating video with avatar...")
                            video_bytes = generate_narration_video(reply_text, avatar_url)
                            if video_bytes:
                                print(f"[TTS] Generated {len(video_bytes)} bytes of video")
                                # Empty text - reply is in video subtitles
                                send_reply(note, "", own_acct=own_acct, visibility=visibility, video_bytes=video_bytes)
                            else:
                                # Fallback to audio only
                                print("[TTS] Video failed, trying audio...")
                                audio_bytes = generate_speech_with_retries(reply_text)
                                send_reply(note, reply_text, own_acct=own_acct, visibility=visibility, audio_bytes=audio_bytes)
                        else:
                            # No avatar, use audio only
                            audio_bytes = generate_speech_with_retries(reply_text)
                            send_reply(note, reply_text, own_acct=own_acct, visibility=visibility, audio_bytes=audio_bytes)
            else:
                # Regular reply - with optional auto-narrate video
                reply_text = generate_reply(prompt_text, thread_history=thread_history, ping=False, narrate_mode=AUTO_NARRATE)
                if reply_text:
                    if AUTO_NARRATE:
                        avatar_url = own.get("avatarUrl") if own else None
                        if avatar_url:
                            print(f"[TTS] Auto-narrate: generating video...")
                            video_bytes = generate_narration_video(reply_text, avatar_url)
                            if video_bytes:
                                print(f"[TTS] Generated {len(video_bytes)} bytes of video")
                                # Empty text - reply is in video subtitles
                                send_reply(note, "", own_acct=own_acct, visibility=visibility, video_bytes=video_bytes)
                            else:
                                # Fallback to audio only
                                audio_bytes = generate_speech_with_retries(reply_text)
                                send_reply(note, reply_text, own_acct=own_acct, visibility=visibility, audio_bytes=audio_bytes)
                        else:
                            audio_bytes = generate_speech_with_retries(reply_text)
                            send_reply(note, reply_text, own_acct=own_acct, visibility=visibility, audio_bytes=audio_bytes)
                    else:
                        send_reply(note, reply_text, own_acct=own_acct, visibility=visibility)

    # Save processed IDs to file after each run
    _save_processed_ids()
