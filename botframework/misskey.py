# misskey.py
import json
import requests
from io import BytesIO
from datetime import datetime, timedelta
import pytz
from config import MISSKEY_SERVER, MISSKEY_ACCESS_TOKEN, BLOCK_PHRASE
from core.utils import is_safe_url

# Only initialize if MISSKEY_SERVER is configured
misskey_server = MISSKEY_SERVER.rstrip('/') if MISSKEY_SERVER else None
misskey_token = MISSKEY_ACCESS_TOKEN
misskey_headers = {f"Content-Type": "application/json"}

# API request timeout in seconds
REQUEST_TIMEOUT = 30

def misskey_post(method, params=None):
    if not misskey_server:
        print("ERROR: MISSKEY_SERVER not configured")
        return None
    url = f"{misskey_server}/api/{method}"
    body = {"i": misskey_token}
    if params:
        body.update(params)
    try:
        r = requests.post(url, headers=misskey_headers, json=body, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            # Check for empty response before parsing JSON
            if r.text:
                try:
                    return r.json()
                except json.JSONDecodeError:
                    print(f"Misskey API POST call {method} returned invalid JSON: {r.text[:200]}")
                    return None
            return {}
        print(f"Misskey API POST call {method} failed: {r.status_code} - {r.text[:200]}")
    except requests.exceptions.Timeout:
        print(f"Misskey API POST call {method} timed out after {REQUEST_TIMEOUT}s")
    except requests.exceptions.RequestException as e:
        print(f"Misskey API POST call {method} failed: {e}")
    return None

def misskey_get(method, params=None):
    if not misskey_server:
        print("ERROR: MISSKEY_SERVER not configured")
        return None
    url = f"{misskey_server}/api/{method}"
    if params is None:
        params = {}
    params["i"] = misskey_token
    try:
        r = requests.get(url, params=params, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            # Check for empty response before parsing JSON
            if r.text:
                try:
                    return r.json()
                except json.JSONDecodeError:
                    print(f"Misskey API GET call {method} returned invalid JSON: {r.text[:200]}")
                    return None
            return {}
        print(f"Misskey API GET call {method} failed: {r.status_code} - {r.text[:200]}")
    except requests.exceptions.Timeout:
        print(f"Misskey API GET call {method} timed out after {REQUEST_TIMEOUT}s")
    except requests.exceptions.RequestException as e:
        print(f"Misskey API GET call {method} failed: {e}")
    return None

def get_own_account():
    return misskey_post("i")

def get_mentions(limit=40):
    # Validate and bound the limit parameter
    if not isinstance(limit, int) or limit < 1:
        limit = 40
    limit = min(limit, 100)  # Cap at 100 to prevent abuse
    # Use /api/notes/mentions to fetch mentions
    params = {"limit": limit}
    mentions = misskey_post("notes/mentions", params=params) or []

    # Also fetch from notifications to catch direct messages
    notif_params = {"limit": limit, "includeTypes": ["mention", "reply"]}
    notifications = misskey_post("i/notifications", notif_params) or []

    # Extract notes from notifications and add to mentions if not already present
    mention_ids = {m.get("id") for m in mentions}
    for notif in notifications:
        note = notif.get("note")
        if note and note.get("id") not in mention_ids:
            mentions.append(note)
            mention_ids.add(note.get("id"))

    return mentions

def get_note(note_id):
    return misskey_post("notes/show", {"noteId": note_id})

def get_last_20_seconds_notifications(notifications, timezone_str="UTC"):
    cutoff_time = datetime.now(pytz.timezone(timezone_str)) - timedelta(seconds=10)
    recent = []
    for notif in notifications:
        created_at = notif.get("createdAt")
        if not created_at:
            continue
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00")).astimezone(pytz.timezone(timezone_str))
        if dt > cutoff_time:
            recent.append(notif)
    return recent

def build_mention_prefix(note_obj, own_acct=None, include_author=True):
    mentions = set()
    if include_author:
        user = note_obj.get("user") or {}
        username = user.get("username")
        host = user.get("host")
        if username:
            mentions.add(f"@{username}@{host}" if host else f"@{username}")

    # Note: Misskey's mentions field is just user IDs, not user objects
    # The author mention above is what matters for notifications

    # Remove bot's own mention - own_acct is just username without @
    if own_acct:
        # Check both formats: @username and @username@host
        mentions.discard(f"@{own_acct}")
        # Also remove any mention that starts with @own_acct@
        to_remove = [m for m in mentions if m.startswith(f"@{own_acct}@")]
        for m in to_remove:
            mentions.discard(m)

    if not mentions:
        return ""
    return " ".join(mentions) + " "

def upload_media_to_misskey(image_bytes, filename="image.png", mime="image/png"):
    # Extract bytes from tuple if needed
    if isinstance(image_bytes, tuple):
        print(f"WARNING: image_bytes is tuple, extracting first element")
        image_bytes = image_bytes[0] if image_bytes else None
    
    if not isinstance(image_bytes, bytes):
        print(f"ERROR: upload_media_to_misskey received {type(image_bytes).__name__} instead of bytes")
        return None
    files = {"file": (filename, BytesIO(image_bytes), mime)}
    url = f"{misskey_server}/api/drive/files/create"
    data = {"i": misskey_token}  # Include token in form data
    try:
        r = requests.post(url, data=data, files=files, timeout=60)  # Longer timeout for uploads
        if r.status_code == 200:
            try:
                res = r.json()
                return res.get("id")
            except json.JSONDecodeError:
                print(f"Media upload returned invalid JSON: {r.text[:200]}")
                return None
        print(f"Media upload failed: {r.status_code} - {r.text[:200]}")
    except requests.exceptions.Timeout:
        print(f"Media upload timed out")
    except requests.exceptions.RequestException as e:
        print(f"Media upload failed: {e}")
    return None

def send_reply(note_obj, reply_text, own_acct=None, visibility=None, image_bytes=None, audio_bytes=None, video_bytes=None):
    # Do not send if reply_text is None or empty (unless video is attached - text is in subtitles)
    if not reply_text and not video_bytes:
        print("Reply is None or empty; not sending.")
        return

    # Prevent sending any reply that contains the BLOCK_PHRASE
    if reply_text and BLOCK_PHRASE and BLOCK_PHRASE in reply_text:
        print("Reply contains blocked phrase; not sending to Misskey.")
        return
    # Map supports both Pleroma-style and Misskey-style visibility values
    visibility_map = {
        # Pleroma/Mastodon style
        "public": "public",
        "unlisted": "home",
        "private": "followers",
        "direct": "specified",
        # Misskey native style (pass through)
        "home": "home",
        "followers": "followers",
        "specified": "specified",
    }
    v = visibility_map.get(visibility, "public")
    reply_note_id = note_obj.get("id")
    mention_prefix = build_mention_prefix(note_obj, own_acct)
    # If video attached, only send mention (text is in video subtitles)
    if video_bytes and not reply_text:
        full_text = mention_prefix.strip()
    else:
        full_text = f"{mention_prefix}{reply_text}".strip()
    print(f"[DEBUG] Mention prefix: '{mention_prefix}', own_acct: {own_acct}")
    media_ids = []
    if image_bytes:
        # Support both single image and list of images
        # Also supports (bytes, mime) tuples from search_and_download_images
        images = image_bytes if isinstance(image_bytes, list) else [image_bytes]
        for idx, img in enumerate(images):
            # Handle both plain bytes and (bytes, mime) tuples
            if isinstance(img, tuple) and len(img) == 2:
                img_data, mime_type = img
                if mime_type is None:
                    mime_type = "image/png"
                ext_map = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
                ext = ext_map.get(mime_type, "png")
                filename = f"image_{idx + 1}.{ext}"
            elif isinstance(img, bytes):
                img_data = img
                mime_type = "image/png"
                filename = f"image_{idx + 1}.png"
            else:
                print(f"Skipping invalid image data at index {idx}")
                continue
            media_id = upload_media_to_misskey(img_data, filename=filename, mime=mime_type)
            if media_id:
                media_ids.append(media_id)
        if not media_ids:
            print("Failed to upload media; sending text only.")
    # Upload video if provided (takes priority over audio)
    if video_bytes:
        video_id = upload_media_to_misskey(video_bytes, filename="narration.mp4", mime="video/mp4")
        if video_id:
            media_ids.append(video_id)
            print(f"[TTS] Video uploaded successfully: {video_id}")
        else:
            print("[TTS] Failed to upload video")
    # Upload audio if provided (only if no video)
    elif audio_bytes:
        audio_id = upload_media_to_misskey(audio_bytes, filename="voice.mp3", mime="audio/mpeg")
        if audio_id:
            media_ids.append(audio_id)
            print(f"[TTS] Audio uploaded successfully: {audio_id}")
        else:
            print("[TTS] Failed to upload audio")
    params = {
        "visibility": v,
        "text": full_text,
        "replyId": reply_note_id,
    }
    # For direct messages (specified visibility), we need to include visibleUserIds
    if v == "specified":
        user = note_obj.get("user") or {}
        user_id = user.get("id")
        if user_id:
            params["visibleUserIds"] = [user_id]
            print(f"[DEBUG] Direct message - adding visibleUserIds: {user_id}")
    if media_ids:
        params["fileIds"] = media_ids
    res = misskey_post("notes/create", params)
    if res:
        print("Replied successfully.")
    else:
        print("Failed to send reply.")

def post_image_to_fediverse(text, image_bytes=None, audio_bytes=None, video_bytes=None):
    # Prevent sending any post that contains the BLOCK_PHRASE
    if BLOCK_PHRASE and BLOCK_PHRASE in text:
        print("Post contains blocked phrase; not sending to Misskey.")
        return

    media_ids = []
    if image_bytes:
        media_id = upload_media_to_misskey(image_bytes)
        if media_id:
            media_ids.append(media_id)
        else:
            print("Failed to upload image; sending text only.")
    # Upload video if provided (takes priority over audio)
    if video_bytes:
        video_id = upload_media_to_misskey(video_bytes, filename="narration.mp4", mime="video/mp4")
        if video_id:
            media_ids.append(video_id)
            print(f"[TTS] Video uploaded successfully: {video_id}")
        else:
            print("[TTS] Failed to upload video")
    elif audio_bytes:
        audio_id = upload_media_to_misskey(audio_bytes, filename="voice.mp3", mime="audio/mpeg")
        if audio_id:
            media_ids.append(audio_id)
            print(f"[TTS] Audio uploaded successfully: {audio_id}")
        else:
            print("[TTS] Failed to upload audio")
    params = {"text": text, "visibility": "public"}
    if media_ids:
        params["fileIds"] = media_ids
    res = misskey_post("notes/create", params)
    if res:
        print("Posted successfully.")
    else:
        print("Failed to post.")

def post_to_fediverse(status_text):
    # Prevent sending any post that contains the BLOCK_PHRASE
    if BLOCK_PHRASE and BLOCK_PHRASE in status_text:
        print("Post contains blocked phrase; not sending to Misskey.")
        return

    # Misskey has a character limit (typically 3000)
    MAX_LENGTH = 3000
    if len(status_text) > MAX_LENGTH:
        print(f"Warning: Post is {len(status_text)} chars, truncating to {MAX_LENGTH}")
        status_text = status_text[:MAX_LENGTH-3] + "..."

    params = {"text": status_text, "visibility": "public"}
    res = misskey_post("notes/create", params)
    if res:
        print("Successfully posted message to Misskey.")
    else:
        print("Failed to post message to Misskey.")

def _trusted_media_hosts():
    """Hostnames the bot trusts for media downloads (its own instance plus any
    configured TRUSTED_MEDIA_HOSTS), which may resolve to a private/LAN IP on a
    self-hosted box."""
    from urllib.parse import urlparse as _urlparse
    from config import TRUSTED_MEDIA_HOSTS
    hosts = set(TRUSTED_MEDIA_HOSTS)
    if misskey_server:
        h = _urlparse(misskey_server).hostname
        if h:
            hosts.add(h.lower())
    return hosts


def download_image_from_url(url, timeout=30):
    """
    Download an image from a URL and return the raw bytes.
    Returns None if download fails.
    """
    if not is_safe_url(url, trusted_hosts=_trusted_media_hosts()):
        print(f"[SECURITY] Rejected unsafe URL: {url[:100]}")
        return None
    try:
        r = requests.get(url, timeout=timeout)
        if r.status_code == 200:
            return r.content
        print(f"Failed to download image from {url}: {r.status_code}")
    except requests.exceptions.RequestException as e:
        print(f"Failed to download image from {url}: {e}")
    return None


def get_note_images(note_obj):
    """
    Extract image URLs from a Misskey note.
    Returns list of dicts: [{"url": str, "type": str}, ...]
    """
    images = []
    files = note_obj.get("files", [])
    for f in files:
        file_type = f.get("type", "")
        if file_type.startswith("image/"):
            url = f.get("url")
            if url:
                images.append({"url": url, "type": file_type})
    return images


def get_thread_images(note_id, max_depth=10):
    """
    Search the thread for images, starting from the replied-to note.
    Returns the first image found as bytes, or None.
    """
    print(f"[get_thread_images] Looking for images in thread for note {note_id}")
    note = get_note(note_id)
    if not note:
        print(f"[get_thread_images] Failed to fetch note {note_id}")
        return None

    print(f"[get_thread_images] Note data: replyId={note.get('replyId')}, renoteId={note.get('renoteId')}, files={len(note.get('files', []))}")

    # First check the note being replied to (if this is a reply)
    reply_id = note.get("replyId")
    if reply_id:
        print(f"[get_thread_images] Checking replied-to note: {reply_id}")
        reply_note = get_note(reply_id)
        if reply_note:
            print(f"[get_thread_images] Reply note has {len(reply_note.get('files', []))} files")
            images = get_note_images(reply_note)
            print(f"[get_thread_images] Images found in reply: {images}")
            if images:
                print(f"Found image in replied-to note: {images[0]['url']}")
                return download_image_from_url(images[0]["url"])
        else:
            print(f"[get_thread_images] Failed to fetch reply note {reply_id}")
    else:
        print(f"[get_thread_images] No replyId found - this note is not a reply")

    # Then check the current note itself
    images = get_note_images(note)
    if images:
        print(f"Found image in current note: {images[0]['url']}")
        return download_image_from_url(images[0]["url"])

    # Walk up the thread to find an image
    current_id = reply_id
    depth = 0
    while current_id and depth < max_depth:
        parent_note = get_note(current_id)
        if not parent_note:
            break
        images = get_note_images(parent_note)
        if images:
            print(f"Found image in thread at depth {depth}: {images[0]['url']}")
            return download_image_from_url(images[0]["url"])
        current_id = parent_note.get("replyId")
        depth += 1

    print("No images found in thread")
    return None


def get_thread_history(note_id, max_depth=20):
    """
    Fetch the full conversation thread by walking back through replyId chain.
    Returns a list of dicts: [{"username": str, "content": str, "is_bot": bool}, ...]
    Ordered from oldest to newest (root first).
    """
    import re
    import html

    def strip_html(html_text):
        if not html_text:
            return ""
        text = re.sub(r"<[^>]+>", "", html_text)
        return html.unescape(text).strip()

    thread = []
    current_id = note_id
    depth = 0

    # Get own account to identify bot messages
    own_account = get_own_account()
    own_username = own_account.get("username") if own_account else None

    while current_id and depth < max_depth:
        note = get_note(current_id)
        if not note:
            break

        user = note.get("user") or {}
        username = user.get("username", "unknown")
        host = user.get("host")
        full_username = f"@{username}@{host}" if host else f"@{username}"

        content = strip_html(note.get("text") or note.get("content", ""))
        is_bot = (username == own_username)

        thread.append({
            "username": full_username,
            "content": content,
            "is_bot": is_bot
        })

        current_id = note.get("replyId")
        depth += 1

    # Reverse to get oldest first
    thread.reverse()
    return thread