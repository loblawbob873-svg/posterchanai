"""First-party PosterChan Direct notification transport.

Android keeps one authenticated WebSocket to its PosterChan node. Notification payloads are queued
briefly (push_store's `direct_push_msgs` documents on this node's relay) and removed only after the device
ACKs them, so a radio handoff or process restart
does not silently lose a notification. Bearer tokens are never stored: only SHA-256 digests are.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import hashlib
import json
import logging
import threading

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)
TRANSPORT = "posterchan-direct"
# EVERY FRAME ON THIS SOCKET WAKES A PHONE'S RADIO, and the socket exists to sit idle in a pocket.
# It used to carry three unaligned keepalives — an app ping from here after 20s of quiet, uvicorn's
# protocol ping every 20s and the APK's OkHttp ping every 30s — so a phone doing nothing was woken
# several times a minute, all day ("bad battery drain"). Now there are two, each every 75s (inside
# Cloudflare's 100s idle window): the APK's (which is how it notices a dead link) and uvicorn's
# (run.py; how this end notices one). This loop sends NOTHING on its own. It still wakes every
# POLL_S, because a notification queued by ANOTHER process (the worker) cannot set this process's
# event — that read is an in-memory table (kept current from the relay) and costs the phone nothing.
POLL_S = 20
_MAX_PENDING = 100
_MAX_PAYLOAD_BYTES = 16 * 1024


@dataclass
class _Live:
    loop: asyncio.AbstractEventLoop
    wake: asyncio.Event
    socket: WebSocket


_live: dict[int, _Live] = {}
_live_lock = threading.Lock()


def token_digest(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def subscription_dict(row) -> dict:
    """Transport-neutral shape consumed by push_service.send().

    `prefs` RIDES ALONG, and it is not decoration. Every caller that filters — the poller, the
    channel poller — holds the ORM row and asks `push_prefs.allows_row(row, ...)`. The two handlers
    that go through `_subs_for` (calls and DMs) hold only this dict, and a dict has no `prefs`
    attribute, so `allows_row` read None off it and answered "send" for every device no matter what
    that device had said. A call is meant to ring regardless; a DM was not, and every phone that
    switched DMs off went on buzzing for them with the row in the database saying otherwise.
    Carrying the column here is what lets the filter be asked at all. pywebpush reads only
    `endpoint` and `keys`, so the extra key costs nothing on the Web Push path.

    A row is a push_store dict (the table left Postgres, #161); an object with the same attributes is
    still accepted.
    """
    get = row.get if isinstance(row, dict) else (lambda k, d=None: getattr(row, k, d))
    return {
        "id": get("id"),
        "transport": get("transport") or "webpush",
        "endpoint": get("endpoint"),
        "keys": {"p256dh": get("p256dh"), "auth": get("auth")},
        "prefs": get("prefs"),
    }


def enqueue(subscription_id: int, payload: dict) -> bool:
    """Legacy retention contract: only a permanently missing device returns False."""
    return enqueue_result(subscription_id, payload) != "expired"


def enqueue_result(subscription_id: int, payload: dict) -> str:
    """Persist a small notification and wake a connected device. Called from worker threads.

    "Could not ask" (the relay) is "failed", NEVER "expired": only "expired" makes a caller delete the device."""
    from app.services import push_store

    try:
        wire = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        logger.warning("[direct-push] refused a non-JSON notification")
        return "failed"
    if not wire or len(wire.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        logger.warning("[direct-push] refused notification larger than %d bytes", _MAX_PAYLOAD_BYTES)
        return "failed"

    try:
        sub = push_store.get_sub_sync(subscription_id)
        if not sub or sub.get("transport") != TRANSPORT:
            return "expired"
        push_store.drop_expired()
        # Bound each device independently. Calls should never sit behind a hundred old social cards.
        ttl = 90 if payload.get("type") == "call" else 6 * 60 * 60
        push_store.enqueue_message(sub["id"], wire, ttl, _MAX_PENDING)
    except Exception as e:
        logger.warning("[direct-push] queue failed: %s", e)
        return "failed"
    wake(subscription_id)
    return "queued"


def wake(subscription_id: int) -> None:
    with _live_lock:
        conn = _live.get(int(subscription_id))
    if conn:
        conn.loop.call_soon_threadsafe(conn.wake.set)


def disconnect(subscription_id: int) -> None:
    """End an active socket after unregister/token rotation."""
    with _live_lock:
        conn = _live.get(int(subscription_id))
    if conn:
        asyncio.run_coroutine_threadsafe(conn.socket.close(code=4001), conn.loop)


def _pending(subscription_id: int) -> list[dict]:
    """The device's queue, oldest first. "Could not ask" is [] for THIS pass only -- nothing is deleted, and
    the loop asks again within POLL_S."""
    from app.services import push_store
    from app.services.relay_reader import Unavailable

    try:
        sub = push_store.get_sub_sync(subscription_id)
        if not sub or sub.get("transport") != TRANSPORT:
            return []
        push_store.drop_expired()
        rows = push_store.queue_for(subscription_id)[:_MAX_PENDING]
    except Unavailable as e:
        logger.info("[direct-push] queue unreadable this pass: %s", e)
        return []
    out = []
    for mid, row in rows:
        try:
            payload = json.loads(row.get("payload") or "")
        except Exception:
            payload = {}
        out.append({"type": "notification", "id": mid, "payload": payload})
    return out


def _ack(subscription_id: int, message_id: int) -> None:
    from app.services import push_store
    from app.services.relay_reader import Unavailable

    try:
        push_store.ack_message(subscription_id, message_id)
    except Unavailable as e:
        # The card stays queued and is replayed; the phone recognises the id and does not draw it twice.
        logger.info("[direct-push] ack not recorded: %s", e)


async def serve(websocket: WebSocket, subscription_id: int) -> None:
    """Deliver/ACK loop for an already authenticated direct device."""
    loop = asyncio.get_running_loop()
    conn = _Live(loop=loop, wake=asyncio.Event(), socket=websocket)
    with _live_lock:
        previous = _live.get(subscription_id)
        _live[subscription_id] = conn
    if previous:
        asyncio.run_coroutine_threadsafe(previous.socket.close(code=4002), previous.loop)
    # Keep one receive pending across queue wakes. Cancelling receive on each new notification
    # could discard an ACK arriving in the same turn, leaving delivered cards in the durable queue.
    recv = asyncio.create_task(websocket.receive_json())
    signalled = None
    try:
        while True:
            for frame in await asyncio.to_thread(_pending, subscription_id):
                await websocket.send_json(frame)

            signalled = asyncio.create_task(conn.wake.wait())
            done, _ = await asyncio.wait((recv, signalled), timeout=POLL_S,
                                         return_when=asyncio.FIRST_COMPLETED)
            if signalled in done:
                conn.wake.clear()
            else:
                signalled.cancel()
                await asyncio.gather(signalled, return_exceptions=True)
            if recv not in done:
                continue
            msg = recv.result()
            recv = asyncio.create_task(websocket.receive_json())
            if not isinstance(msg, dict):
                continue
            if msg.get("type") == "ack" and isinstance(msg.get("id"), int):
                await asyncio.to_thread(_ack, subscription_id, msg["id"])
            elif msg.get("type") == "ping":
                await websocket.send_json({"type": "pong"})
    except (WebSocketDisconnect, asyncio.CancelledError):
        pass
    finally:
        tasks = [task for task in (recv, signalled) if task is not None]
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        with _live_lock:
            if _live.get(subscription_id) is conn:
                _live.pop(subscription_id, None)
