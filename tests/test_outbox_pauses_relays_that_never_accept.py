"""The outbox stops sending to an upstream relay that has accepted none of our last N events -- and never pauses
one that still accepts some.

Measured on server1 2026-10-19: five upstream relays gave up on EVERY event for hours (frens.nostr1.com 892 times in
3 h): unreachable from here or auth-only. The connect breaker never tripped (a direct-fallback connect that succeeds
resets it) and their answers were not one of the worded permanent refusals, so each event was sent, retried twice
and given up on, forever. Reads from such a relay are unaffected (the refusal clock is write-only).
"""
import asyncio

from app.services.nostr import relay as R
from app.services.nostr_relay import outbox as O

STREAK = getattr(O.Outbox, "GIVE_UP_STREAK", 20)
DEAD, FLAKY, GOOD = "wss://dead.example", "wss://flaky.example", "wss://good.example"


def test_a_relay_that_never_accepts_is_paused_and_one_that_sometimes_does_is_not(monkeypatch):
    for r in (DEAD, FLAKY, GOOD):
        R._relay_refuse_until.pop(r, None)
        R._relay_paused_until.pop(r, None)
    n = {"i": 0}

    async def publish_to(relays, ev, direct=False):
        n["i"] += 1
        ok = {r for r in relays if r == GOOD}
        if FLAKY in relays and n["i"] % 9 == 0:
            ok.add(FLAKY)                       # accepts now and then
        return ok
    monkeypatch.setattr(R, "publish_to", publish_to)
    ob = O.Outbox([DEAD, FLAKY, GOOD], min_interval=0, retries=2, retry_delay=0)

    async def go():
        for i in range(STREAK + 5):
            await ob._retry_misses({"id": "%064x" % i}, {DEAD, FLAKY})
    asyncio.run(go())
    try:
        assert R._relay_refuses(DEAD), "a relay that accepted nothing for %d events is still sent every event" \
            % (STREAK + 5)
        assert not R._relay_refuses(FLAKY), "a relay that still accepts some events must not be paused"
        assert DEAD not in R.publishable([DEAD, FLAKY, GOOD])
        assert not R._relay_paused(DEAD), "the pause is write-only: reads from it must continue"
    finally:
        for r in (DEAD, FLAKY, GOOD):
            R._relay_refuse_until.pop(r, None)
