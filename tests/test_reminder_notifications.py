"""Private durable reminder history and live payload share occurrence identities.

Reminders live in the `reminders` DocTable on this node's relay (#161); the history rows are written there and
read back through the shipped relay (tests/app_tables_harness.py)."""
from datetime import datetime, timedelta
import pytest
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from app.services.app_tables import new_id
from app.services.doc_table import DocTable
from tests.reminder_api_harness import reminder_modules
from tests.app_tables_harness import tables, shared_relay, fresh_process_view  # noqa: F401


@pytest.fixture(autouse=True)
def isolated_reminder_modules(tables):
    global User, router, get_current_user, get_db, notification_record, service
    with reminder_modules() as harness:
        User = harness.models.User
        router, get_current_user, get_db = harness.router, harness.get_current_user, harness.get_db
        notification_record = harness.service.notification_record
        service = harness.service
        yield harness


def Reminder(user_id, text, due_at, delivered_at=None, status='pending'):
    return (str(new_id()), service.reminder_row(user_id, text, due_at, status, due_at, delivered_at))


def _put_all(rows):
    t = DocTable('reminders')
    for k, row in rows:
        t.put(k, row)



def test_reminder_history_authenticated_owner_status_limit_and_repeat_identity():
    now=datetime.utcnow()-timedelta(days=1)
    rows=[Reminder(user_id=1,text='📅 Event '+str(i),due_at=now+timedelta(minutes=i),
                   delivered_at=now+timedelta(minutes=i),status='done') for i in range(205)]
    rows+=[Reminder(user_id=2,text='private other owner',due_at=now,status='done'),
           Reminder(user_id=1,text='pending',due_at=now,status='pending'),
           Reminder(user_id=1,text='cancelled',due_at=now,status='cancelled')]
    _put_all(rows)
    app=FastAPI();app.include_router(router)
    app.dependency_overrides[get_db]=lambda:None
    with TestClient(app) as client:
        assert client.get('/api/auth/reminder-notifications').status_code in (401,403)
        app.dependency_overrides[get_current_user]=lambda:SimpleNamespace(id=1)
        items=client.get('/api/auth/reminder-notifications').json()['items']
        assert len(items)==200
        assert all('Event' in r['content'] and r['route']=='calendar' for r in items)
        assert items[0]['content'].endswith('Event 204')
        first=dict(items[0])
        stored=fresh_process_view('reminders').get(str(first['reminder_id']))
        row=service.as_reminder(first['reminder_id'],stored)
        row.due_at+=timedelta(days=1);row.delivered_at+=timedelta(days=1)
        repeated=notification_record(row)
        assert repeated['reminder_id']==first['reminder_id'] and repeated['due_at']!=first['due_at']
        app.dependency_overrides[get_current_user]=lambda:SimpleNamespace(id=2)
        other=client.get('/api/auth/reminder-notifications').json()['items']
        assert len(other)==1 and other[0]['content'].endswith('private other owner')
        assert other[0]['route']=='notifications'


def test_live_delivery_keeps_ai_archive_and_push_uses_calendar_view(monkeypatch):
    import asyncio
    from app.models import User
    from app.services import reminder_service, chat_history, push_service, direct_push_service, push_store
    from app.services.nostr import nostr_service
    from app.routers.chat import manager
    # The push devices are push_store documents (#161); this harness stubs settings, so the device table
    # is stubbed at push_store's boundary (tests/test_push_store.py runs that boundary on a real relay).
    async def subs_for(pks):
        return {'a'*64:[{'id':1,'pubkey':'a'*64,'endpoint':'private-fixture','p256dh':'fixture',
                         'auth':'fixture','prefs':None}]} if 'a'*64 in pks else {}
    monkeypatch.setattr(push_store,'subs_for',subs_for)
    engine=create_engine('sqlite://')
    for table in (User.__table__,):table.create(engine)
    archived=[];live=[];pushed=[]
    async def archive(db,user,conversation,role,body):archived.append((user.id,conversation,body))
    async def socket(owner,data):live.append((owner,data))
    monkeypatch.setattr(chat_history,'append',archive)
    monkeypatch.setattr(manager,'send_json',socket)
    monkeypatch.setattr(reminder_service,'_get_or_create_reminders_chat',lambda db,uid:SimpleNamespace(id=88))
    monkeypatch.setattr(nostr_service,'to_pubkey_hex',lambda value:'a'*64)
    monkeypatch.setattr(direct_push_service,'subscription_dict',lambda row:{'endpoint':'private-fixture'})
    monkeypatch.setattr(push_service,'send',lambda sub,payload:pushed.append(payload) or True)
    with Session(engine) as db:
        user=User(username='fixture',password_hash='unused',nostr_npub='fixture',telegram_enabled=False)
        db.add(user);db.flush()
        db.commit()
        rid,row=Reminder(user_id=user.id,text='📅 Calendar appointment',due_at=datetime(2026,9,8),delivered_at=datetime(2026,9,8),status='done')
        reminder=service.as_reminder(rid,row);asyncio.run(reminder_service.deliver(db,reminder))
        assert archived==[(user.id,88,'⏰ Reminder: 📅 Calendar appointment')]
        assert live[0][1]['route']=='calendar' and live[0][1]['reminder_id']==reminder.id
        assert pushed[0]['view']=='calendar' and pushed[0]['due_at']==live[0][1]['due_at']
    engine.dispose()


def test_isolated_modules_restore_existing_imports_and_model_registry(isolated_reminder_modules):
    import sys
    import app
    from app.services import settings_store
    before = {name: sys.modules[name] for name in (
        'app.database', 'app.models', 'app.routers.auth', 'app.services.settings_store')}
    outer_models = isolated_reminder_modules.models
    with reminder_modules() as nested:
        assert nested.models.Base.metadata is not outer_models.Base.metadata
        assert nested.models.User.registry is not outer_models.User.registry
        assert app.models is nested.models
        assert nested.settings is not settings_store
        nested.settings.get = lambda *args: 365
    assert all(sys.modules[name] is module for name, module in before.items())
    assert app.models is outer_models
    from app.services import settings_store as restored_settings
    assert restored_settings is settings_store
    assert restored_settings.get('reminder_history_days') == 7
