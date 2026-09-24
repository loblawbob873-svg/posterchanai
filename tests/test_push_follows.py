"""A NEW FOLLOWER BUZZES THE PHONE -- once, and only a new one.

"yes add push notifications for follows too". A follow is a kind-3 contact list that p-tags you, and a
contact list is republished every time its owner follows or unfollows ANYONE -- so pushing on every
kind-3 would re-announce every existing follower all day (which is why the open app never toasts
them). The watcher remembers who already follows whom (push_follow_seen): the first sighting of a
person seeds their existing followers silently, a follower it has never seen pushes "X followed you"
once, and a re-saved list pushes nothing. Drives the real `_poll()`; the relay and the devices are stubs.
"""
import asyncio

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, PushFollowSeen, PushSubscription
from app.services import nostr_push_service as nps
from app.services import push_prefs

ME = "a" * 64
OLD = "b" * 64          # already followed ME before any of this
NEW = "c" * 64          # follows ME for the first time


def _contacts(author, eid, *pks):
    return {"id": eid, "kind": 3, "pubkey": author, "created_at": 100, "content": "",
            "tags": [["p", pk] for pk in pks], "sig": ""}


@pytest.fixture
def world(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool, connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[PushSubscription.__table__, PushFollowSeen.__table__])
    Session = sessionmaker(bind=engine)
    monkeypatch.setattr("app.database.SessionLocal", Session)
    db = Session()
    db.add(PushSubscription(pubkey=ME, endpoint="https://push.example/phone", transport="webpush", p256dh="p", auth="a"))
    db.commit()
    db.close()
    relay_lists = [_contacts(OLD, "1" * 64, ME)]            # what the relay holds: OLD's list names ME
    poll_events = []
    sent = []

    async def query(relays, filters, timeout=8):
        f = filters[0]
        if f.get("kinds") == [3] and f.get("limit"):          # the seed: every list naming the recipient
            return list(relay_lists)
        return list(poll_events)
    monkeypatch.setattr(nps.relay, "query", query)

    async def name(pk):
        return {OLD: "Olga", NEW: "Nadia"}.get(pk, "")
    monkeypatch.setattr(nps, "_name_for", name)
    monkeypatch.setattr(nps.push_service, "send", lambda sub, payload: sent.append(payload) or True)
    monkeypatch.setattr(nps, "subscription_dict", lambda s: {"endpoint": s.endpoint})

    def poll(*events):
        poll_events[:] = events
        nps._cursor = 1
        asyncio.run(nps._poll())
        return sent
    return {"poll": poll, "sent": sent, "Session": Session, "relay_lists": relay_lists}


def test_an_existing_follower_resaving_their_list_is_not_a_new_follow(world):
    assert world["poll"](_contacts(OLD, "2" * 64, ME, "d" * 64)) == []


def test_a_new_follower_pushes_once_and_opens_notifications(world):
    sent = world["poll"](_contacts(OLD, "2" * 64, ME), _contacts(NEW, "3" * 64, ME))
    assert len(sent) == 1, sent
    push = sent[0]
    assert push["body"] == "🫂 Nadia followed you" and push["type"] == "follows"
    assert push["view"] == "notifications" and "eid" not in push
    # Nadia follows somebody else later: her whole list comes again -- and nothing is pushed
    world["sent"].clear()
    assert world["poll"](_contacts(NEW, "4" * 64, ME, "e" * 64)) == []


def test_the_new_followers_toggle_silences_it_per_device(world):
    db = world["Session"]()
    row = db.query(PushSubscription).first()
    row.prefs = '{"follows": false}'
    db.commit()
    db.close()
    assert world["poll"](_contacts(NEW, "3" * 64, ME)) == []


def test_follows_are_one_of_the_shared_toggle_names():
    from pathlib import Path
    assert push_prefs.push_type({"kind": 3}) == "follows" and "follows" in push_prefs.PUSH_TYPES
    client = (Path(__file__).resolve().parents[1] / "static/js/client/app.js").read_text()
    assert "['follows','New followers']" in client
