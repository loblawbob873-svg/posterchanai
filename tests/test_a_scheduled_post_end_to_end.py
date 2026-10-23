"""A SCHEDULED POST, END TO END ON THE SERVER: THE DOOR THE CLIENT KNOCKS ON, THE CLOCK, AND THE PUBLISHER.

tests/test_scheduled_posts_endpoints.py drives the three routes; tests/test_a_scheduled_post_goes_out_once.py
drives the publisher on rows it typed itself. Nothing handed the publisher a row the ROUTE stored from a
really signed note, or moved a clock through the moment a post is due. Here, with the schedules in the
`scheduled_posts` DocTable on the shipped relay (tests/app_tables_harness.py) and only the clock and the
outbound publish replaced:

  * a note signed for "in ten minutes" is stored by POST /client/scheduled, and another device (a fresh
    list call with its own proof) sees it, pending, at that time;
  * a poll a minute BEFORE it is due publishes nothing; a poll just after publishes EXACTLY the signed
    event (same id and signature) once; the next poll publishes nothing; the list stops showing it;
  * a cancel through the route before it is due means it never goes out, and cancelling a post that has
    already gone answers 200 "already sending or sent" -- the answer the client reads as "too late", which
    is different from the 503 it reads as "could not ask; still scheduled".
"""
import time
from datetime import datetime

import pytest

from app.routers import client
from app.services import scheduled_posts_service as sched
from tests.app_tables_harness import tables, shared_relay, fresh_process_view  # noqa: F401
from tests.test_scheduled_posts_endpoints import PK_A, SK_A, _accounts, body, create, note, proof, run


class Clock:
    """`datetime.utcnow()` as the scheduler sees it -- moved by the test, never slept through."""

    def __init__(self, monkeypatch):
        self.now = datetime.utcnow()
        clock = self

        class _DT(datetime):
            @classmethod
            def utcnow(cls):
                return clock.now
        monkeypatch.setattr(sched, "datetime", _DT)

    def at(self, unix):
        self.now = datetime.utcfromtimestamp(unix)


@pytest.fixture
def world(tables, monkeypatch):
    db = _accounts()
    published = []

    async def publish_event(port, event):
        published.append(event)
        return True, ""
    monkeypatch.setattr(sched.store, "publish_event", publish_event)
    monkeypatch.setattr(sched._ss, "_port", lambda: 3052)
    yield db, published, Clock(monkeypatch)
    db.close()


def listing(db):
    code, j = body(run(client.scheduled_list(client.ScheduledAuthReq(pubkey=PK_A, auth=proof(SK_A)), db=db)))
    assert code == 200, j
    return j["posts"]


def cancel(db, rid):
    return body(run(client.scheduled_cancel(client.ScheduledCancelReq(pubkey=PK_A, auth=proof(SK_A), id=rid), db=db)))


def poll():
    run(sched._publish_due_once())


def test_a_post_scheduled_through_the_route_goes_out_at_its_time_exactly_once(world):
    db, published, clock = world
    when = int(time.time()) + 600
    ev = note(SK_A, when, content="good morning, ten minutes from now")
    code, j = create(db, SK_A, PK_A, ev, when)
    assert code == 200 and j["ok"], j
    # Another device asks: it is there, pending, at that time, with its text.
    posts = listing(db)
    assert [(p["id"], p["status"], p["scheduled_at"]) for p in posts] == [(j["id"], "pending", when)], posts
    assert "good morning" in posts[0]["preview"]
    clock.at(when - 60)
    poll()
    assert published == [], "a post went out a minute early"
    clock.at(when + 5)
    poll()
    assert len(published) == 1, published
    assert published[0] == ev, "what went out is not the note the author signed"
    poll()
    clock.at(when + 120)
    poll()
    assert len(published) == 1, "a scheduled post was published twice"
    assert listing(db) == [], "a post that went out is still listed as scheduled"
    # Cancelling it now is answered -- "already sending or sent", not "could not ask".
    code, j2 = cancel(db, j["id"])
    assert code == 200 and not j2["ok"] and j2["error"] == "already sending or sent", (code, j2)


def test_a_post_cancelled_through_the_route_never_goes_out(world):
    db, published, clock = world
    when = int(time.time()) + 900
    keep = note(SK_A, when, content="this one stays")
    drop = note(SK_A, when + 1, content="this one is cancelled")
    rk = create(db, SK_A, PK_A, keep, when)[1]["id"]
    rd = create(db, SK_A, PK_A, drop, when + 1)[1]["id"]
    assert [p["id"] for p in listing(db)] == [rk, rd]
    code, j = cancel(db, rd)
    assert code == 200 and j["ok"], j
    assert [p["id"] for p in listing(db)] == [rk], "a cancelled post is still listed"
    clock.at(when + 60)
    poll()
    poll()
    assert published == [keep], ("only the post that was not cancelled goes out, once", published)
    assert fresh_process_view("scheduled_posts").get(str(rd))["status"] == "cancelled"
