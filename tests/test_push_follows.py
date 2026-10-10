"""A NEW FOLLOWER BUZZES THE PHONE -- once, and only a new one.

"yes add push notifications for follows too". A follow is a kind-3 contact list that p-tags you, and a
contact list is republished every time its owner follows or unfollows ANYONE -- so pushing on every
kind-3 would re-announce every existing follower all day (which is why the open app never toasts
them). The watcher remembers who already follows whom (push_follow_seen): the first sighting of a
person seeds their existing followers silently, a follower it has never seen pushes "X followed you"
once, and a re-saved list pushes nothing. Drives the real `_poll()`; the events the poll reads are stubs,
while the devices and the seen-ledger are the real push_store documents on a real relay (#161).
"""
import asyncio

import pytest

from app.services import nostr_push_service as nps
from app.services import push_prefs, push_store
from tests import push_relay_harness as H
from tests.client_source import app_source_with

ME = "a" * 64
OLD = "b" * 64          # already followed ME before any of this
NEW = "c" * 64          # follows ME for the first time


def _contacts(author, eid, *pks):
    return {"id": eid, "kind": 3, "pubkey": author, "created_at": 100, "content": "",
            "tags": [["p", pk] for pk in pks], "sig": ""}


@pytest.fixture
def world(tmp_path, monkeypatch):
    r = H.start(tmp_path, monkeypatch)
    monkeypatch.setattr(nps, "_seen", set())
    H.add_sub(pubkey=ME, endpoint="https://push.example/phone", transport="webpush", p256dh="p", auth="a")
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
    monkeypatch.setattr(nps, "subscription_dict", lambda s: {"endpoint": s["endpoint"]})

    def poll(*events):
        poll_events[:] = events
        nps._cursor = 1
        asyncio.run(nps._poll())
        return sent
    yield {"poll": poll, "sent": sent, "relay_lists": relay_lists}
    H.reset()
    r.close()


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
    [row] = H.subs()
    row["prefs"] = '{"follows": false}'
    asyncio.run(push_store.put_sub(row))
    assert world["poll"](_contacts(NEW, "3" * 64, ME)) == []


def test_the_ledger_survives_a_restart(world):
    """A fresh process (the worker restarted) must not re-announce a follower it already announced."""
    assert len(world["poll"](_contacts(NEW, "3" * 64, ME))) == 1
    world["sent"].clear()
    H.reset()                                                  # a cold process view, read from the relay
    nps._seen.clear()
    assert world["poll"](_contacts(NEW, "5" * 64, ME, "e" * 64)) == []
    assert push_store.follows().get(ME)["seeded"] is True


def test_an_unreadable_ledger_announces_nothing(world, monkeypatch):
    """Unlike the own-sent-DM dedup, "unknown" here is NOT "send": a contact list is re-saved on every
    follow of anybody, so failing open would announce old followers as new. This was the SQL version's
    behaviour on a database error, kept."""
    from app.services.relay_reader import Unavailable

    async def down(*_a, **_k):
        raise Unavailable("relay down")
    monkeypatch.setattr(push_store, "follow_seeded", down)
    assert asyncio.run(nps._is_new_follower(ME, NEW)) is False


def test_follows_are_one_of_the_shared_toggle_names():
    assert push_prefs.push_type({"kind": 3}) == "follows" and "follows" in push_prefs.PUSH_TYPES
    client = app_source_with("blossom.js")   # _NOTIFICATION_TYPES lives in blossom.js
    assert "['follows','New followers']" in client
