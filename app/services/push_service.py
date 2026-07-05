"""Web Push (VAPID) for the Nostr web-client PWA.

Sends OS-level notifications to a browser's push endpoint so mentions/zaps/replies arrive even when
the PWA is closed — the retention piece the client was missing. VAPID keys are generated once and
persisted in settings (so subscriptions stay valid across restarts); `pywebpush`/`py_vapid` are
imported lazily so the app still boots on a node where the dep isn't installed yet.
"""
import asyncio
import base64
import json
import logging

from app.services import settings_store

logger = logging.getLogger(__name__)
_vapid_lock = asyncio.Lock()

# Contact address embedded in the VAPID JWT (push services want a way to reach the app operator).
_VAPID_SUBJECT = "mailto:admin@poster.place"


def get_public_key() -> str | None:
    """The application-server key the browser needs to subscribe (base64url), or None if not set up."""
    return settings_store.get("push_vapid_public") or None


async def ensure_vapid(db) -> tuple[str, str]:
    """Return (private_pem, public_appserver_key), generating + persisting them ONCE. Persisted via
    write_through (awaited, reliable) so the keypair survives restarts — subscriptions are bound to it."""
    priv = settings_store.get("push_vapid_private")
    pub = settings_store.get("push_vapid_public")
    if priv and pub:
        return priv, pub
    async with _vapid_lock:
        # Re-check inside the lock: another concurrent first-request may have just generated it. Without
        # this (and the cache update below) EVERY call regenerated — write_through persists to the relay
        # but does NOT update the in-memory cache that get() reads, so the key was never seen again.
        priv = settings_store.get("push_vapid_private")
        pub = settings_store.get("push_vapid_public")
        if priv and pub:
            return priv, pub
        from py_vapid import Vapid01
        from cryptography.hazmat.primitives import serialization

        v = Vapid01()
        v.generate_keys()
        pem = v.private_pem()
        priv = pem.decode() if isinstance(pem, (bytes, bytearray)) else pem
        raw = v.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
        pub = base64.urlsafe_b64encode(raw).rstrip(b"=").decode()
        # Update the in-memory cache NOW (so the next get() returns this key, not a fresh one) + persist
        # durably to the relay so it survives restarts and stays consistent across nodes.
        settings_store.put_many({"push_vapid_private": priv, "push_vapid_public": pub}, write_relay=False)
        try:
            from app.services.settings_store import write_through
            await write_through(db, {"push_vapid_private": priv, "push_vapid_public": pub})
        except Exception as e:
            logger.warning(f"[push] VAPID relay persist failed: {e}")
        logger.info("[push] generated + cached a VAPID keypair")
        return priv, pub


def send(subscription: dict, payload: dict) -> bool:
    """Send ONE web push (blocking — call via asyncio.to_thread). Returns False if the endpoint is
    permanently gone (404/410) so the caller deletes it; True otherwise (sent, or transient failure)."""
    priv = settings_store.get("push_vapid_private")
    if not priv:
        return True
    try:
        from pywebpush import webpush, WebPushException
    except Exception as e:
        logger.warning(f"[push] pywebpush not installed: {e}")
        return True
    try:
        webpush(subscription_info=subscription, data=json.dumps(payload),
                vapid_private_key=priv, vapid_claims={"sub": _VAPID_SUBJECT}, timeout=10)
        return True
    except WebPushException as e:
        code = getattr(getattr(e, "response", None), "status_code", None)
        if code in (404, 410):
            return False   # subscription expired/unsubscribed → prune it
        logger.warning(f"[push] send failed ({code}): {e}")
        return True
    except Exception as e:
        logger.warning(f"[push] send error: {e}")
        return True
