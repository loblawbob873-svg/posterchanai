"""The outbox's publish path, against a REAL websocket relay rather than a mock.

Both behaviours here were found by measuring this node's own 24 upstream relays, not by reading code:
8 of them accept the connection, take the event and then refuse it forever, while the existing
circuit breaker — which only ever counted CONNECT failures — saw 24 healthy relays. Each refusing
relay cost a publish plus two 15s retries on every single event, which is what pinned the outbox's
retry pool at its 50-task cap (and, because the drain only spawns a retry while under that cap,
silently left most events with no retry at all).

The relays are stood up in-process with `websockets.serve` and speak real NIP-01/NIP-42 frames. A
mock of our own client would have proved nothing: the bug that matters here is what a relay actually
says back, and the trap in the fix is a message ORDERING one (the OK for our own AUTH event arriving
before the OK for the published event) that only exists on the wire.
"""

import asyncio
import json

import pytest

from app.services.nostr import relay as R


def _ev(eid="a" * 64):
    return {"id": eid, "pubkey": "b" * 64, "created_at": 1786118000,
            "kind": 1, "tags": [], "content": "hi", "sig": "c" * 128}


class Relay:
    """A scriptable relay. `behaviour` decides what it answers to an EVENT."""

    def __init__(self, behaviour):
        self.behaviour = behaviour
        self.events = []          # event ids it actually stored
        self.saw_auth = False
        self.port = None
        self._server = None

    async def _handle(self, ws):
        authed = False
        async for raw in ws:
            msg = json.loads(raw)
            if msg[0] == "AUTH":
                authed = True
                self.saw_auth = True
                # A relay OKs the AUTH event itself — with the AUTH EVENT's id, not the published
                # one. This is the frame that breaks a client which returns on the first OK it sees.
                await ws.send(json.dumps(["OK", msg[1]["id"], True, ""]))
                continue
            if msg[0] != "EVENT":
                continue
            ev = msg[1]
            b = self.behaviour
            if b == "auth":
                if not authed:
                    await ws.send(json.dumps(["AUTH", "challenge-123"]))
                    await ws.send(json.dumps(["OK", ev["id"], False, "auth-required: we need auth"]))
                    continue
                self.events.append(ev["id"])
                await ws.send(json.dumps(["OK", ev["id"], True, ""]))
            elif b == "auth_refuse":
                # Authenticates you happily, then refuses the event anyway — eden.nostr.land's real
                # behaviour (`restricted: Pay …` before AND after AUTH). The point of this one is the
                # ORDER: the `OK true` for our own AUTH event arrives before the `OK false` for the
                # published event, so a client that returns on the first OK reports success.
                if not authed:
                    await ws.send(json.dumps(["AUTH", "challenge-123"]))
                    await ws.send(json.dumps(["OK", ev["id"], False, "auth-required: we need auth"]))
                    continue
                await ws.send(json.dumps(["OK", ev["id"], False, "restricted: Pay for access."]))
            elif b == "accept":
                self.events.append(ev["id"])
                await ws.send(json.dumps(["OK", ev["id"], True, ""]))
            elif b == "duplicate":
                await ws.send(json.dumps(["OK", ev["id"], True, "duplicate: have this event"]))
            elif b == "pow":
                await ws.send(json.dumps(["OK", ev["id"], False, "pow: 28 bits needed. (12)"]))
            elif b == "blocked":
                await ws.send(json.dumps(["OK", ev["id"], False, "blocked: Country US not allowed"]))
            elif b == "restricted":
                await ws.send(json.dumps(["OK", ev["id"], False, "restricted: Pay for access."]))
            elif b == "invalid":
                await ws.send(json.dumps(["OK", ev["id"], False, "invalid: bad signature"]))
            elif b == "noisy":
                # NOTICEs before the real answer — a client must not mistake chatter for a verdict.
                await ws.send(json.dumps(["NOTICE", "hello"]))
                await ws.send(json.dumps(["NOTICE", "still here"]))
                self.events.append(ev["id"])
                await ws.send(json.dumps(["OK", ev["id"], True, ""]))

    async def __aenter__(self):
        import websockets
        self._server = await websockets.serve(self._handle, "127.0.0.1", 0)
        self.port = self._server.sockets[0].getsockname()[1]
        return self

    async def __aexit__(self, *a):
        self._server.close()
        await self._server.wait_closed()

    @property
    def url(self):
        return f"ws://127.0.0.1:{self.port}"


def _run(coro):
    return asyncio.run(coro)


def _clear(url):
    """The breaker is module-level state shared across tests."""
    R._relay_paused_until.pop(url, None)
    R._relay_refuse_until.pop(url, None)
    R._relay_fail.pop(url, None)
    R._relay_pause_streak.pop(url, None)
    R._relay_last_fail.pop(url, None)


@pytest.fixture(autouse=True)
def _test_env(monkeypatch):
    """Give the AUTH path a real, deterministic key without touching the node's keyfile.

    Also make the fake relay look REMOTE. It is served on 127.0.0.1, which `_is_local` (rightly)
    exempts from the breaker — our own relay must never be paused. Without this the refusal tests
    would pass for the wrong reason: nothing would be paused because nothing local ever is.
    `test_our_own_relay_is_never_paused` covers the exemption itself.
    """
    import hashlib
    monkeypatch.setattr(R, "_AUTH_SK", hashlib.sha256(b"test-operator").digest(), raising=False)
    monkeypatch.setattr(R, "_is_local", lambda url: False)
    yield


@pytest.mark.parametrize("behaviour,expected", [
    ("accept", True),
    ("duplicate", True),      # `OK true duplicate:` is success — the relay already has it
    ("noisy", True),
    ("pow", False),
    ("blocked", False),
    ("restricted", False),
    ("invalid", False),
])
def test_publish_reports_what_the_relay_actually_said(behaviour, expected):
    async def go():
        async with Relay(behaviour) as srv:
            _clear(srv.url)
            try:
                return await R._publish_one(srv.url, _ev(), direct=True)
            finally:
                _clear(srv.url)
    assert _run(go()) is expected


def test_auth_required_relay_accepts_once_we_sign_the_challenge():
    """auth.nostr1.com and relay.froth.zone measured exactly this: refuse, challenge, accept.

    The event is authored by a DIFFERENT pubkey than the one signing AUTH — which is the whole
    premise of a relay-side outbox, and which both real relays were verified to allow.
    """
    async def go():
        async with Relay("auth") as srv:
            _clear(srv.url)
            try:
                ok = await R._publish_one(srv.url, _ev(), direct=True)
                return ok, srv.saw_auth, list(srv.events), R._relay_refuses(srv.url)
            finally:
                _clear(srv.url)

    ok, saw_auth, stored, paused = _run(go())
    assert saw_auth, "never answered the NIP-42 challenge"
    assert ok is True, "authenticated, but the publish still reported failure"
    assert stored == ["a" * 64], (
        "the event must be RE-SENT after authenticating — a relay that challenged us has already "
        "refused the first copy, so authenticating alone stores nothing"
    )
    assert paused is False, "auth-required is a request, not a refusal — the relay must not be paused"


def test_the_auth_events_own_ok_is_not_mistaken_for_the_published_events():
    """The ordering trap, and the reason `OK` is matched on the event id.

    A relay OKs our kind-22242 AUTH event — a well-formed `OK true` for a DIFFERENT id — and that
    frame arrives BEFORE the verdict on the event we published. Taking the first OK therefore
    reports "delivered" for an event the relay went on to refuse, which is worse than the refusal:
    the outbox records an ack, never retries, and the delivery-rate panel counts it as a success.

    So this asserts the VERDICT, not what the relay stored. An earlier version of this test checked
    `srv.events` and passed with the id-match removed — the relay stores the event either way; only
    the answer we come back with differs.
    """
    async def go():
        async with Relay("auth_refuse") as srv:
            _clear(srv.url)
            try:
                ok = await R._publish_one(srv.url, _ev("d" * 64), direct=True)
                return ok, R._relay_refuses(srv.url)
            finally:
                _clear(srv.url)

    ok, paused = _run(go())
    assert ok is False, (
        "returned the OK for our own AUTH event — the published event was refused `restricted:`"
    )
    assert paused is True, "the refusal after authenticating must still trip the breaker"


@pytest.mark.parametrize("behaviour", ["pow", "blocked", "restricted"])
def test_a_permanent_refusal_pauses_the_relay(behaviour):
    """These cost two 15s retries per event, forever, and no retry can ever succeed."""
    async def go():
        async with Relay(behaviour) as srv:
            _clear(srv.url)
            try:
                await R._publish_one(srv.url, _ev(), direct=True)
                paused = R._relay_refuses(srv.url)
                # And a paused relay must be SKIPPED, not merely remembered — the skip is the saving.
                second = await R._publish_one(srv.url, _ev("e" * 64), direct=True)
                return paused, second, list(srv.events)
            finally:
                _clear(srv.url)

    paused, second, stored = _run(go())
    assert paused is True, f"a `{behaviour}` refusal must pause the relay"
    assert second is False
    assert stored == [], "a paused relay was dialled again"


@pytest.mark.parametrize("behaviour", ["pow", "blocked", "restricted"])
def test_reading_from_a_refusing_relay_does_not_clear_the_publish_ban(behaviour):
    """The bug that made the first version of this breaker useless.

    `_connect` calls `_note_relay_ok` on every successful connect, and that POPS
    `_relay_paused_until`. The firehose reads from these same upstreams continuously, so a publish
    ban kept there is wiped by the next read — within minutes, forever. Refusals therefore live on
    their own clock that only time clears.

    The same separation is what keeps READS working: `_relay_paused` also short-circuits `_sync`, and
    nos.lol refuses our writes while remaining one of the better read sources on the network.
    """
    async def go():
        async with Relay(behaviour) as srv:
            _clear(srv.url)
            try:
                await R._publish_one(srv.url, _ev(), direct=True)
                banned_before = R._relay_refuses(srv.url)
                # A read: exactly what the firehose/sync does to this relay all day.
                async with R._connect(srv.url, True):
                    pass
                return banned_before, R._relay_refuses(srv.url), R._relay_paused(srv.url)
            finally:
                _clear(srv.url)

    before, after, connect_paused = _run(go())
    assert before is True
    assert after is True, "a successful READ cleared the publish ban — the breaker never holds"
    assert connect_paused is False, (
        "a write refusal must not register as a CONNECT failure — that would stop syncing FROM a "
        "relay that is perfectly good to read"
    )


@pytest.mark.parametrize("behaviour", ["invalid"])
def test_an_event_specific_refusal_never_pauses_the_relay(behaviour):
    """`invalid:`/`error:` describe THE EVENT, not the relay's policy.

    Pausing on these would let one malformed event of ours blacklist a perfectly good relay for six
    hours — a far worse failure than the one being fixed, and the reason the permanent set is a
    short explicit allowlist rather than "any OK false".
    """
    async def go():
        async with Relay(behaviour) as srv:
            _clear(srv.url)
            try:
                await R._publish_one(srv.url, _ev(), direct=True)
                return R._relay_refuses(srv.url)
            finally:
                _clear(srv.url)

    assert _run(go()) is False


def test_our_own_relay_is_never_paused(monkeypatch):
    """The breaker must not be able to cut this node off from its OWN relay.

    A local relay refusing an event means a bug or a policy on OUR machine, and pausing publishes to
    it for six hours would stop the node storing its own users' writes — with a `restricted:` from a
    WoT check being the likely trigger. So the local exemption is asserted, not assumed.
    """
    monkeypatch.setattr(R, "_is_local", lambda url: True)

    async def go():
        async with Relay("restricted") as srv:
            _clear(srv.url)
            try:
                await R._publish_one(srv.url, _ev(), direct=True)
                return R._relay_refuses(srv.url)
            finally:
                _clear(srv.url)

    assert _run(go()) is False, "the breaker paused our own relay"


def test_the_classifier_is_case_and_whitespace_tolerant():
    """Real relays are inconsistent about both; the reason string is human-facing text."""
    assert R._refusal_is_permanent("pow: 28 bits needed. (12)")
    assert R._refusal_is_permanent("  BLOCKED: Country US not allowed")
    assert R._refusal_is_permanent("Restricted: Pay on https://nostr.land for access.")
    assert not R._refusal_is_permanent("auth-required: you must auth")
    assert not R._refusal_is_permanent("rate-limited: slow down")
    assert not R._refusal_is_permanent("invalid: bad signature")
    assert not R._refusal_is_permanent("error: could not connect to the database")
    assert not R._refusal_is_permanent("")
    assert not R._refusal_is_permanent("duplicate: have this event")
