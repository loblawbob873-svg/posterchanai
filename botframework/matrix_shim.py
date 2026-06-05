"""Matrix parity shim — DRAFT for the incremental dedup (Phase 4).

Like the Misskey shim, this swaps only the transport. matrix_client.py layers all 17 of its
public functions on two primitives — ``matrix_request`` and ``upload_media_to_matrix`` — so
this shim points those at the shared ``app.services.matrix_service`` and re-exports the
original functions unchanged. Behavior (sync handling, reply/threading, polls, media) is
reused verbatim — parity by construction; only the HTTP layer is deduplicated.

**Opt-in and reversible**: ``matrixListener`` imports this only when
``MATRIX_USE_APP_SERVICE`` is truthy. Validate with ``test_matrix_parity.py``.
"""

import os
import sys
import asyncio

_REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _REPO_ROOT not in sys.path:
    sys.path.append(_REPO_ROOT)

from app.services import matrix_service as _svc  # noqa: E402
import matrix_client as _mx  # noqa: E402

SERVER = _mx.matrix_server          # already rstrip('/')-normalized
TOKEN = _mx.matrix_token
VERIFY = _mx.matrix_verify_ssl


def _run(coro):
    return asyncio.run(coro)


def _request(method, endpoint, data=None, params=None):
    """Drop-in for matrix_client.matrix_request, routed through matrix_service.request."""
    if not SERVER:
        print("ERROR: MATRIX_SERVER not configured")
        return None
    try:
        return _run(_svc.request(SERVER, TOKEN, method, endpoint, data=data, params=params,
                                 verify_ssl=VERIFY))
    except Exception as e:
        print(f"[shim] Matrix API {method} {endpoint} failed: {e}")
        return None


def _upload(image_bytes, filename="image.png", mime="image/png"):
    """Drop-in for matrix_client.upload_media_to_matrix → matrix_service.upload_media_bytes.
    Returns the mxc:// content URI (or None on failure)."""
    if isinstance(image_bytes, tuple):
        image_bytes = image_bytes[0] if image_bytes else None
    if not isinstance(image_bytes, bytes):
        print(f"ERROR: upload received {type(image_bytes).__name__} instead of bytes")
        return None
    try:
        return _run(_svc.upload_media_bytes(SERVER, TOKEN, image_bytes, mime, filename))
    except Exception as e:
        print(f"[shim] Matrix media upload failed: {e}")
        return None


# Swap the transport; the unchanged higher-level functions then route through the service.
_mx.matrix_request = _request
_mx.upload_media_to_matrix = _upload

# Re-export the exact surface matrixListener imports.
get_messages = _mx.get_messages
get_sync_token = _mx.get_sync_token
get_own_account = _mx.get_own_account
send_message = _mx.send_message
send_reply = _mx.send_reply
post_image_to_matrix = _mx.post_image_to_matrix
get_thread_history = _mx.get_thread_history
get_thread_images = _mx.get_thread_images
get_room_member_count = _mx.get_room_member_count
get_event = _mx.get_event
join_room = _mx.join_room
leave_room = _mx.leave_room
send_poll = _mx.send_poll
mxc_to_https = _mx.mxc_to_https
download_image_from_url = _mx.download_image_from_url
send_file_to_room = _mx.send_file_to_room
