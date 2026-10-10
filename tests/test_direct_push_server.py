"""Security and reconnect behavior for the first-party PosterChan Direct server transport.

The device rows and the queue are documents on this node's relay now (#161, push_store), so this runs
against the REAL relay (tests/push_relay_harness.py) -- every assertion the SQL version made is kept.
"""
import asyncio
import base64
import json
import time

import pytest

from app.routers import push as push_router
from app.services import direct_push_service as direct
from app.services import push_store
from app.services.nostr import event as nostr_event
from tests import push_relay_harness as H


@pytest.fixture()
def direct_db(tmp_path, monkeypatch):
    r = H.start(tmp_path, monkeypatch)
    yield lambda: None
    H.reset()
    r.close()


class _Request:
    def __init__(self, body):
        self.body = body

    async def json(self):
        return self.body


class _Row(dict):
    """A push_store row with attribute reads, so assertions written against the ORM row still read."""
    def __getattr__(self, name):
        try:
            return self[name]
        except KeyError:
            raise AttributeError(name)


def _direct_row(db=None, device="12345678-1234-1234-1234-123456789abc", digest="a" * 64, pubkey="b" * 64):
    return _Row(H.add_sub(pubkey=pubkey, endpoint=f"direct:{pubkey}:{device}", transport=direct.TRANSPORT,
                          device_id=device, token_hash=digest, p256dh=None, auth=None))


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
    direct_db()
    device = "12345678-1234-1234-1234-123456789abc"
    result = asyncio.run(push_router.register_direct(
        _Request({"pubkey": "b" * 64, "device_id": device, "auth": "signed"})))
    [row] = H.fresh_subs()
    assert result["ok"] and result["device_id"] == device
    assert result["websocket_url"] == "/api/push/direct/ws"
    assert len(result["token"]) >= 32
    assert row["token_hash"] == direct.token_digest(result["token"])
    assert result["token"] not in row["endpoint"]
    assert result["token"] != row["token_hash"]
    assert row["transport"] == direct.TRANSPORT
    # the token never reaches the relay, not even encrypted
    assert result["token"] not in json.dumps(row)


def test_direct_queue_survives_reconnect_until_ack(direct_db, monkeypatch):
    monkeypatch.setattr(direct, "wake", lambda *a: None)
    db = direct_db()
    sid = _direct_row(db).id

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
    push_store.direct().put("5", {"sub": sid, "payload": '{"type":"call"}', "at": int(time.time()) - 91,
                                  "exp": int(time.time()) - 1})
    assert direct._pending(sid) == []
    H.reset()
    assert push_store.direct().all() == {}


def test_unregister_requires_matching_signed_device(direct_db, monkeypatch):
    db = direct_db()
    device = "12345678-1234-1234-1234-123456789abc"
    sid = _direct_row(db, device=device).id
    disconnected = []
    monkeypatch.setattr(direct, "disconnect", disconnected.append)
    monkeypatch.setattr(push_router, "_direct_auth", lambda _a, _p, action, _d: action == "unregister")
    result = asyncio.run(push_router.unregister_direct(
        _Request({"pubkey": "b" * 64, "device_id": device, "auth": "signed"})))
    assert result == {"ok": True}
    assert H.fresh_subs() == []
    assert disconnected == [sid]


def test_keyless_webpush_cannot_restore_unifiedpush(direct_db, monkeypatch):
    monkeypatch.setattr(nostr_event, "verify_self_auth", lambda *a: True)
    direct_db()
    result = asyncio.run(push_router.subscribe(_Request({
        "pubkey": "b" * 64,
        "auth": "signed",
        "subscription": {"endpoint": "https://ntfy.invalid/a", "keys": {}},
    })))
    assert result["ok"] is False
    assert H.fresh_subs() == []


def test_live_wake_and_ack_together_drain_the_real_queue(direct_db, monkeypatch):
    """A simultaneous new message wake must not swallow the previous notification's receipt."""
    db = direct_db()
    sid = _direct_row(db).id
    assert direct.enqueue_result(sid, {'type': 'test'}) == 'queued'

    async def exercise():
        incoming = asyncio.Queue()
        acknowledged = asyncio.Event()
        loop = asyncio.get_running_loop()
        original_ack = direct._ack
        def ack(subscription, message):
            original_ack(subscription, message)
            loop.call_soon_threadsafe(acknowledged.set)
        monkeypatch.setattr(direct, '_ack', ack)
        class Socket:
            async def send_json(self, frame):
                if frame['type'] != 'notification':
                    return
                # Both tasks are ready when serve waits: this used to discard the ACK.
                direct._live[sid].wake.set()
                await incoming.put({'type': 'ack', 'id': frame['id']})
            async def receive_json(self):
                return await incoming.get()
        task = asyncio.create_task(direct.serve(Socket(), sid))
        try:
            await asyncio.wait_for(acknowledged.wait(), timeout=2)
        finally:
            task.cancel()
            await task
        assert sid not in direct._live
        assert await asyncio.to_thread(direct._pending, sid) == []
    asyncio.run(exercise())


def test_queue_wakes_do_not_cancel_an_inflight_device_receive(direct_db):
    """A message arriving while another is being ACKed keeps the same socket reader alive."""
    db = direct_db()
    sid = _direct_row(db).id
    async def exercise():
        started = asyncio.Event()
        class Socket:
            cancelled = 0
            async def send_json(self, frame):
                pass
            async def receive_json(self):
                started.set()
                try:
                    await asyncio.Future()
                except asyncio.CancelledError:
                    self.cancelled += 1
                    raise
        socket = Socket()
        task = asyncio.create_task(direct.serve(socket, sid))
        await started.wait()
        try:
            for _ in range(3):
                direct._live[sid].wake.set()
                await asyncio.sleep(.01)
            assert socket.cancelled == 0
        finally:
            task.cancel()
            await task
        assert socket.cancelled == 1
    asyncio.run(exercise())
