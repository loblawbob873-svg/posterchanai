"""Pleroma parity shim — DRAFT for the incremental dedup (Phase 4).

Exposes the same public surface the listeners import from ``pleroma`` (get_own_account,
get_notifications, get_status, send_reply, post_image_to_fediverse, get_thread_history,
get_thread_images, …), but routes every NETWORK call through the app's shared
``app.services.pleroma_service`` instead of this package's own ``requests`` client. Pure,
non-network helpers (mention-prefix building, the 10s notification window, image extraction,
HTML stripping, safe download) are reused verbatim from ``pleroma`` so behavior can't drift.

**Opt-in and reversible.** ``pleromaListener`` only imports this when
``PLEROMA_USE_APP_SERVICE`` is truthy; default is the original ``pleroma``. Validate with
``tests/test_pleroma_parity.py`` (A/B the constructed HTTP), then, once confirmed in prod,
delete the duplicated network code from ``pleroma.py`` and make this the only path.
"""

import os
import sys
import asyncio

# Defensive: ensure the repo root is importable even when run outside the manager (e.g. the
# parity test). The manager already puts it on PYTHONPATH for spawned bots.
_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)

from app.services import pleroma_service as _svc  # noqa: E402

from config import PLEROMA_ENDPOINT as _RAW_ENDPOINT, PLEROMA_ACCESS_TOKEN, BLOCK_PHRASE  # noqa: E402
# Reuse the pure / non-Pleroma-API helpers unchanged — no need to re-implement or risk drift.
from pleroma import (  # noqa: E402
    get_last_20_seconds_notifications,   # pure: filters a notifications list by time window
    build_mention_prefix,                # pure: formats the @mention prefix
    get_status_images,                   # pure: pulls image urls out of a status object
    download_image_from_url,             # generic HTTP GET + SSRF guard (not a Pleroma API call)
)

# Match pleroma.py's endpoint normalization (add scheme if missing).
ENDPOINT = _RAW_ENDPOINT
if ENDPOINT and not ENDPOINT.startswith(("http://", "https://")):
    ENDPOINT = f"https://{ENDPOINT}"
TOKEN = PLEROMA_ACCESS_TOKEN


def _run(coro):
    """Drive an async pleroma_service call from this synchronous bot runtime."""
    return asyncio.run(coro)


# ---- network ops delegated to app.services.pleroma_service -------------------

def get_own_account():
    try:
        return _run(_svc.verify_credentials(ENDPOINT, TOKEN))
    except Exception as e:
        print(f"[shim] verify_credentials failed: {e}")
        return None


def get_notifications():
    try:
        return _run(_svc.fetch_notifications(ENDPOINT, TOKEN, limit=20))
    except Exception as e:
        print(f"[shim] fetch_notifications failed: {e}")
        return []


def get_status(status_id):
    try:
        return _run(_svc.fetch_status(ENDPOINT, TOKEN, status_id))
    except Exception as e:
        print(f"[shim] fetch_status({status_id}) failed: {e}")
        return None


def _normalize_media(image_bytes=None, audio_bytes=None, video_bytes=None):
    """Build the (bytes, mime) media list the same way pleroma.send_reply does: a single
    image, a list of images, or (bytes, mime) tuples; then video (priority) or audio."""
    media = []
    if image_bytes:
        images = image_bytes if isinstance(image_bytes, list) else [image_bytes]
        for img in images:
            if isinstance(img, tuple) and len(img) == 2:
                data, mime = img
                media.append((data, mime or "image/png"))
            elif isinstance(img, bytes):
                media.append((img, "image/png"))
    if video_bytes:
        media.append((video_bytes, "video/mp4"))
    elif audio_bytes:
        media.append((audio_bytes, "audio/mpeg"))
    return media


def send_reply(status_obj, reply_text, own_acct=None, visibility=None,
               image_bytes=None, audio_bytes=None, video_bytes=None):
    # Identical guards to pleroma.send_reply.
    if not reply_text and not video_bytes:
        print("Reply is None or empty; not sending to Mastodon.")
        return
    if reply_text and BLOCK_PHRASE and BLOCK_PHRASE in reply_text:
        print("Reply contains blocked phrase; not sending to Mastodon.")
        return

    mention_prefix = build_mention_prefix(status_obj, own_acct)
    if video_bytes and not reply_text:
        full_status = mention_prefix.strip()
    else:
        full_status = f"{mention_prefix} {reply_text}".strip()

    media = _normalize_media(image_bytes, audio_bytes, video_bytes)
    try:
        # Replies carry no content_type (instance default), matching pleroma.send_reply.
        _run(_svc.post_status(
            ENDPOINT, TOKEN, full_status,
            visibility=visibility or "public",
            in_reply_to_id=status_obj.get("id"),
            media=media or None,
        ))
        print("Replied successfully (via app service).")
    except Exception as e:
        print(f"[shim] send_reply failed: {e}")


def post_image_to_fediverse(text, image_bytes=None, audio_bytes=None, video_bytes=None):
    if BLOCK_PHRASE and BLOCK_PHRASE in text:
        print("Post contains blocked phrase; not sending to Pleroma.")
        return
    media = _normalize_media(image_bytes, audio_bytes, video_bytes)
    try:
        # Top-level posts use text/markdown, matching pleroma.post_image_to_fediverse.
        _run(_svc.post_status(
            ENDPOINT, TOKEN, text,
            visibility="public",
            media=media or None,
            content_type="text/markdown",
        ))
        print("Posted successfully (via app service).")
    except Exception as e:
        print(f"[shim] post_image_to_fediverse failed: {e}")


# ---- thread walkers: same algorithm as pleroma.py, on shim's delegated get_status --------

def get_thread_history(status_id, max_depth=20):
    import re, html

    def strip_html(html_text):
        if not html_text:
            return ""
        return html.unescape(re.sub(r"<[^>]+>", "", html_text)).strip()

    thread = []
    current_id = status_id
    depth = 0
    own_account = get_own_account()
    own_acct = own_account.get("acct") if own_account else None

    while current_id and depth < max_depth:
        status = get_status(current_id)
        if not status:
            break
        account = status.get("account") or {}
        acct = account.get("acct", "unknown")
        thread.append({
            "username": f"@{acct}",
            "content": strip_html(status.get("content", "")),
            "is_bot": (acct == own_acct),
        })
        current_id = status.get("in_reply_to_id")
        depth += 1

    thread.reverse()
    return thread


def get_thread_images(status_id, max_depth=10):
    status = get_status(status_id)
    if not status:
        return None

    reply_id = status.get("in_reply_to_id")
    if reply_id:
        reply_status = get_status(reply_id)
        if reply_status:
            images = get_status_images(reply_status)
            if images:
                return download_image_from_url(images[0]["url"])

    images = get_status_images(status)
    if images:
        return download_image_from_url(images[0]["url"])

    current_id = reply_id
    depth = 0
    while current_id and depth < max_depth:
        parent = get_status(current_id)
        if not parent:
            break
        images = get_status_images(parent)
        if images:
            return download_image_from_url(images[0]["url"])
        current_id = parent.get("in_reply_to_id")
        depth += 1
    return None
