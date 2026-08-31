"""Security and reconnect behavior for the first-party PosterChan Direct server transport."""
import asyncio
import base64
from datetime import datetime, timedelta
import json

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import DirectPushMessage, PushSubscription
from app.routers import push as push_router
from app.services import direct_push_service as direct
from app.services.nostr import event as nostr_event


@pytest.fixture()
def direct_db(monkeypatch):
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    PushSubscription.__table__.create(engine)
    DirectPushMessage.__table__.create(engine)
    sessions = sessionmaker(bind=engine)
    monkeypatch.setattr("app.database.SessionLocal", sessions)
    return sessions


class _Request:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


def _direct_row(db, device="12345678-1234-1234-1234-123456789abc", digest="a" * 64):
    row = PushSubscription(pubkey="b" * 64, endpoint=f"direct:{'b' * 64}:{device}",
                           transport=direct.TRANSPORT, device_id=device, token_hash=digest)
    db.add(row)
    db.commit()
    return row


def test_registration_proof_is_bound_to_action_and_device():
    secret = bytes.fromhex("01" * 32)
    device = "12345678-1234-1234-1234-123456789abc"
    ev = nostr_event.build_event(secret, 27235, f"posterchan-direct:register:{device}")
    auth = base64.b64encode(json.dumps(ev).encode()).decode()
    assert push_router._direct_auth(auth, ev["pubkey"], "register", device)
    assert not push_router._direct_auth(auth, ev["pubkey"], "unregister", device)
    assert not push_router._direct_auth(auth, ev["pubkey"], "register", "other-device-0000")


def test_register_returns_token_but_stores_only_digest(direct_db, monkeypatch):
    monkeypatch.setattr(push_router, "_direct_auth", lambda *a: True)
    monkeypatch.setattr(direct, "disconnect", lambda *a: None)
    db = direct_db()
    device = "12345678-1234-1234-1234-123456789abc"
    result = asyncio.run(push_router.register_direct(
        _Request({"pubkey": "b" * 64, "device_id": device, "auth": "signed"}), db))
    row = db.query(PushSubscription).one()
    assert result["ok"] and result["device_id"] == device
    assert result["websocket_url"] == "/api/push/direct/ws"
    assert len(result["token"]) >= 32
    assert row.token_hash == direct.token_digest(result["token"])
    assert result["token"] not in row.endpoint
    assert result["token"] != row.token_hash
    assert row.transport == direct.TRANSPORT


def test_direct_queue_survives_reconnect_until_ack(direct_db, monkeypatch):
    monkeypatch.setattr(direct, "wake", lambda *a: None)
    db = direct_db()
    sid = _direct_row(db).id
    db.close()

    payload = {"type": "dm", "title": "New message", "body": "Open Messages"}
    assert direct.enqueue(sid, payload)
    first = direct._pending(sid)
    second = direct._pending(sid)
    assert first == second and first[0]["payload"] == payload
    assert first[0]["type"] == "notification"
    direct._ack(sid, first[0]["id"])
    assert direct._pending(sid) == []


def test_expired_calls_are_not_replayed(direct_db):
    db = direct_db()
    sid = _direct_row(db).id
    db.add(DirectPushMessage(subscription_id=sid, payload='{"type":"call"}',
                             expires_at=datetime.utcnow() - timedelta(seconds=1)))
    db.commit()
    assert direct._pending(sid) == []
    assert db.query(DirectPushMessage).count() == 0


def test_unregister_requires_matching_signed_device(direct_db, monkeypatch):
    db = direct_db()
    device = "12345678-1234-1234-1234-123456789abc"
    sid = _direct_row(db, device=device).id
    disconnected = []
    monkeypatch.setattr(direct, "disconnect", disconnected.append)
    monkeypatch.setattr(push_router, "_direct_auth", lambda _a, _p, action, _d: action == "unregister")
    result = asyncio.run(push_router.unregister_direct(
        _Request({"pubkey": "b" * 64, "device_id": device, "auth": "signed"}), db))
    assert result == {"ok": True}
    assert db.query(PushSubscription).count() == 0
    assert disconnected == [sid]


def test_keyless_webpush_cannot_restore_unifiedpush(direct_db, monkeypatch):
    monkeypatch.setattr(nostr_event, "verify_self_auth", lambda *a: True)
    db = direct_db()
    result = asyncio.run(push_router.subscribe(_Request({
        "pubkey": "b" * 64,
        "auth": "signed",
        "subscription": {"endpoint": "https://ntfy.invalid/a", "keys": {}},
    }), db))
    assert result["ok"] is False
    assert db.query(PushSubscription).count() == 0
