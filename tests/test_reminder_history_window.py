"""The notifications panel gets RECENT reminders, not every one ever delivered.

Reported from the PosterChanOS desktop: "why is desktop showing old calendar reminders in
notifications now" / "my notifications just got flooded with it". Nothing had re-fired — the
database held three calendar reminders, all already delivered. What shipped that day was reminder
history import, and this endpoint returned every delivered reminder the account had, back to June.
A notification list is about what recently happened; an eight-week-old reminder is not news, and
burying today's items under it is worse than showing nothing.

Nothing is lost by bounding it: reminder_service.deliver also persists every reminder into the
"⏰ Reminders" conversation, which is the durable history and is not age-bounded.

The reminders live in the `reminders` DocTable on this node's relay (#161), so the rows below are written there
and the endpoint reads them back through the shipped relay (tests/app_tables_harness.py).
"""
from datetime import datetime, timedelta
import pytest
from contextlib import contextmanager
from fastapi import FastAPI
from fastapi.testclient import TestClient
from app.services.app_tables import new_id
from app.services.doc_table import DocTable
from tests.reminder_api_harness import reminder_modules
from tests.app_tables_harness import tables, shared_relay  # noqa: F401


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


class _Rows:
    """Stands where the SQL session stood: `add`/`add_all` write reminder rows to the relay."""
    def add(self, row):
        DocTable('reminders').put(*row)

    def add_all(self, rows):
        for row in rows:
            self.add(row)

    def commit(self):
        pass


def _client(db, user_id=1):
    app = FastAPI(); app.include_router(router)
    app.dependency_overrides[get_db] = lambda: None
    app.dependency_overrides[get_current_user] = lambda: User(id=user_id, username="u")
    return TestClient(app)


@contextmanager
def _db():
    yield _Rows()


def _texts(client):
    r = client.get('/api/auth/reminder-notifications')
    assert r.status_code == 200, r.text
    return [i["content"] for i in r.json()["items"]]


def test_a_reminder_from_months_ago_is_not_in_todays_notifications():
    now = datetime.utcnow()
    with _db() as db:
        db.add_all([
            Reminder(user_id=1, text='📅 DENTIST phil and kids', due_at=now - timedelta(hours=2),
                     delivered_at=now - timedelta(hours=2), status='done'),
            Reminder(user_id=1, text='open oven', due_at=now - timedelta(days=75),
                     delivered_at=now - timedelta(days=75), status='done'),
        ]); db.commit()
        got = _texts(_client(db))
    assert any('DENTIST' in t for t in got), got
    assert not any('open oven' in t for t in got), 'a reminder from months ago flooded the panel'


def test_the_boundary_is_inclusive_and_measured_from_delivery():
    now = datetime.utcnow()
    with _db() as db:
        db.add_all([
            Reminder(user_id=1, text='just inside', due_at=now - timedelta(days=6, hours=23),
                     delivered_at=now - timedelta(days=6, hours=23), status='done'),
            Reminder(user_id=1, text='just outside', due_at=now - timedelta(days=7, hours=1),
                     delivered_at=now - timedelta(days=7, hours=1), status='done'),
        ]); db.commit()
        got = _texts(_client(db))
    assert any('just inside' in t for t in got), got
    assert not any('just outside' in t for t in got), got


def test_a_row_delivered_before_delivered_at_existed_still_counts():
    """Older rows can have a null delivered_at; due_at is the fallback, not an exclusion."""
    now = datetime.utcnow()
    with _db() as db:
        db.add(Reminder(user_id=1, text='no delivered_at', due_at=now - timedelta(hours=1),
                        delivered_at=None, status='done')); db.commit()
        got = _texts(_client(db))
    assert any('no delivered_at' in t for t in got), got


def test_another_account_is_still_invisible():
    """The age bound must not be mistaken for the ownership filter."""
    now = datetime.utcnow()
    with _db() as db:
        db.add(Reminder(user_id=2, text='someone elses', due_at=now, delivered_at=now, status='done'))
        db.commit()
        got = _texts(_client(db, user_id=1))
    assert got == [], got


def test_history_reports_the_configured_window_to_offline_clients(monkeypatch):
    from app.services import settings_store
    monkeypatch.setattr(settings_store, 'get', lambda *args: 30)
    now = datetime.utcnow()
    with _db() as db:
        db.add(Reminder(user_id=1, text='inside configured window', due_at=now-timedelta(days=20),
                        delivered_at=now-timedelta(days=20), status='done'))
        db.commit()
        result = _client(db).get('/api/auth/reminder-notifications').json()
        assert result['history_days'] == 30
        assert len(result['items']) == 1
