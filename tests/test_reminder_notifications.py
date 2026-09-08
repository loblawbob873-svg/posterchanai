"""Private durable reminder history and live payload share occurrence identities."""
from datetime import datetime, timedelta
from types import SimpleNamespace
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool
from app.models import Reminder
from app.routers.auth import router
from app.auth import get_current_user
from app.database import get_db
from app.services.reminder_service import notification_record


def test_reminder_history_authenticated_owner_status_limit_and_repeat_identity():
    engine=create_engine('sqlite://',connect_args={'check_same_thread':False},poolclass=StaticPool)
    Reminder.__table__.create(engine)
    with Session(engine) as db:
        now=datetime(2026,9,8,12)
        for i in range(205):
            db.add(Reminder(user_id=1,text='📅 Event '+str(i),due_at=now+timedelta(minutes=i),
                            delivered_at=now+timedelta(minutes=i),status='done'))
        db.add_all([Reminder(user_id=2,text='private other owner',due_at=now,status='done'),
                    Reminder(user_id=1,text='pending',due_at=now,status='pending'),
                    Reminder(user_id=1,text='cancelled',due_at=now,status='cancelled')]);db.commit()
        app=FastAPI();app.include_router(router)
        app.dependency_overrides[get_db]=lambda:db
        with TestClient(app) as client:
            assert client.get('/api/auth/reminder-notifications').status_code in (401,403)
            app.dependency_overrides[get_current_user]=lambda:SimpleNamespace(id=1)
            items=client.get('/api/auth/reminder-notifications').json()['items']
            assert len(items)==200
            assert all('Event' in r['content'] and r['route']=='calendar' for r in items)
            assert items[0]['content'].endswith('Event 204')
            first=dict(items[0]);row=db.get(Reminder,first['reminder_id'])
            row.due_at+=timedelta(days=1);row.delivered_at+=timedelta(days=1);db.commit()
            repeated=notification_record(row)
            assert repeated['reminder_id']==first['reminder_id'] and repeated['due_at']!=first['due_at']
            app.dependency_overrides[get_current_user]=lambda:SimpleNamespace(id=2)
            other=client.get('/api/auth/reminder-notifications').json()['items']
            assert len(other)==1 and other[0]['content'].endswith('private other owner')
            assert other[0]['route']=='notifications'
    engine.dispose()


def test_live_delivery_keeps_ai_archive_and_push_uses_calendar_view(monkeypatch):
    import asyncio
    from app.models import User, PushSubscription
    from app.services import reminder_service, chat_history, push_service, direct_push_service
    from app.services.nostr import nostr_service
    from app.routers.chat import manager
    engine=create_engine('sqlite://')
    for table in (User.__table__,Reminder.__table__,PushSubscription.__table__):table.create(engine)
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
        db.add(PushSubscription(pubkey='a'*64,endpoint='private-fixture',p256dh='fixture',auth='fixture'))
        reminder=Reminder(user_id=user.id,text='📅 Calendar appointment',due_at=datetime(2026,9,8),delivered_at=datetime(2026,9,8),status='done')
        db.add(reminder);db.commit();asyncio.run(reminder_service.deliver(db,reminder))
        assert archived==[(user.id,88,'⏰ Reminder: 📅 Calendar appointment')]
        assert live[0][1]['route']=='calendar' and live[0][1]['reminder_id']==reminder.id
        assert pushed[0]['view']=='calendar' and pushed[0]['due_at']==live[0][1]['due_at']
    engine.dispose()
