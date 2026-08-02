from config import PLEROMA_ENDPOINT
from config import PLEROMA_ACCESS_TOKEN
from config import PLEROMA_USERNAME
from config import BLOCK_PHRASE
import requests
import json
import re
import html
import pytz
from io import BytesIO
from datetime import datetime, timedelta
from config import TIMEZONE
from core.utils import is_safe_url

# Ensure PLEROMA_ENDPOINT has a scheme
if PLEROMA_ENDPOINT and not PLEROMA_ENDPOINT.startswith(('http://', 'https://')):
    PLEROMA_ENDPOINT = f"https://{PLEROMA_ENDPOINT}"

mastodon_headers = {"Authorization": f"Bearer {PLEROMA_ACCESS_TOKEN}"}

# API request timeout in seconds
REQUEST_TIMEOUT = 30

def get_own_account():
    url = f"{PLEROMA_ENDPOINT}/api/v1/accounts/verify_credentials"
    try:
        r = requests.get(url, headers=mastodon_headers, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            try:
                return r.json()
            except json.JSONDecodeError:
                print(f"Invalid JSON in verify_credentials response: {r.text[:200]}")
                return None
        print(f"Failed to verify credentials: {r.status_code} - {r.text[:200]}")
    except requests.exceptions.Timeout:
        print(f"Verify credentials timed out after {REQUEST_TIMEOUT}s")
    except requests.exceptions.RequestException as e:
        print(f"Verify credentials failed: {e}")
    return None


def get_notifications():
    url = f"{PLEROMA_ENDPOINT}/api/v1/notifications"
    try:
        r = requests.get(url, headers=mastodon_headers, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            try:
                return r.json()
            except json.JSONDecodeError:
                print(f"Invalid JSON in notifications response: {r.text[:200]}")
                return None
        print(f"Failed to fetch notifications: {r.status_code} - {r.text[:200]}")
    except requests.exceptions.Timeout:
        print(f"Fetch notifications timed out after {REQUEST_TIMEOUT}s")
    except requests.exceptions.RequestException as e:
        print(f"Fetch notifications failed: {e}")
    return []


def get_status(status_id):
    url = f"{PLEROMA_ENDPOINT}/api/v1/statuses/{status_id}"
    try:
        r = requests.get(url, headers=mastodon_headers, timeout=REQUEST_TIMEOUT)
        if r.status_code == 200:
            try:
                return r.json()
            except json.JSONDecodeError:
                print(f"Invalid JSON in status response: {r.text[:200]}")
                return None
        print(f"Failed to fetch status {status_id}: {r.status_code} - {r.text[:200]}")
    except requests.exceptions.Timeout:
        print(f"Fetch status timed out after {REQUEST_TIMEOUT}s")
    except requests.exceptions.RequestException as e:
        print(f"Fetch status failed: {e}")
    return None


def get_last_20_seconds_notifications(notifications):
    cutoff_time = datetime.now(pytz.timezone(TIMEZONE)) - timedelta(seconds=10)
    recent = []
    for notif in notifications:
        if notif.get("type") != "mention":
            continue
        created_at = notif.get("created_at")
        if not created_at:
            continue
        try:
            dt = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%S.%fZ").replace(
                tzinfo=pytz.UTC
            )
            dt = dt.astimezone(pytz.timezone(TIMEZONE))
        except ValueError:
            try:
                dt = datetime.strptime(created_at, "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=pytz.UTC
                )
                dt = dt.astimezone(pytz.timezone(TIMEZONE))
            except ValueError:
                continue
        if dt > cutoff_time:
            recent.append(notif)
    return recent


def build_mention_prefix(status_obj, own_acct=None, include_author=True):
    mentions = set()

    if include_author:
        account = status_obj.get("account") or {}
        acct = account.get("acct")
        if acct:
            mentions.add(acct)
    for m in status_obj.get("mentions", []):
        acct = m.get("acct")
        if acct:
            mentions.add(acct)
    if own_acct and own_acct in mentions:
        mentions.discard(own_acct)
    if not mentions:
        return ""
    return " ".join("@" + acct for acct in mentions) + " "


def upload_media_to_pleroma(image_bytes, filename="image.png", mime="image/png"):
    # A (bytes, mime) tuple is the normal way callers carry a non-PNG image — the post card is
    # compressed to JPEG server-side, and the defaults above would otherwise upload it as
    # "image.png". Honour the mime and match the filename extension to it; anything else keeps
    # the old warn-and-unwrap behaviour.
    if isinstance(image_bytes, tuple):
        if len(image_bytes) == 2 and isinstance(image_bytes[0], bytes) and image_bytes[1]:
            image_bytes, mime = image_bytes[0], image_bytes[1]
            ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/webp": ".webp",
                   "image/gif": ".gif"}.get(mime)
            if ext:
                filename = filename.rsplit(".", 1)[0] + ext
        else:
            print(f"WARNING: image_bytes is tuple, extracting first element")
            image_bytes = image_bytes[0] if image_bytes else None

    if not isinstance(image_bytes, bytes):
        print(f"ERROR: upload_media_to_pleroma received {type(image_bytes).__name__} instead of bytes")
        return None
    # Try v2 then v1 endpoint for compatibility
    endpoints = [f"{PLEROMA_ENDPOINT}/api/v2/media", f"{PLEROMA_ENDPOINT}/api/v1/media"]
    files = {"file": (filename, BytesIO(image_bytes), mime)}
    for endpoint in endpoints:
        try:
            r = requests.post(
                endpoint, headers=mastodon_headers, files=files, timeout=60
            )
        except requests.exceptions.RequestException as e:
            print(f"Media upload request error to {endpoint}: {e}")
            continue
        if r.status_code in (200, 202):
            try:
                return r.json().get("id")
            except Exception as e:
                print(f"Failed to parse media upload response: {e} - {r.text[:200]}")
                return None
        else:
            print(f"Media upload failed to {endpoint}: {r.status_code} - {r.text[:200]}")
    return None


def send_reply(
    status_obj, reply_text, own_acct=None, visibility=None, image_bytes=None, audio_bytes=None, video_bytes=None
):
    # Do not send if there's nothing to post: no text AND no media (video subtitles
    # carry the text for video; an image-only reply — e.g. `meme` — is also valid).
    if not reply_text and not video_bytes and not image_bytes:
        print("Reply is None or empty; not sending to Mastodon.")
        return

    # Prevent sending any reply that contains the BLOCK_PHRASE
    if reply_text and BLOCK_PHRASE and BLOCK_PHRASE in reply_text:
        print("Reply contains blocked phrase; not sending to Mastodon.")
        return

    url = f"{PLEROMA_ENDPOINT}/api/v1/statuses"
    mention_prefix = build_mention_prefix(status_obj, own_acct)
    # Media-only reply (video subtitles carry text, or an image-only meme): post just
    # the mention prefix so the reply still threads/notifies, with the media attached.
    if (video_bytes or image_bytes) and not reply_text:
        full_status = mention_prefix.strip()
    else:
        full_status = f"{mention_prefix} {reply_text}".strip()
    print(f"[DEBUG] Pleroma mention prefix: '{mention_prefix}', own_acct: {own_acct}")
    print(f"[DEBUG] Full status text: {full_status[:200]}")
    data = {
        "status": full_status,
        "in_reply_to_id": status_obj.get("id"),
        "visibility": visibility or "public",
    }

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
            media_id = upload_media_to_pleroma(img_data, filename=filename, mime=mime_type)
            if media_id:
                media_ids.append(media_id)
        if not media_ids:
            print("Failed to upload media; will send text-only reply.")

    # Upload video if provided (takes priority over audio)
    if video_bytes:
        video_id = upload_media_to_pleroma(video_bytes, filename="narration.mp4", mime="video/mp4")
        if video_id:
            media_ids.append(video_id)
            print(f"[TTS] Video uploaded successfully: {video_id}")
        else:
            print("[TTS] Failed to upload video")
    # Upload audio if provided (only if no video)
    elif audio_bytes:
        audio_id = upload_media_to_pleroma(audio_bytes, filename="voice.mp3", mime="audio/mpeg")
        if audio_id:
            media_ids.append(audio_id)
            print(f"[TTS] Audio uploaded successfully: {audio_id}")
        else:
            print("[TTS] Failed to upload audio")

    try:
        # For multiple media_ids, Pleroma expects repeated media_ids[] params
        # Convert to list of tuples for proper encoding
        post_data = list(data.items())
        for mid in media_ids:
            post_data.append(("media_ids[]", mid))
        r = requests.post(url, headers=mastodon_headers, data=post_data, timeout=REQUEST_TIMEOUT)
        if r.status_code in (200, 202):
            print(f"Replied successfully. Media IDs: {media_ids if media_ids else 'none'}")
            # Debug: log the response to see mentions
            try:
                resp = r.json()
                mentions = resp.get("mentions", [])
                print(f"[DEBUG] Response mentions: {[m.get('acct') for m in mentions]}")
            except (json.JSONDecodeError, KeyError, TypeError):
                pass
        else:
            print(f"Failed to send reply: {r.status_code} - {r.text[:200]}")
    except requests.exceptions.Timeout:
        print(f"Send reply timed out after {REQUEST_TIMEOUT}s")
    except requests.exceptions.RequestException as e:
        print(f"Send reply failed: {e}")

def post_image_to_fediverse(text, image_bytes=None, audio_bytes=None, video_bytes=None):
    text = text or ""  # caption may be unset (image-only post) — avoid None in checks below
    # Prevent sending any post that contains the BLOCK_PHRASE
    if BLOCK_PHRASE and BLOCK_PHRASE in text:
        print("Post contains blocked phrase; not sending to Pleroma.")
        return

    post_url = f"{PLEROMA_ENDPOINT.rstrip('/')}/api/v1/statuses"
    media_ids = []

    if image_bytes:
        media_id = upload_media_to_pleroma(image_bytes)
        if media_id:
            media_ids.append(media_id)
        else:
            print("Failed to upload image; will send text-only reply.")

    # Upload video if provided (takes priority over audio)
    if video_bytes:
        video_id = upload_media_to_pleroma(video_bytes, filename="narration.mp4", mime="video/mp4")
        if video_id:
            media_ids.append(video_id)
            print(f"[TTS] Video uploaded successfully: {video_id}")
        else:
            print("[TTS] Failed to upload video")
    elif audio_bytes:
        audio_id = upload_media_to_pleroma(audio_bytes, filename="voice.mp3", mime="audio/mpeg")
        if audio_id:
            media_ids.append(audio_id)
            print(f"[TTS] Audio uploaded successfully: {audio_id}")
        else:
            print("[TTS] Failed to upload audio")

    try:
        # Build post data with multiple media_ids if needed
        post_data = [("status", text), ("visibility", "public"), ("content_type", "text/markdown")]
        for mid in media_ids:
            post_data.append(("media_ids[]", mid))
        r = requests.post(post_url, headers=mastodon_headers, data=post_data, timeout=REQUEST_TIMEOUT)
        if r.status_code in (200, 202):
            print("Posted successfully.")
        else:
            print(f"Failed to post: {r.status_code} - {r.text[:200]}")
    except requests.exceptions.Timeout:
        print(f"Post timed out after {REQUEST_TIMEOUT}s")
    except requests.exceptions.RequestException as e:
        print(f"Post failed: {e}")

        
def post_to_fediverse(status_text):
    # Prevent sending any post that contains the BLOCK_PHRASE
    if BLOCK_PHRASE and BLOCK_PHRASE in status_text:
        print("Post contains blocked phrase; not sending to Pleroma.")
        return

    post_url = f"{PLEROMA_ENDPOINT.rstrip('/')}/api/v1/statuses"
    data = {"status": status_text, "visibility": "public", "content_type": "text/markdown"}
    try:
        response = requests.post(
            post_url, headers=mastodon_headers, data=data, timeout=REQUEST_TIMEOUT
        )
        if response.status_code in (200, 202):
            print("Successfully posted message to Mastodon.")
        else:
            print(f"Failed to post message to Mastodon: {response.status_code} {response.text[:200]}")
    except requests.exceptions.Timeout:
        print(f"Post to fediverse timed out after {REQUEST_TIMEOUT}s")
    except requests.exceptions.RequestException as e:
        print(f"Error posting to Mastodon: {e}")
        
def direct_message_to_pleroma(status_text):
    # Prevent sending any message that contains the BLOCK_PHRASE
    if BLOCK_PHRASE and BLOCK_PHRASE in status_text:
        print("Message contains blocked phrase; not sending to Pleroma.")
        return

    post_url = f"{PLEROMA_ENDPOINT.rstrip('/')}/api/v1/statuses"
    data = {"status": status_text, "visibility": "direct", "content_type": "text/markdown"}
    try:
        response = requests.post(
            post_url, headers=mastodon_headers, data=data, timeout=REQUEST_TIMEOUT
        )
        if response.status_code in (200, 202):
            print("Successfully posted direct message to Pleroma.")
        else:
            print(f"Failed to post direct message to Pleroma: {response.status_code} {response.text[:200]}")
    except requests.exceptions.Timeout:
        print(f"Direct message timed out after {REQUEST_TIMEOUT}s")
    except requests.exceptions.RequestException as e:
        print(f"Error posting direct message: {e}")

def _trusted_media_hosts():
    """Hostnames the bot trusts for media downloads (its own instance plus any
    configured TRUSTED_MEDIA_HOSTS), which may resolve to a private/LAN IP on a
    self-hosted box."""
    from urllib.parse import urlparse as _urlparse
    from config import TRUSTED_MEDIA_HOSTS
    hosts = set(TRUSTED_MEDIA_HOSTS)
    if PLEROMA_ENDPOINT:
        h = _urlparse(PLEROMA_ENDPOINT).hostname
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
    # A browser-like User-Agent: some fediverse media CDNs reset the connection for
    # the default python-requests UA (symptom: "Stream … was reset by remote peer"),
    # which broke downloading a remote post's image for `meme`/compress on a reply.
    headers = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                             "(KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
    try:
        r = requests.get(url, timeout=timeout, headers=headers)
        if r.status_code == 200:
            return r.content
        print(f"Failed to download image from {url}: {r.status_code}")
    except requests.exceptions.RequestException as e:
        print(f"Failed to download image from {url}: {e}")
    return None


def get_status_images(status_obj):
    """
    Extract image URLs from a Pleroma/Mastodon status.
    Returns list of dicts: [{"url": str, "type": str}, ...]
    """
    images = []
    attachments = status_obj.get("media_attachments", [])
    for att in attachments:
        att_type = att.get("type", "")
        if att_type == "image":
            url = att.get("url")
            if url:
                images.append({"url": url, "type": "image"})
    return images


def get_thread_images(status_id, max_depth=10):
    """
    Search the thread for images, starting from the replied-to status.
    Returns the first image found as bytes, or None.
    """
    status = get_status(status_id)
    if not status:
        return None

    # First check the status being replied to (if this is a reply)
    reply_id = status.get("in_reply_to_id")
    if reply_id:
        reply_status = get_status(reply_id)
        if reply_status:
            images = get_status_images(reply_status)
            if images:
                print(f"Found image in replied-to status: {images[0]['url']}")
                return download_image_from_url(images[0]["url"])

    # Then check the current status itself
    images = get_status_images(status)
    if images:
        print(f"Found image in current status: {images[0]['url']}")
        return download_image_from_url(images[0]["url"])

    # Walk up the thread to find an image
    current_id = reply_id
    depth = 0
    while current_id and depth < max_depth:
        parent_status = get_status(current_id)
        if not parent_status:
            break
        images = get_status_images(parent_status)
        if images:
            print(f"Found image in thread at depth {depth}: {images[0]['url']}")
            return download_image_from_url(images[0]["url"])
        current_id = parent_status.get("in_reply_to_id")
        depth += 1

    print("No images found in thread")
    return None


def get_thread_history(status_id, max_depth=20):
    """
    Fetch the full conversation thread by walking back through in_reply_to_id chain.
    Returns a list of dicts: [{"username": str, "content": str, "is_bot": bool}, ...]
    Ordered from oldest to newest (root first).
    """
    def strip_html(html_text):
        if not html_text:
            return ""
        text = re.sub(r"<[^>]+>", "", html_text)
        return html.unescape(text).strip()

    thread = []
    current_id = status_id
    depth = 0

    # Get own account to identify bot messages
    own_account = get_own_account()
    own_acct = own_account.get("acct") if own_account else None

    while current_id and depth < max_depth:
        status = get_status(current_id)
        if not status:
            break

        account = status.get("account") or {}
        acct = account.get("acct", "unknown")

        content = strip_html(status.get("content", ""))
        is_bot = (acct == own_acct)

        thread.append({
            "username": f"@{acct}",
            "content": content,
            "is_bot": is_bot
        })

        current_id = status.get("in_reply_to_id")
        depth += 1

    # Reverse to get oldest first
    thread.reverse()
    return thread