"""A calendar / addressbook delete the relay stops answering PART-WAY THROUGH still tells CalDAV to re-read.

DELETE /api/calendar/calendars/<id> and /api/contacts/books/<id> (strict) delete the collection's items one by one
and then its metadata. The relay refusing an item already invalidated the CalDAV disk cache for what did go
(DeleteRefused.deleted); the relay going unreachable mid-loop raised the bare connection error, the route answered
503, and the cache was never told -- so a phone kept being served events / cards that are no longer on the relay
until the node restarted.

Driven through the real routes and the real caldav_store.delete_calendar; only the relay socket is replaced, by
one that deletes the first document and then refuses the connection (what websockets.connect raises for a relay
that went away). A relay that is gone before anything was deleted invalidates nothing (nothing changed).
"""
import asyncio

import pytest
from fastapi import HTTPException

from app.services import caldav_store
from app.services.caldav import storage as caldav_storage


class _User:
    username = "alice"


def _wire(monkeypatch, *, deletable):
    """The relay holds three items in collection `c1`; it deletes `deletable` documents, then is unreachable."""
    state = {"deleted": [], "forgot": []}
    monkeypatch.setattr(caldav_store, "enabled", lambda: True)
    monkeypatch.setattr(caldav_store, "user_storage_seckey", lambda db, u: b"\x01" * 32)
    monkeypatch.setattr(caldav_store, "_port", lambda db: 1)

    async def list_docs(port, prefix, **kw):
        return {f"{prefix}{u}": {"uid": u} for u in ("a", "b", "c")}

    async def delete_doc(port, sk, d, **kw):
        if len(state["deleted"]) >= deletable:
            raise ConnectionRefusedError(111, "Connect call failed ('127.0.0.1', 1)")
        state["deleted"].append(d)
        return True
    monkeypatch.setattr(caldav_store.store, "list_docs", list_docs)
    monkeypatch.setattr(caldav_store.store, "delete_doc", delete_doc)
    monkeypatch.setattr(caldav_storage, "forget_user", lambda name: state["forgot"].append(name))
    return state


def _routes():
    from app.routers import calendar, contacts
    return [("calendar", calendar.delete_calendar), ("contacts", contacts.delete_book)]


@pytest.mark.parametrize("which", ["calendar", "contacts"])
@pytest.mark.parametrize("deletable", [1, 3])          # cut short among the items / at the collection itself
def test_a_delete_cut_short_by_the_relay_still_invalidates_caldav(monkeypatch, which, deletable):
    st = _wire(monkeypatch, deletable=deletable)
    route = dict(_routes())[which]
    with pytest.raises(HTTPException) as e:
        asyncio.run(route("c1", current_user=_User(), db=None))
    assert e.value.status_code == 503, e.value.detail
    assert len(st["deleted"]) == deletable
    assert st["forgot"] == ["alice"], ("%d documents gone from the relay and CalDAV was never told" % deletable)


@pytest.mark.parametrize("which", ["calendar", "contacts"])
def test_a_relay_gone_before_anything_was_deleted_invalidates_nothing(monkeypatch, which):
    st = _wire(monkeypatch, deletable=0)
    route = dict(_routes())[which]
    with pytest.raises(HTTPException) as e:
        asyncio.run(route("c1", current_user=_User(), db=None))
    assert e.value.status_code == 503
    assert st["deleted"] == [] and st["forgot"] == []
