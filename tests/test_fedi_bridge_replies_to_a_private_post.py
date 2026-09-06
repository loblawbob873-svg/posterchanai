"""A REPLY TO A FEDIVERSE-ONLY POST MUST STILL SAY WHAT IT REPLIES TO.

Reported as "User replying to my status and posterchan does not load the status", with the event:
mirrored reply 3a953bb2 from echo@stereophonic.space carried p-tags, an emoji tag, `fedibridge` and
`proxy` -- and NO `e` TAG AT ALL. Nothing on it said which post it answered, so no client could
thread it and the parent could never be shown.

The cause is a guard that is right everywhere else. `_deliver` confirms a mapped parent still exists
before linking to it, because a pruned or deleted parent would leave a DANGLING `e` tag -- a
reference that never loads, which is its own bug. It asks that question as `SELECT 1 FROM events`.

A Fediverse-only note is never in `events`. That is the entire point of the mode: it is signed,
cross-posted, and deliberately not published to any relay (`fedi_only_service.route`), living in
FediOnlyEvent and reaching its author through /api/pleroma/private-events. Traced on the real data:
in_reply_to_id BA788JrJ4vIvPjg8Lg maps to 2c487ad7, absent from `events`, present in FediOnlyEvent
tagged ["client-mode","fedi-only"].

So the reference is emitted for a live fedi-only parent. It resolves for the AUTHOR -- who is who
the reply is addressed to, whose pubkey is p-tagged on it, and the only person who can see that half
of the thread at all. A reference that resolves for the person the reply is for beats one that
dangles for everyone including them. A parent that is genuinely gone (or tombstoned) still gets no
tag, which is the case the guard exists for.
"""
import asyncio
import json
import uuid

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (Base, User, UserSetting, FediBridgeDelivered, FediBridgeAction,
                        FediBridgeSkipped, FediOnlyEvent, FediPuppet, FediReconcileState)
from app.services import fedi_nostr_bridge_service as mirror

INST = "https://fedi.test"
PARENT_EVENT = "2c" + "4" * 62
PARENT_NOTE = "BA788JrJ4vIvPjg8Lg"
AUTHOR_PK = "4b" + "5" * 62


@pytest.fixture
def db(monkeypatch):
    engine = create_engine("sqlite://", poolclass=StaticPool,
                           connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine, tables=[
        User.__table__, UserSetting.__table__, FediBridgeDelivered.__table__,
        FediBridgeAction.__table__, FediBridgeSkipped.__table__, FediOnlyEvent.__table__,
        FediPuppet.__table__, FediReconcileState.__table__])
    s = sessionmaker(bind=engine)()
    # The events table is the RELAY's, not this schema's -- the service reaches it with raw SQL, so
    # the stand-in here is an empty one, which is exactly the state a fedi-only parent leaves.
    s.execute(text("CREATE TABLE events (id TEXT PRIMARY KEY)"))
    s.add(FediBridgeDelivered(platform="pleroma", instance_url=INST, note_id=PARENT_NOTE,
                              note_uri=INST + "/objects/72a55fd8", author_acct=None,
                              nostr_event_id=PARENT_EVENT, nostr_pubkey=AUTHOR_PK))
    s.commit()
    yield s
    s.close()


def _published(monkeypatch):
    """Capture the event the mirror would publish, and skip everything that needs a network."""
    seen = {}

    async def publish(port, ev):
        seen["ev"] = ev
        return True, ""

    async def puppet(*a, **k):
        return {"pubkey_hex": "aa" * 32, "seckey": ("01" * 32), "actor_uri": "https://x/y",
                "acct": "echo@stereophonic.space", "nip05": "echo"}

    monkeypatch.setattr(mirror.ident, "publish", publish)
    monkeypatch.setattr(mirror.ident, "ensure_puppet", puppet)
    # The real signer needs a key pair and a relay; only the TAGS are under test here.
    monkeypatch.setattr(mirror.ident, "build_event",
                        lambda p, kind, content, tags=None, **k: {"id": "e" * 64, "kind": kind,
                                                                  "content": content,
                                                                  "tags": list(tags or [])})
    return seen


def _raw():
    return {"id": "BA7AB8NOQUQuZx9lse", "visibility": "public", "in_reply_to_id": PARENT_NOTE,
            "content": "<p>hello</p>", "created_at": "2026-09-05T00:00:00.000Z",
            "uri": "https://stereophonic.space/objects/b010f08b",
            "account": {"acct": "echo@stereophonic.space", "username": "echo",
                        "url": "https://stereophonic.space/users/echo"}}


def _tags_of(db, monkeypatch, *, parent_present, parent_deleted=False):
    if parent_present:
        db.add(FediOnlyEvent(id=PARENT_EVENT, user_id=1, created_at=1,
                             raw=json.dumps({"id": PARENT_EVENT, "kind": 1, "pubkey": AUTHOR_PK,
                                             "tags": [["client-mode", "fedi-only"]], "content": "x"}),
                             deleted=parent_deleted))
        db.commit()
    seen = _published(monkeypatch)
    raw = _raw()
    post = mirror._norm("pleroma", raw)
    asyncio.run(mirror._deliver(db, 3052, "pleroma", INST, "fedi.test", raw, post, backfill=False))
    ev = seen.get("ev")
    return [t for t in ((ev or {}).get("tags") or [])]


def test_the_reply_carries_an_e_tag_to_the_private_parent(db, monkeypatch):
    tags = _tags_of(db, monkeypatch, parent_present=True)
    e = [t for t in tags if t and t[0] == "e"]
    assert e, f"the mirrored reply has no parent reference at all: {tags}"
    assert e[0][1] == PARENT_EVENT, e
    assert e[0][-1] == "reply", f"NIP-10 marker missing: {e}"
    assert [t for t in tags if t and t[0] == "p" and t[1] == AUTHOR_PK], (
        f"the parent's author must be p-tagged so the reply reaches them: {tags}")


def test_a_parent_that_is_nowhere_still_gets_no_tag(db, monkeypatch):
    """The dangling-reference guard is intact: no relay row, no fedi-only row, no `e` tag."""
    tags = _tags_of(db, monkeypatch, parent_present=False)
    assert not [t for t in tags if t and t[0] == "e"], f"linked to a parent that does not exist: {tags}"


def test_a_deleted_private_parent_gets_no_tag(db, monkeypatch):
    """A fedi-only post the author retracted is gone; a reference to it would dangle like any other."""
    tags = _tags_of(db, monkeypatch, parent_present=True, parent_deleted=True)
    assert not [t for t in tags if t and t[0] == "e"], f"linked to a deleted parent: {tags}"
