"""THE POLL ITSELF MUST OBEY THE TOGGLES, per device.

`test_push_prefs.py` covers the classifier and the gate as pure functions. This drives the real
`nostr_push_service._poll()` — the thing that actually decides whether a closed phone buzzes —
against a stub relay result and two stub devices, because the bug was never in the predicate: it
was that nothing consulted one.

The rule with two sides: a phone set to mentions-only must go quiet for a like, and the desktop
beside it must not, from the SAME event in the SAME poll. One preference list governing both
devices is the failure this split exists to prevent.
"""
import asyncio
import json
import types

import pytest

from app.services import nostr_push_service as nps


class _Sub:
    """Just enough PushSubscription for the poll: an identity, a transport and its preferences."""
    def __init__(self, pubkey, endpoint, prefs=None):
        self.id = endpoint
        self.pubkey = pubkey
        self.endpoint = endpoint
        self.transport = "webpush"
        self.device_id = None
        self.token_hash = None
        self.p256dh = "p"
        self.auth = "a"
        self.prefs = prefs


class _DB:
    def __init__(self, subs):
        self._subs = subs
    def query(self, _model):
        return self
    def all(self):
        return list(self._subs)
    def delete(self, row):
        self._subs = [s for s in self._subs if s is not row]
    def commit(self):
        pass
    def close(self):
        pass


PK = "a" * 64
AUTHOR = "b" * 64


def _run_poll(monkeypatch, subs, events):
    """Run one _poll() with a primed cursor and capture every (endpoint, payload) sent."""
    sent = []
    db = _DB(subs)
    monkeypatch.setattr("app.database.SessionLocal", lambda: db)
    monkeypatch.setattr(nps.relay, "query", lambda *a, **k: _async(events))
    monkeypatch.setattr(nps, "_name_for", lambda pk: _async("Alice"))
    monkeypatch.setattr(nps.push_service, "send",
                        lambda sub, payload: sent.append((sub.get("endpoint"), payload)) or True)
    monkeypatch.setattr(nps, "subscription_dict", lambda s: {"endpoint": s.endpoint})
    nps._cursor = 1                     # not the first poll, so it delivers rather than priming
    nps._seen.clear()
    asyncio.run(nps._poll())
    return sent


def _async(value):
    async def go():
        return value
    return go()


def _event(kind, eid="e" * 64, tags=None):
    return {"id": eid, "kind": kind, "pubkey": AUTHOR, "created_at": 100,
            "tags": (tags if tags is not None else [["p", PK]]), "content": "hi", "sig": ""}


def test_a_device_that_turned_likes_off_gets_no_like_and_its_desktop_still_does(monkeypatch):
    phone = _Sub(PK, "https://push.example/phone", json.dumps({"likes": False}))
    desktop = _Sub(PK, "https://push.example/desktop", json.dumps({"likes": True}))
    sent = _run_poll(monkeypatch, [phone, desktop], [_event(7)])
    assert [e for e, _ in sent] == ["https://push.example/desktop"], (
        "one device's preferences must not decide another's — that is the whole point of the split")


def test_the_type_travels_with_the_payload(monkeypatch):
    """The native side gets to see what kind of notification this is; without it the phone cannot
    channel or re-check anything, and a payload that omits it is indistinguishable from an older
    server on the receiving end."""
    sub = _Sub(PK, "https://push.example/x")
    sent = _run_poll(monkeypatch, [sub], [_event(7)])
    assert sent and sent[0][1]["type"] == "likes"


@pytest.mark.parametrize("kind,ntype", [(7, "likes"), (6, "reposts"), (9735, "zaps"),
                                        (1111, "replies")])
def test_each_type_can_be_silenced_on_its_own(monkeypatch, kind, ntype):
    off = _Sub(PK, "https://push.example/off", json.dumps({ntype: False}))
    on = _Sub(PK, "https://push.example/on", json.dumps({ntype: True}))
    sent = _run_poll(monkeypatch, [off, on], [_event(kind)])
    assert [e for e, _ in sent] == ["https://push.example/on"]


def test_silencing_one_type_does_not_silence_the_rest(monkeypatch):
    """The failure that would look like a fix: a gate that reads any preference object as "quiet"."""
    sub = _Sub(PK, "https://push.example/x", json.dumps({"likes": False}))
    sent = _run_poll(monkeypatch, [sub], [_event(6)])
    assert [p["type"] for _, p in sent] == ["reposts"]


@pytest.mark.parametrize("stored", [None, "", "garbage", "{}"])
def test_a_device_that_never_configured_anything_still_gets_everything(monkeypatch, stored):
    """The old behaviour is the default, deliberately. Shipping this must not silence a soul who
    never opened the tab."""
    sub = _Sub(PK, "https://push.example/x", stored)
    sent = _run_poll(monkeypatch, [sub], [_event(7)])
    assert len(sent) == 1
