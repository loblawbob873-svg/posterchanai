"""First-party PosterChan Direct notification transport.

Android keeps one authenticated WebSocket to its PosterChan node. Notification payloads are queued
briefly in Postgres and removed only after the device ACKs them, so a radio handoff or process restart
does not silently lose a notification. Bearer tokens are never stored: only SHA-256 digests are.
"""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta
import hashlib
import json
import logging
import threading

from fastapi import WebSocket, WebSocketDisconnect

logger = logging.getLogger(__name__)
TRANSPORT = "posterchan-direct"
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
    """Transport-neutral shape consumed by push_service.send()."""
    return {
        "id": row.id,
        "transport": getattr(row, "transport", None) or "webpush",
        "endpoint": row.endpoint,
        "keys": {"p256dh": row.p256dh, "auth": row.auth},
    }


def enqueue(subscription_id: int, payload: dict) -> bool:
    """Legacy retention contract: only a permanently missing device returns False."""
    return enqueue_result(subscription_id, payload) != "expired"


def enqueue_result(subscription_id: int, payload: dict) -> str:
    """Persist a small notification and wake a connected device. Called from worker threads."""
    from app.database import SessionLocal
    from app.models import DirectPushMessage, PushSubscription

    try:
        wire = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    except (TypeError, ValueError):
        logger.warning("[direct-push] refused a non-JSON notification")
        return "failed"
    if not wire or len(wire.encode("utf-8")) > _MAX_PAYLOAD_BYTES:
        logger.warning("[direct-push] refused notification larger than %d bytes", _MAX_PAYLOAD_BYTES)
        return "failed"

    db = SessionLocal()
    try:
        sub = db.query(PushSubscription).filter(
            PushSubscription.id == int(subscription_id),
            PushSubscription.transport == TRANSPORT,
        ).first()
        if not sub:
            return "expired"
        now = datetime.utcnow()
        db.query(DirectPushMessage).filter(DirectPushMessage.expires_at <= now).delete(
            synchronize_session=False)
        # Bound each device independently. Calls should never sit behind a hundred old social cards.
        ids = [r[0] for r in db.query(DirectPushMessage.id).filter(
            DirectPushMessage.subscription_id == sub.id
        ).order_by(DirectPushMessage.id.desc()).offset(_MAX_PENDING - 1).all()]
        if ids:
            db.query(DirectPushMessage).filter(DirectPushMessage.id.in_(ids)).delete(
                synchronize_session=False)
        ttl = 90 if payload.get("type") == "call" else 6 * 60 * 60
        db.add(DirectPushMessage(subscription_id=sub.id, payload=wire,
                                 expires_at=now + timedelta(seconds=ttl)))
        db.commit()
    except Exception as e:
        db.rollback()
        logger.warning("[direct-push] queue failed: %s", e)
        return "failed"
    finally:
        db.close()
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
    from app.database import SessionLocal
    from app.models import DirectPushMessage, PushSubscription

    db = SessionLocal()
    try:
        exists = db.query(PushSubscription.id).filter(
            PushSubscription.id == subscription_id,
            PushSubscription.transport == TRANSPORT,
        ).first()
        if not exists:
            return []
        now = datetime.utcnow()
        db.query(DirectPushMessage).filter(DirectPushMessage.expires_at <= now).delete(
            synchronize_session=False)
        rows = db.query(DirectPushMessage).filter(
            DirectPushMessage.subscription_id == subscription_id
        ).order_by(DirectPushMessage.id.asc()).limit(_MAX_PENDING).all()
        db.commit()
        out = []
        for row in rows:
            try:
                payload = json.loads(row.payload)
            except Exception:
                payload = {}
            out.append({"type": "notification", "id": row.id, "payload": payload})
        return out
    finally:
        db.close()


def _ack(subscription_id: int, message_id: int) -> None:
    from app.database import SessionLocal
    from app.models import DirectPushMessage

    db = SessionLocal()
    try:
        db.query(DirectPushMessage).filter(
            DirectPushMessage.id == int(message_id),
            DirectPushMessage.subscription_id == subscription_id,
        ).delete(synchronize_session=False)
        db.commit()
    finally:
        db.close()


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
            done, _ = await asyncio.wait((recv, signalled), timeout=20,
                                         return_when=asyncio.FIRST_COMPLETED)
            if signalled in done:
                conn.wake.clear()
            else:
                signalled.cancel()
                await asyncio.gather(signalled, return_exceptions=True)
            if not done:
                await websocket.send_json({"type": "ping"})
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
