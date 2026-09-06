"""Exercise signed applications and approval DMs without contacting a real relay."""
import asyncio
import base64
import json

import httpx
import pytest
from fastapi import FastAPI
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from app.database import Base, get_db
from app.models import User
from app.routers import instance_welcome as routes
from app.services import instance_welcome as service, settings_store, system_dm
from app.services.nostr.event import build_event


def proof(action='apply', key='21', **kw):
    ev = build_event(bytes.fromhex(key * 32), 27235, 'instance-welcome-' + action, **kw)
    return {'pubkey': ev['pubkey'], 'auth': base64.b64encode(json.dumps(ev).encode()).decode()}


@pytest.fixture
def setup(monkeypatch):
    engine = create_engine('sqlite://', poolclass=StaticPool, connect_args={'check_same_thread': False})
    Base.metadata.create_all(engine)
    db = Session(engine)
    admin = User(username='admin', password_hash='unused', is_admin=True, nostr_npub=proof(key='22')['pubkey'])
    db.add(admin); db.commit()
    values = {'site_name': 'Example', 'nostr_relay_nip05_domain': 'example.test'}
    monkeypatch.setattr(settings_store, 'get', lambda key, default='': values.get(key, default))
    sent = []
    async def send(to, text):
        sent.append((to, text)); return True
    monkeypatch.setattr(system_dm, 'send', send)
    app = FastAPI(); app.include_router(routes.router)
    app.dependency_overrides[get_db] = lambda: db
    yield app, db, sent, values
    db.close(); engine.dispose()


def call(app, path, body):
    async def run():
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='https://example.test') as client:
            return await client.post('/api/instance-welcome/' + path, json=body)
    return asyncio.run(run())


def test_eligibility_uses_instance_assignment_not_external_profile(setup):
    app, db, sent, values = setup
    body = proof('status')
    assert call(app, 'status', body).json()['eligible']
    values['nostr_relay_nip05_names'] = 'alice ' + body['pubkey']
    result = call(app, 'status', body).json()
    assert not result['eligible'] and result['address'] == 'alice@example.test'
    assert sent == []


def test_application_is_durable_idempotent_and_not_an_automatic_grant(setup):
    app, db, sent, values = setup
    first = call(app, 'apply', proof()).json()
    second = call(app, 'apply', proof()).json()
    assert first['created'] and not second['created']
    assert db.query(service.InstanceApplication).count() == 1
    assert len(sent) == 1 and 'Permissions' in sent[0][1]
    from app.services.nostr import nostr_service
    mention = sent[0][1].split('\n\n')[1]
    assert mention.startswith('nostr:npub1')
    assert nostr_service.to_pubkey_hex(mention.removeprefix('nostr:')) == proof()['pubkey']
    assert not values.get('nostr_relay_nip05_names')
    assert call(app, 'status', proof('status')).json()['pending']


@pytest.mark.parametrize('body', [proof('status'), proof(created_at=1), {'pubkey': 'bad', 'auth': 'bad'},
    {**proof(), 'pubkey': proof(key='23')['pubkey']}])
def test_invalid_wrong_action_stale_and_other_identity_proofs_cannot_apply(setup, body):
    app, db, sent, _ = setup
    assert call(app, 'apply', body).status_code == 403
    assert db.query(service.InstanceApplication).count() == 0 and not sent


def test_existing_member_does_not_create_application(setup):
    app, db, sent, values = setup
    values['nostr_relay_nip05_names'] = 'alice ' + proof()['pubkey']
    assert call(app, 'apply', proof()).json()['already']
    assert db.query(service.InstanceApplication).count() == 0 and not sent


def test_approval_dm_contains_address_and_setup_steps_and_is_not_duplicated(setup):
    app, db, sent, _ = setup
    call(app, 'apply', proof())
    pk = proof()['pubkey']
    assert asyncio.run(service.notify_approval(db, pk, 'alice@example.test'))
    assert asyncio.run(service.notify_approval(db, pk, 'alice@example.test'))
    assert len(sent) == 2
    assert sent[-1][0] == pk
    assert all(word in sent[-1][1] for word in ('approved', 'alice@example.test', 'Edit profile', 'save'))


def test_failed_approval_dm_can_be_retried_without_losing_approval(setup, monkeypatch):
    app, db, sent, _ = setup
    call(app, 'apply', proof()); pk = proof()['pubkey']
    async def fail(*args): return False
    original = system_dm.send
    monkeypatch.setattr(system_dm, 'send', fail)
    assert not asyncio.run(service.notify_approval(db, pk, 'alice@example.test'))
    row = service.application(db, pk)
    assert row.approved_address == 'alice@example.test' and not row.notified_address
    monkeypatch.setattr(system_dm, 'send', original)
    row.approval_retry_at = 0; db.commit()
    assert asyncio.run(service.notify_approval(db, pk, 'alice@example.test'))
    assert row.notified_address == 'alice@example.test'


def test_partial_admin_delivery_retries_only_the_missing_recipient(setup, monkeypatch):
    app, db, sent, _ = setup
    second = User(username='second', password_hash='unused', is_admin=True,
                  nostr_npub=proof(key='24')['pubkey'])
    db.add(second); db.commit()
    original = system_dm.send
    async def partial(to, text):
        if to == second.nostr_npub: return False
        return await original(to, text)
    monkeypatch.setattr(system_dm, 'send', partial)
    assert not call(app, 'apply', proof()).json()['admin_notified']
    row = service.application(db, proof()['pubkey'])
    row.admin_retry_at = 0; db.commit()
    monkeypatch.setattr(system_dm, 'send', original)
    assert call(app, 'apply', proof()).json()['admin_notified']
    assert len(sent) == 2 and len({to for to, text in sent}) == 2


def test_background_retry_delivers_without_user_reopening_app(setup, monkeypatch):
    app, db, sent, _ = setup
    original = system_dm.send
    async def fail(*args): return False
    monkeypatch.setattr(system_dm, 'send', fail)
    call(app, 'apply', proof())
    pk = proof()['pubkey']
    row = service.application(db, pk)
    row.admin_retry_at = 0; db.commit()
    monkeypatch.setattr(system_dm, 'send', original)
    asyncio.run(service.retry_notifications(db))
    assert row.admin_notified and len(sent) == 1
    monkeypatch.setattr(system_dm, 'send', fail)
    asyncio.run(service.notify_approval(db, pk, 'alice@example.test'))
    row.approval_retry_at = 0; db.commit()
    monkeypatch.setattr(system_dm, 'send', original)
    asyncio.run(service.retry_notifications(db))
    asyncio.run(service.retry_notifications(db))
    assert row.notified_address == 'alice@example.test' and len(sent) == 2


def test_real_admin_grant_requires_admin_signature_and_sends_approval(setup, monkeypatch):
    from app.routers.client import router
    from app.services.nostr import nostr_service
    from app.services.nostr_relay import thread
    app, db, sent, values = setup
    app.include_router(router)
    monkeypatch.setattr(settings_store, 'put', lambda key, value: values.__setitem__(key, value))
    monkeypatch.setattr(settings_store, 'hydrate_from_db', lambda db: None)
    monkeypatch.setattr(thread, 'trigger_nip05_reload', lambda: None)
    admin = db.query(User).filter(User.is_admin == True).first()
    admin.nostr_npub = nostr_service.npub_of(proof(key='22')['pubkey']); db.commit()
    call(app, 'apply', proof())
    pk = proof()['pubkey']
    async def grant(key):
        ev = build_event(bytes.fromhex(key * 32), 27235, 'nip05', [['p', pk]])
        body = {'target': pk, 'name': 'alice', 'auth': base64.b64encode(json.dumps(ev).encode()).decode()}
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app), base_url='https://example.test') as client:
            return await client.post('/client/admin-nip05', json=body)
    assert asyncio.run(grant('21')).status_code == 403
    assert len(sent) == 1
    response = asyncio.run(grant('22'))
    assert response.status_code == 200 and response.json()['nip05'] == 'alice@example.test'
    assert len(sent) == 2 and 'approved' in sent[-1][1]
    assert asyncio.run(grant('22')).status_code == 200
    assert len(sent) == 2
