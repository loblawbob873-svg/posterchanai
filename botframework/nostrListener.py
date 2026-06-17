"""Nostr mention listener — replies to kind-1 notes that p-tag the bot.

Structurally mirrors misskeyListener (recent-mention window + persistent dedup +
shared command dispatch), but Nostr-shaped: a note that tags the bot's pubkey IS a
mention (no @handle parsing needed), media is referenced by URL, posts are public.
DMs (NIP-04/17) are intentionally out of scope for v1.
"""

import os
import re
import sys
import time
import fcntl
import hashlib
import tempfile
from datetime import datetime, timedelta, timezone

script_dir = os.path.dirname(os.path.abspath(__file__))
if script_dir not in sys.path:
    sys.path.insert(0, script_dir)

from config import NOSTR_NSEC, BOT_BLACKLIST
from bot_commands import MEDIA_COMMANDS, NO_CAPTION_COMMANDS, BOT_HELP_TEXT
from ai import generate_reply, is_ai_configured
import nostr as _nk
from searxng import smart_search, summarize_search_results, search_and_download_images
from news import fetch_news_from_source
from core.utils import contains_bad_words
from posterchanai_api import process_media, capture_screenshot, fetch_ytdl_media, parse_ytdl_postaction

get_mentions = _nk.get_mentions
get_note = _nk.get_note
get_own_account = _nk.get_own_account
send_reply = _nk.send_reply
get_thread_history = _nk.get_thread_history
download_image_from_url = _nk.download_image_from_url

# nostr: tokens in content (nostr:npub1…, nostr:nprofile…, nostr:note…) — stripped from prompts.
_NOSTR_TOKEN_RE = re.compile(r"nostr:[a-z0-9]+", re.IGNORECASE)
_YTDL_COOLDOWN_SECONDS = 30
_ytdl_last_request: dict = {}


def _state_suffix() -> str:
    key = (NOSTR_NSEC or "").strip()
    return hashlib.sha1(key.encode()).hexdigest()[:10] if key else "default"


_PROCESSED_IDS_FILE = os.path.join(script_dir, f".processed_nostr_ids_{_state_suffix()}")
_LOCK_FILE = _PROCESSED_IDS_FILE + ".lock"
_MAX_TRACKED_IDS = 5000
_replied_ids: set = set()


def _load_ids():
    global _replied_ids
    try:
        with open(_PROCESSED_IDS_FILE, "r") as f:
            _replied_ids = {ln.strip() for ln in f if ln.strip()}
    except FileNotFoundError:
        _replied_ids = set()


def _save_ids():
    global _replied_ids
    if len(_replied_ids) > _MAX_TRACKED_IDS:
        _replied_ids = set(sorted(_replied_ids)[-_MAX_TRACKED_IDS:])
    try:
        fd, tmp = tempfile.mkstemp(dir=script_dir, prefix=".nids_tmp_")
        with os.fdopen(fd, "w") as f:
            f.write("\n".join(_replied_ids))
        os.replace(tmp, _PROCESSED_IDS_FILE)
    except Exception as e:
        print(f"[nostr] failed to save ids: {e}", flush=True)


def _claim(note_id) -> bool:
    """Atomically claim a note id across processes (file lock), like the misskey listener."""
    try:
        with open(_LOCK_FILE, "w") as lock_f:
            fcntl.flock(lock_f.fileno(), fcntl.LOCK_EX)
            try:
                _load_ids()
                if note_id in _replied_ids:
                    return False
                _replied_ids.add(note_id)
                _save_ids()
                return True
            finally:
                fcntl.flock(lock_f.fileno(), fcntl.LOCK_UN)
    except Exception as e:
        print(f"[nostr] claim failed: {e}", flush=True)
        return False


_load_ids()


def _gather_media(note):
    """Download files linked on the note (or its nearest ancestor with media)."""
    files = list(note.get("files") or [])
    cur = note
    hops = 0
    while not files and cur.get("replyId") and hops < 5:
        cur = get_note(cur["replyId"])
        if not cur:
            break
        files = list(cur.get("files") or [])
        hops += 1
    media = []
    for f in files:
        data = download_image_from_url(f["url"], timeout=300)
        if data:
            media.append((f.get("name") or "file", data, f.get("type") or ""))
    return media


def _sender_brand(note):
    # Resolves the sender's kind-0 profile (network) — only reached when an effect runs.
    return _nk.get_brand(note)


def _handle_media_command(note, command, arg):
    brand_handle, brand_avatar = _sender_brand(note)
    media = _gather_media(note)
    if not media:
        if command == "glow" and arg.strip():
            _gsum, _gout = process_media("glow", arg, [], brand_handle, brand_avatar)
            imgs = [(f["data"], f["content_type"]) for f in _gout if f["content_type"].startswith("image/")]
            send_reply(note, "" if imgs else (_gsum or "Couldn't make that glow post."), image_bytes=imgs or None)
            return
        send_reply(note, "📎 Attach or link a file, then add `compress`, `clip 0:10 0:30`, `convert`, `meme <text>` or `glow <text>`.")
        return
    print(f"[nostr] forwarding {len(media)} file(s) for '{command}'")
    summary, out_files = process_media(command, arg, media, brand_handle, brand_avatar)
    image_outs = [(f["data"], f["content_type"]) for f in out_files if f["content_type"].startswith("image/")]
    video_outs = [f["data"] for f in out_files if f["content_type"].startswith("video/")]
    text = "" if command in NO_CAPTION_COMMANDS else (summary or "Done.")
    send_reply(note, text, image_bytes=image_outs or None, video_bytes=(video_outs[0] if video_outs else None))
    for v in video_outs[1:]:
        send_reply(note, "", video_bytes=v)


def _dispatch(note, prompt_text, own, thread_history):
    lower = prompt_text.lower()
    # Media / effect commands on an attached or linked file.
    media_cmd = next((c for c in MEDIA_COMMANDS if lower == c or lower.startswith(c + " ")), None)
    if media_cmd:
        arg = prompt_text[len(media_cmd):].strip()
        if media_cmd == "meme" and contains_bad_words(arg.lower()):
            send_reply(note, "I cannot add that text to an image.")
        else:
            _handle_media_command(note, media_cmd, arg)
        return

    if lower in ("screenshot", "shot", "ss") or lower.startswith(("screenshot ", "shot ", "ss ")):
        parts = prompt_text.split(None, 1)
        url = parts[1].strip() if len(parts) > 1 else ""
        if not url:
            send_reply(note, "Usage: screenshot <url>")
            return
        png, err = capture_screenshot(url)
        send_reply(note, f"📸 {url}" if png else (err or "❌ Screenshot failed."), image_bytes=[png] if png else None)
        return

    if lower == "ytdl" or lower.startswith("ytdl "):
        arg = prompt_text[4:].strip()
        as_video = False
        if arg.lower().startswith("video"):
            as_video, arg = True, arg[5:].strip()
        elif arg.lower().startswith("mp3"):
            arg = arg[3:].strip()
        url, clip, compress = parse_ytdl_postaction(arg)
        if clip or compress:
            as_video = True
        sender = (note.get("user") or {}).get("pubkey", "")
        elapsed = time.monotonic() - _ytdl_last_request.get(sender, 0.0)
        if not url:
            send_reply(note, "Usage: ytdl <url> (audio), ytdl video <url>, or ytdl video <url> clip 0:10 0:30 compress")
        elif elapsed < _YTDL_COOLDOWN_SECONDS:
            send_reply(note, f"⏳ Please wait {int(_YTDL_COOLDOWN_SECONDS - elapsed)}s before another download.")
        else:
            _ytdl_last_request[sender] = time.monotonic()
            data, mime, err = fetch_ytdl_media(url, video=as_video, clip=clip, compress=compress)
            if data and (as_video or (mime or "").startswith("video/")):
                send_reply(note, f"🎬 {url}", video_bytes=data)
            elif data:
                send_reply(note, f"🎵 {url}", audio_bytes=data)
            else:
                send_reply(note, f"❌ Download failed: {err or 'unknown error'}")
        return

    if lower in ("help", "/help", "commands", "?"):
        send_reply(note, BOT_HELP_TEXT)
        return

    if lower.startswith("search "):
        query = prompt_text[7:].strip()
        results, categories = smart_search(query)
        send_reply(note, summarize_search_results(results, query, categories) if results else f'No results found for "{query}".')
        return

    if lower.startswith("images "):
        query = prompt_text[7:].strip()
        if contains_bad_words(query.lower()):
            send_reply(note, "I cannot search for images with that content.")
            return
        reply_text, image_list = search_and_download_images(query, max_images=4)
        send_reply(note, reply_text, image_bytes=image_list or None)
        return

    if lower.startswith("news "):
        source = prompt_text[5:].strip()
        try:
            send_reply(note, fetch_news_from_source(source, max_headlines=10))
        except Exception as e:
            send_reply(note, f"Sorry, there was an error fetching news: {e}")
        return

    if "geni" in lower:
        if contains_bad_words(lower):
            send_reply(note, "I cannot generate images for that content.")
            return
        from image_backend import generate_image_bytes_with_retries
        image_bytes = generate_image_bytes_with_retries(prompt_text, max_retries=10, retry_delay=30)
        if image_bytes:
            send_reply(note, "Here is your image. Hope you like it.", image_bytes=image_bytes)
        else:
            print("[nostr] image generation returned None after retries")
        return

    if "/narrate" in lower:
        if not is_ai_configured():
            return
        narrate_prompt = re.sub(r"/narrate\s*", "", prompt_text, flags=re.IGNORECASE).strip()
        if not narrate_prompt:
            send_reply(note, "Usage: /narrate <your message>")
            return
        reply_text = generate_reply(narrate_prompt, thread_history=thread_history, ping=False, narrate_mode=True)
        if reply_text:
            from tts import generate_narration_video, generate_speech_with_retries
            avatar_url = own.get("avatarUrl") if own else None
            video = generate_narration_video(reply_text, avatar_url) if avatar_url else None
            if video:
                send_reply(note, "", video_bytes=video)
            else:
                send_reply(note, reply_text, audio_bytes=generate_speech_with_retries(reply_text))
        return

    # Plain reply.
    reply_text = generate_reply(prompt_text, thread_history=thread_history, ping=False)
    if reply_text:
        send_reply(note, reply_text)


def process_mentions():
    own = get_own_account()
    if not own:
        print("[nostr] no account configured (NOSTR_NSEC missing)", flush=True)
        return
    own_pubkey = own.get("pubkey")
    blacklist = [b.lower() for b in BOT_BLACKLIST]

    mentions = get_mentions(limit=40)
    print(f"[nostr] fetched {len(mentions)} p-tagged notes", flush=True)
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=10)

    for note in mentions:
        nid = note.get("id")
        if not nid:
            continue
        try:
            dt = datetime.fromisoformat((note.get("createdAt") or "").replace("Z", "+00:00"))
            if dt < cutoff:
                continue
        except Exception:
            continue
        user = note.get("user") or {}
        if user.get("pubkey") == own_pubkey:
            continue  # never reply to self
        if any(b in (user.get("username") or "").lower() for b in blacklist):
            continue
        prompt_text = _NOSTR_TOKEN_RE.sub("", note.get("text") or "").strip()
        prompt_text = re.sub(r"@[\w@.]+", "", prompt_text).strip()[:4000]
        if not prompt_text:
            continue
        if not _claim(nid):
            continue
        print(f"[nostr] processing {nid[:12]} from {user.get('username')}: {prompt_text[:80]}", flush=True)
        try:
            thread_history = get_thread_history(nid)
            _dispatch(note, prompt_text, own, thread_history)
        except Exception as e:
            print(f"[nostr] dispatch failed for {nid[:12]}: {e}", flush=True)
            import traceback
            traceback.print_exc()
