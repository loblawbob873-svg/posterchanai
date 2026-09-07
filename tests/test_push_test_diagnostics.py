"""Real DB queue and endpoint diagnostics: acceptance is not delivery; failures retain devices."""
import asyncio
import base64
import json
import threading
from types import SimpleNamespace

import pytest

from tests.test_direct_push_server import direct_db, _direct_row, _Request
from app.models import PushSubscription
from app.routers import push as router
from app.services import direct_push_service as direct, push_service as push


REAL_AUTH = router.nostr_event.verify_self_auth


def request(device=None):
    data = {'pubkey': 'b' * 64, 'auth': 'signed'}
    if device is not None:
        data['device_id'] = device
    return _Request(data)


@pytest.fixture(autouse=True)
def auth(monkeypatch):
    monkeypatch.setattr(router.nostr_event, 'verify_self_auth', lambda *a: True)


def test_native_test_targets_only_current_owned_device_and_persists_queue(direct_db):
    db = direct_db()
    first = _direct_row(db, device='first-phone-12345678')
    second = _direct_row(db, device='second-phone-12345678', digest='c' * 64)
    result = asyncio.run(router.test_push(request(first.device_id), db))
    assert result['ok'] and result['queued'] == 1 and result['accepted'] == 0
    assert len(direct._pending(first.id)) == 1
    assert direct._pending(second.id) == []
    frame = direct._pending(first.id)[0]
    assert frame['payload']['type'] == 'test'
    direct._ack(first.id, frame['id'])
    assert direct._pending(first.id) == []


def test_other_devices_cannot_mask_missing_current_registration(direct_db):
    db = direct_db()
    _direct_row(db)
    result = asyncio.run(router.test_push(request('missing-device-12345678'), db))
    assert not result['ok'] and result['devices'] == 0


@pytest.mark.parametrize('outcome', ['failed', 'expired', 'accepted', 'queued'])
def test_diagnostic_counts_and_retention_and_nonblocking_send(direct_db, monkeypatch, outcome):
    db = direct_db()
    _direct_row(db)
    caller = threading.get_ident()
    def send(*args):
        assert threading.get_ident() != caller, 'push blocked the websocket event loop'
        return outcome
    monkeypatch.setattr(push, 'send_result', send)
    result = asyncio.run(router.test_push(request(), db))
    assert result['ok'] == (outcome in ('accepted', 'queued'))
    assert result[outcome] == 1
    assert db.query(PushSubscription).count() == (0 if outcome == 'expired' else 1)


@pytest.mark.parametrize('device', ['', 123, [], 'x'])
def test_invalid_device_is_rejected(direct_db, device):
    assert asyncio.run(router.test_push(request(device), direct_db()))['error'] == 'invalid device_id'


def test_failed_direct_queue_is_not_claimed_as_sent_or_deleted(direct_db, monkeypatch):
    db = direct_db()
    _direct_row(db)
    monkeypatch.setattr(direct, 'enqueue_result', lambda *a: 'failed')
    result = asyncio.run(router.test_push(request(), db))
    assert not result['ok'] and result['failed'] == 1 and result['delivered'] == 0
    assert db.query(PushSubscription).count() == 1
    assert push.send({'transport': direct.TRANSPORT, 'id': 1}, {'type': 'dm'}) is True


@pytest.mark.parametrize('code,expected', [(None, 'accepted'), (410, 'expired'), (503, 'failed')])
def test_real_webpush_adapter_distinguishes_acceptance_failure_and_expiry(monkeypatch, code, expected):
    import pywebpush
    monkeypatch.setattr(push.settings_store, 'get', lambda *a: 'private')
    def webpush(**kwargs):
        if code:
            raise pywebpush.WebPushException('test failure', response=SimpleNamespace(status_code=code))
    monkeypatch.setattr(pywebpush, 'webpush', webpush)
    sub = {'endpoint': 'https://example.test/push', 'keys': {'p256dh': 'key', 'auth': 'auth'}}
    assert push.send_result(sub, {'type': 'test'}) == expected
    assert push.send(sub, {'type': 'dm'}) == (expected != 'expired')


def test_actual_queue_failure_retains_subscription(direct_db, monkeypatch):
    db = direct_db()
    row = _direct_row(db)
    from app.models import DirectPushMessage
    DirectPushMessage.__table__.drop(db.get_bind())
    assert direct.enqueue_result(row.id, {'type': 'test'}) == 'failed'
    assert direct.enqueue(row.id, {'type': 'dm'}) is True
    assert db.query(PushSubscription).count() == 1


def test_signed_owner_cannot_test_other_accounts_device(direct_db):
    db = direct_db()
    row = _direct_row(db)
    row.pubkey = 'c' * 64
    db.commit()
    result = asyncio.run(router.test_push(request(row.device_id), db))
    assert not result['ok'] and result['devices'] == 0
    assert direct._pending(row.id) == []


def test_real_signed_test_proof_and_invalid_signature(direct_db, monkeypatch):
    monkeypatch.setattr(router.nostr_event, 'verify_self_auth', REAL_AUTH)
    event = router.nostr_event.build_event(bytes.fromhex('01' * 32), 27235, 'push-test')
    db = direct_db()
    row = _direct_row(db)
    row.pubkey = event['pubkey']
    db.commit()
    body = {'pubkey': row.pubkey, 'device_id': row.device_id,
            'auth': base64.b64encode(json.dumps(event).encode()).decode()}
    result = asyncio.run(router.test_push(_Request(body), db))
    assert result['ok'] and result['queued'] == 1
    event['sig'] = '0' * 128
    body['auth'] = base64.b64encode(json.dumps(event).encode()).decode()
    assert asyncio.run(router.test_push(_Request(body), db))['error'] == 'auth required'
    assert len(direct._pending(row.id)) == 1
