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

import requests

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
    """Send ONE notification (blocking — call via asyncio.to_thread). Returns False if the endpoint is
    permanently gone (404/410) so the caller deletes it; True otherwise (sent, or transient failure).

    Two transports behind one call, because every caller above this line — the call watcher, the DM
    watcher, the pollers — should never care which kind of device it is talking to:

    * **Web Push (VAPID)**, for browsers and the iOS/Android PWA.
    * **UnifiedPush**, for the native APK. A WebView has no push service at all, so the packaged app
      could not receive ANY of this; UnifiedPush endpoints are ordinary HTTPS URLs handed out by a
      distributor the user chose (ntfy and friends), so delivering to one is a plain POST and needs
      no Google account, no Firebase project and no proprietary SDK.

    A subscription without VAPID keys is a UnifiedPush one — that is the whole discriminator, and it
    is why the model's key columns became nullable.
    """
    if not (subscription.get("keys") or {}).get("p256dh"):
        return _send_unifiedpush(subscription.get("endpoint") or "", payload)
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


def can_reach(endpoint: str) -> bool:
    """Can THIS server resolve the push service at all?

    Exists because of a real failure that looks nothing like its cause: an ad-blocking resolver on the
    LAN (AdGuard, Pi-hole) answers fcm.googleapis.com with 0.0.0.0, so every Chrome and Android-Chrome
    push silently fails to send while the phone, the browser and the subscription are all perfectly
    fine. Without this the only symptom is "notifications don't work", pointing at the wrong machine.
    """
    import ipaddress
    import socket
    from urllib.parse import urlparse
    host = (urlparse(endpoint).hostname or "").lower()
    if not host:
        return False
    try:
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if ip.is_unspecified or ip.is_loopback:
                return False        # sinkholed
        return True
    except Exception:
        return False                # cannot resolve at all


def _send_unifiedpush(endpoint: str, payload: dict) -> bool:
    """POST a notification to a UnifiedPush endpoint. Returns False only when the endpoint is
    permanently gone, so the caller prunes it.

    The distributor hands the bytes to the app unchanged, so we send the SAME JSON the service worker
    already parses — one payload shape for every transport, and the Android receiver can reuse the
    exact title/body/type contract rather than inventing a second one.

    Deliberately no auth header and no VAPID: the endpoint URL IS the capability. That means it must
    be treated as a secret, which is exactly how the Web Push endpoint is treated already.
    """
    if not endpoint.startswith(("http://", "https://")):
        return True                      # nothing sane to do with it; don't churn the row
    try:
        r = requests.post(endpoint, json=payload, timeout=10,
                          headers={"Content-Type": "application/json",
                                   # ntfy would otherwise render our JSON as the message body.
                                   "Urgency": "high" if payload.get("type") == "call" else "normal"})
        if r.status_code in (404, 410):
            return False                 # distributor dropped it / app uninstalled → prune
        if r.status_code >= 400:
            logger.warning("[push] unifiedpush %s -> %s", endpoint.split("/")[2], r.status_code)
        return True
    except Exception as e:
        logger.warning(f"[push] unifiedpush send error: {e}")
        return True
