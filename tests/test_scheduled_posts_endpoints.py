"""Scheduled posts: the three ENDPOINTS the client calls — /client/scheduled, /list, /cancel.

Run: venv-unified/bin/python -m pytest tests/test_scheduled_posts_endpoints.py

tests/test_a_scheduled_post_goes_out_once.py drives the scheduler's state machine. Nothing drove the
door in front of it, and that door is where the security lives: the server BROADCASTS a stored event
verbatim later, under the author's name, so what it agrees to store is what it will publish. Every
check here is driven through the real route functions against a real (sqlite) database with REALLY
signed events and a real kind-27235 ownership proof — no signature or auth helper is stubbed.

  proof         no valid, fresh, self-signed proof → 403, and nothing is stored;
  yours         an event signed by somebody else (or tampered after signing) is refused;
  kinds         only kind 1 notes and kind 1068 polls — never a profile, a follow list, a deletion;
  when          not in the past, not beyond the cap, and created_at must be the scheduled time
                (it is the time the published note shows, and a strict relay rejects future events);
  cap           a per-user limit on pending rows;
  list/cancel   scoped to the proven owner — another account sees nothing and cancels nothing.
"""
import asyncio
import base64
import json
import time

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import ScheduledPost, User
from app.routers import client
from app.services.nostr import event as nostr_event, nostr_service

SK_A, SK_B = b"\x11" * 32, b"\x22" * 32
PK_A = nostr_event.build_event(SK_A, 1, "x")["pubkey"]
PK_B = nostr_event.build_event(SK_B, 1, "x")["pubkey"]


def run(c):
    return asyncio.run(c)


def body(r):
    return r.status_code, json.loads(r.body)


def proof(sk, created_at=None):
    ev = nostr_event.build_event(sk, 27235, "", tags=[], created_at=created_at)
    return base64.b64encode(json.dumps(ev).encode()).decode()


def note(sk, when, kind=1, content="later, world"):
    return nostr_event.build_event(sk, kind, content, tags=[], created_at=when)


@pytest.fixture
def db():
    engine = create_engine("sqlite://")
    User.__table__.create(engine)
    ScheduledPost.__table__.create(engine)
    s = sessionmaker(bind=engine)()
    for pk, name in ((PK_A, "alice"), (PK_B, "bob")):
        s.add(User(username=name, email=f"{name}@x.example", password_hash="x", nostr_npub=nostr_service.npub_of(pk)))
    s.commit()
    yield s
    s.close()


def create(db, sk, pk, ev, when, auth=None):
    return body(run(client.scheduled_create(client.ScheduledCreateReq(
        pubkey=pk, auth=auth if auth is not None else proof(sk), event=ev, scheduled_at=when), db=db)))


def rows(db):
    return db.query(ScheduledPost).all()


def test_a_valid_schedule_is_stored_exactly_as_signed(db):
    when = int(time.time()) + 3600
    ev = note(SK_A, when)
    code, j = create(db, SK_A, PK_A, ev, when)
    assert code == 200 and j["ok"] and j["scheduled_at"] == when
    (r,) = rows(db)
    assert r.status == "pending" and r.event_id == ev["id"]
    assert json.loads(r.event_json) == ev, "what is broadcast later must be byte-for-byte what was signed"
    assert nostr_event.verify_event(json.loads(r.event_json))


def test_a_poll_can_be_scheduled(db):
    when = int(time.time()) + 600
    assert create(db, SK_A, PK_A, note(SK_A, when, kind=1068), when)[1]["ok"]


@pytest.mark.parametrize("auth", ["", "not base64", "__stale__", "__bob__", "__note__"])
def test_no_valid_ownership_proof_no_schedule(db, auth):
    when = int(time.time()) + 3600
    a = {"__stale__": proof(SK_A, created_at=int(time.time()) - 3600),
         "__bob__": proof(SK_B),
         "__note__": base64.b64encode(json.dumps(note(SK_A, int(time.time()))).encode()).decode()}.get(auth, auth)
    code, j = create(db, SK_A, PK_A, note(SK_A, when), when, auth=a)
    assert code == 403 and not j["ok"] and not rows(db)


def test_somebody_else_s_note_cannot_be_queued_under_your_proof(db):
    """Your proof + bob's signed note: the server would broadcast bob's words on a schedule you chose."""
    when = int(time.time()) + 3600
    code, j = create(db, SK_A, PK_A, note(SK_B, when), when)
    assert code == 400 and not rows(db)


def test_a_tampered_note_is_refused(db):
    when = int(time.time()) + 3600
    ev = dict(note(SK_A, when), content="edited after signing")
    code, _ = create(db, SK_A, PK_A, ev, when)
    assert code == 400 and not rows(db)


@pytest.mark.parametrize("kind", [0, 3, 5, 6, 7, 10002, 30078])
def test_only_notes_and_polls(db, kind):
    """A kind-0 or kind-3 queued for later would REPLACE the profile or follow list whenever it fired."""
    when = int(time.time()) + 3600
    code, j = create(db, SK_A, PK_A, note(SK_A, when, kind=kind), when)
    assert code == 400 and not rows(db), kind


def test_the_time_rules(db):
    now = int(time.time())
    past = now - 600
    assert create(db, SK_A, PK_A, note(SK_A, past), past)[0] == 400, "a time in the past"
    far = now + 301 * 86400
    assert create(db, SK_A, PK_A, note(SK_A, far), far)[0] == 400, "beyond the cap"
    when = now + 3600
    assert create(db, SK_A, PK_A, note(SK_A, when + 600), when)[0] == 400, \
        "created_at must be the scheduled time (it is the time the note shows)"
    assert create(db, SK_A, PK_A, note(SK_A, when + 60), when)[1]["ok"], "a small clock skew is allowed"
    assert len(rows(db)) == 1


def test_an_unknown_account_cannot_schedule(db):
    sk = b"\x33" * 32
    pk = nostr_event.build_event(sk, 1, "x")["pubkey"]
    when = int(time.time()) + 3600
    code, _ = create(db, sk, pk, note(sk, when), when)
    assert code == 403 and not rows(db)


def test_the_per_user_cap(db, monkeypatch):
    monkeypatch.setattr(client, "_MAX_PENDING_SCHEDULES", 3)
    base = int(time.time()) + 3600
    for i in range(3):
        assert create(db, SK_A, PK_A, note(SK_A, base + i, content=f"n{i}"), base + i)[1]["ok"]
    code, j = create(db, SK_A, PK_A, note(SK_A, base + 9, content="one too many"), base + 9)
    assert code == 429 and len(rows(db)) == 3
    # …and it is per USER: bob is not blocked by alice's queue.
    assert create(db, SK_B, PK_B, note(SK_B, base), base)[1]["ok"]


def test_list_and_cancel_belong_to_the_owner(db):
    when = int(time.time()) + 3600
    rid = create(db, SK_A, PK_A, note(SK_A, when), when)[1]["id"]

    def lst(sk, pk):
        return body(run(client.scheduled_list(client.ScheduledAuthReq(pubkey=pk, auth=proof(sk)), db=db)))

    def cancel(sk, pk, i):
        return body(run(client.scheduled_cancel(client.ScheduledCancelReq(pubkey=pk, auth=proof(sk), id=i), db=db)))

    assert [p["id"] for p in lst(SK_A, PK_A)[1]["posts"]] == [rid]
    assert lst(SK_B, PK_B)[1]["posts"] == [], "bob sees alice's schedule"
    assert body(run(client.scheduled_list(client.ScheduledAuthReq(pubkey=PK_A, auth=proof(SK_B)), db=db)))[0] == 403, \
        "listing somebody else's schedule with your own proof"
    assert not cancel(SK_B, PK_B, rid)[1]["ok"], "bob cancelled alice's post"
    assert rows(db)[0].status == "pending"
    assert cancel(SK_A, PK_A, rid)[1]["ok"]
    assert lst(SK_A, PK_A)[1]["posts"] == [], "a cancelled post is still listed"
    assert not cancel(SK_A, PK_A, rid)[1]["ok"], "cancelling twice reports success"
