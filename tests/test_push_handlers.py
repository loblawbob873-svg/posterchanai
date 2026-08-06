"""The two push handlers, driven directly.

Both take a pubkey straight out of an untrusted event's `p` tags and use it to look up devices and to
stamp a rate limiter, so the interesting cases are hostile ones, and none of them are reachable from
the transport tests. What is covered here:

* the subscriber intersection, which is a security boundary — without it, an event carrying arbitrary
  p tags caused a DB query per tag AND parked a cooldown stamp on strangers, silently suppressing
  their real notifications for as long as the attacker kept it up;
* `payload["type"]`, which sw.js branches on to decide whether a notification may be suppressed while
  the app is focused. A typo here is invisible server-side and stops phones ringing;
* the cooldowns, which are what stop a call's frame burst — or a chatty sender — from becoming a
  buzz per event.
"""
import asyncio

import pytest

from app.services import nostr_push_service as nps


@pytest.fixture(autouse=True)
def _isolate(monkeypatch):
    """Freeze the module's global state and stub everything that would touch the DB, the relay or a
    real push service, so these run offline and cannot leak state between tests."""
    sent = []
    monkeypatch.setattr(nps, "_dm_recent", {})
    monkeypatch.setattr(nps, "_call_recent", {})
    monkeypatch.setattr(nps, "_sub_pks", set())
    monkeypatch.setattr(nps, "_sub_pks_at", 0.0)
    monkeypatch.setattr(nps.push_service, "send", lambda sub, payload: sent.append((sub, payload)) or True)
    monkeypatch.setattr(nps, "_name_for", _async(""))
    return sent


def _async(value):
    async def f(*a, **k):
        return value
    return f


def _subscribed(monkeypatch, *pks):
    """Pretend exactly `pks` have registered a device."""
    monkeypatch.setattr(nps, "_subscriber_pks", _async(set(pks)))
    monkeypatch.setattr(nps, "_subs_for",
                        lambda want: {pk: [{"endpoint": f"https://push/{pk}", "keys": {}}]
                                      for pk in want if pk in set(pks)})


ME, PEER, STRANGER = "a" * 64, "b" * 64, "c" * 64


def _wrap(*ptags, author="e" * 64, kind=1059):
    return {"id": "x" * 64, "kind": kind, "pubkey": author, "created_at": 0,
            "tags": [["p", p] for p in ptags], "content": "", "sig": ""}


def test_dm_pushes_the_recipient(monkeypatch, _isolate):
    _subscribed(monkeypatch, ME)
    asyncio.run(nps._dm_handler(_wrap(ME)))
    assert len(_isolate) == 1
    _, payload = _isolate[0]
    # sw.js keys its focus-suppression off this exact string; if it drifts, your own outgoing DM
    # starts notifying the tab you typed it in.
    assert payload["type"] == "dm"
    assert "message" in payload["body"].lower()


def test_dm_ignores_a_pubkey_with_no_device(monkeypatch, _isolate):
    _subscribed(monkeypatch, ME)
    asyncio.run(nps._dm_handler(_wrap(STRANGER)))
    assert _isolate == []


def test_a_stranger_cannot_park_a_cooldown_on_a_subscriber(monkeypatch, _isolate):
    """The attack the subscriber intersection closes: p-tag a victim from an unrelated event and, if
    the stamp happened before the membership check, their next REAL message would be suppressed."""
    _subscribed(monkeypatch, ME)
    asyncio.run(nps._dm_handler(_wrap(STRANGER)))          # nothing to do with ME...
    assert nps._dm_recent == {}, "cooldown stamped for a non-subscriber"
    asyncio.run(nps._dm_handler(_wrap(ME)))                # ...so ME's real DM still gets through
    assert len(_isolate) == 1


def test_dm_cooldown_collapses_a_burst(monkeypatch, _isolate):
    _subscribed(monkeypatch, ME)
    for _ in range(5):
        asyncio.run(nps._dm_handler(_wrap(ME)))
    assert len(_isolate) == 1, "a burst should be one buzz, not five"


def test_dm_does_not_push_the_author_back_to_themselves(monkeypatch, _isolate):
    """A kind-4 note-to-self is p-tagged to its own author; notifying them about their own message
    is noise. (The NIP-17 self-copy is authored by a throwaway key and is NOT excludable here —
    sw.js suppresses that one while a window is focused.)"""
    _subscribed(monkeypatch, ME)
    asyncio.run(nps._dm_handler(_wrap(ME, author=ME, kind=4)))
    assert _isolate == []


def test_call_pushes_only_for_an_invite(monkeypatch, _isolate):
    _subscribed(monkeypatch, ME)
    ring = _wrap(ME, kind=25050)
    ring["tags"].append(["t", "invite"])
    asyncio.run(nps._call_handler(ring))
    assert len(_isolate) == 1 and _isolate[0][1]["type"] == "call"

    hangup = _wrap(ME, kind=25050)
    hangup["tags"].append(["t", "sig"])
    asyncio.run(nps._call_handler(hangup))
    assert len(_isolate) == 1, "a non-invite frame pushed — this is the phantom second ring"


def test_handlers_survive_a_hostile_event(monkeypatch, _isolate):
    """These run on a live relay subscription: one malformed event must not kill the watcher and
    silence calls and messages for everyone on the node."""
    _subscribed(monkeypatch, ME)
    for ev in ({}, {"tags": None}, {"tags": [["p"]]}, {"tags": "nope"}, {"tags": [[]], "pubkey": None}):
        for handler in (nps._dm_handler, nps._call_handler):
            try:
                asyncio.run(handler(dict(ev)))
            except Exception as e:                                  # noqa: BLE001
                pytest.fail(f"{handler.__name__} raised on {ev!r}: {e}")
