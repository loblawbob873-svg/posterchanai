"""Misskey parity shim — DRAFT for the incremental dedup (Phase 4).

Misskey's bot client (``misskey.py``) is uniformly built on two transport primitives —
``misskey_post`` and ``upload_media_to_misskey`` — that every higher-level function
(get_mentions, send_reply, get_thread_history, …) is layered on. So instead of
re-implementing those functions (as the Pleroma shim does), this shim just **swaps the
transport**: it points ``misskey.misskey_post`` / ``upload_media_to_misskey`` at the shared
``app.services.misskey_service`` and re-exports the original functions unchanged. The bot's
logic is reused verbatim — parity is by construction; only the HTTP layer is deduplicated.

**Opt-in and reversible**, exactly like pleroma_shim: ``misskeyListener`` imports this only
when ``MISSKEY_USE_APP_SERVICE`` is truthy. Validate with ``test_misskey_parity.py``.
"""

import os
import sys
import asyncio

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)

from app.services import misskey_service as _svc  # noqa: E402
import misskey as _mk  # noqa: E402

SERVER = _mk.misskey_server          # already rstrip('/')-normalized by misskey.py
TOKEN = _mk.misskey_token


def _run(coro):
    return asyncio.run(coro)


def _post(method, params=None):
    """Drop-in for misskey.misskey_post, routed through app.services.misskey_service.call."""
    if not SERVER:
        print("ERROR: MISSKEY_SERVER not configured")
        return None
    try:
        return _run(_svc.call(SERVER, TOKEN, method, params))
    except Exception as e:
        print(f"[shim] Misskey API call {method} failed: {e}")
        return None


def _upload(image_bytes, filename="image.png", mime="image/png"):
    """Drop-in for misskey.upload_media_to_misskey, routed through misskey_service.upload_file."""
    if isinstance(image_bytes, tuple):
        image_bytes = image_bytes[0] if image_bytes else None
    if not isinstance(image_bytes, bytes):
        print(f"ERROR: upload received {type(image_bytes).__name__} instead of bytes")
        return None
    try:
        return _run(_svc.upload_file(SERVER, TOKEN, image_bytes, mime))
    except Exception as e:
        print(f"[shim] Misskey media upload failed: {e}")
        return None


# Swap the transport, then the unchanged higher-level functions all route through the service.
_mk.misskey_post = _post
_mk.upload_media_to_misskey = _upload

# Re-export the exact surface misskeyListener imports.
get_mentions = _mk.get_mentions
get_note = _mk.get_note
get_own_account = _mk.get_own_account
send_reply = _mk.send_reply
post_image_to_fediverse = _mk.post_image_to_fediverse
get_thread_history = _mk.get_thread_history
get_thread_images = _mk.get_thread_images
download_image_from_url = _mk.download_image_from_url
get_last_20_seconds_notifications = _mk.get_last_20_seconds_notifications
build_mention_prefix = _mk.build_mention_prefix
