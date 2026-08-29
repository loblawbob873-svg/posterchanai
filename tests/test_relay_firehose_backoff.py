"""A RELAY THAT ACCEPTS US AND THEN DROPS US MUST NOT BE HAMMERED.

Measured on this node before the fix: `wss://nostr.openhoofd.nl/` reconnected **102 times in ten
minutes** — one every six seconds — while every other upstream reconnected once or twice. The
backoff was reset the moment the socket OPENED, so an upstream that accepts and then drops the
stream (a rate limiter, a relay that dislikes the REQ, a proxy closing idle tunnels) reset the delay
to 2s on every attempt and the exponential backoff could never engage.

Why it matters beyond the wasted requests: each attempt replays a 120s look-back, and this process
is ALSO the local relay that every client and the app itself connect to. `files-index save HTTP 503`
with `timed out during opening handshake`, off the user's own screen, is what a storm in here looks
like from the outside.

This drives the real `_one_firehose` with a fake `_connect`, so it measures the loop's actual
decisions rather than re-describing them.
"""
import asyncio

from app.services.nostr_relay import firehose as fh



class _Flag:
    """A stop signal the loop can read without a running event loop behind it."""

    def __init__(self):
        self._set = False

    def set(self):
        self._set = True

    def is_set(self):
        return self._set

    async def wait(self):
        while not self._set:
            await asyncio.sleep(0)


def _drive(waiter, connect, clock, url, stop):
    """Run the REAL loop with a fake socket, a fake clock and no real waiting."""
    orig_wait, orig_mono, orig_connect = fh.asyncio.wait_for, fh.time.monotonic, fh._connect
    fh.asyncio.wait_for = waiter
    fh.time.monotonic = lambda: clock["t"]
    fh._connect = connect
    try:
        asyncio.run(fh._run_one(url, [1], lambda e: None, stop, True, extra=None,
                                     start_delay=0.0, label=""))
    finally:
        fh.asyncio.wait_for, fh.time.monotonic, fh._connect = orig_wait, orig_mono, orig_connect


class _Sock:
    """A socket that opens happily and dies after `alive` seconds of the caller waiting on it."""

    def __init__(self, alive):
        self.alive = alive
        self.sent = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def send(self, raw):
        self.sent.append(raw)

    async def recv(self):
        await asyncio.sleep(self.alive)
        raise ConnectionError("upstream closed the stream")

    async def ping(self):
        raise ConnectionError("gone")


def _harness(monkeypatch, alive, clock):
    """Fake connect + a clock the test drives, so no test waits in real time."""
    opens = []

    def connect(url, direct, max_size=0):
        opens.append(clock["t"])
        return _Sock(alive)

    async def wait_for(aw, timeout):
        # Every wait in the loop — the recv, the ping, the backoff sleep — advances the fake clock
        # by its own timeout and then times out. That is what makes the delays measurable.
        if asyncio.iscoroutine(aw):
            aw.close()
        clock["t"] += timeout
        clock["waits"].append(timeout)
        raise asyncio.TimeoutError()

    monkeypatch.setattr(fh, "_connect", connect)
    monkeypatch.setattr(fh.time, "monotonic", lambda: clock["t"])
    return opens


def test_an_upstream_that_drops_us_on_sight_is_backed_off():
    """The storm, reproduced: connect, dropped immediately, connect again. The delay between
    attempts must GROW."""
    clock = {"t": 0.0, "waits": []}
    stop = _Flag()
    attempts = []

    class _Dead:
        """ACCEPTS US, TAKES THE REQ, THEN DROPS THE STREAM — which is the whole point.

        The reset lives AFTER the REQ is sent (that is the line that logs "firehose connected"),
        and this node logged that 102 times in ten minutes for one upstream. A fake that fails
        before it would never reach the bug: it has to get all the way in and then die."""

        async def __aenter__(self):
            attempts.append(clock["t"])
            if len(attempts) >= 6:
                stop.set()
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, raw):
            return None                     # the REQ is accepted…

        async def recv(self):
            raise ConnectionError("upstream closed the stream")   # …and then it is gone

        async def ping(self):
            raise ConnectionError("gone")

    async def waiter(aw, timeout):
        if asyncio.iscoroutine(aw):
            aw.close()
        clock["t"] += timeout
        raise asyncio.TimeoutError()

    _drive(waiter, lambda url, direct, max_size=0: _Dead(), clock,
           "wss://hostile.example", stop)

    # Each gap is a fixed session cost (the 45s recv wait plus the 10s ping) plus the backoff, so
    # the SPREAD between the first gap and the last is the backoff growth with the constant removed.
    # With the bug every gap is the same, because the delay is reset to 2s on every attempt.
    gaps = [round(b - a, 2) for a, b in zip(attempts, attempts[1:])]
    assert len(gaps) >= 4, "the loop did not retry enough to measure a backoff: %r" % (attempts,)
    assert gaps[-1] - gaps[0] >= 30, (
        "the delay barely moved — this is the 102-reconnects-in-ten-minutes bug, where a relay "
        "that accepts the REQ and then drops the stream resets the backoff every time: %r"
        % (gaps,))


def test_a_stream_that_lasted_reconnects_immediately():
    """The other half, and the reason this cannot simply be a fixed long delay: an upstream that
    worked for an hour and then blipped must come straight back, not wait a minute."""
    clock = {"t": 0.0}
    stop = _Flag()
    attempts = []

    class _Good:
        async def __aenter__(self):
            attempts.append(clock["t"])
            if len(attempts) >= 4:
                stop.set()
            return self

        async def __aexit__(self, *a):
            return False

        async def send(self, raw):
            # A long, healthy session, then a drop.
            clock["t"] += 3600.0
            raise ConnectionError("blip")

    async def waiter(aw, timeout):
        if asyncio.iscoroutine(aw):
            aw.close()
        clock["t"] += timeout
        raise asyncio.TimeoutError()

    _drive(waiter, lambda url, direct, max_size=0: _Good(), clock,
           "wss://healthy.example", stop)

    # Each attempt costs 3600s of session; the RECONNECT delay is whatever is left over.
    gaps = [round(b - a - 3600.0, 2) for a, b in zip(attempts, attempts[1:])]
    assert gaps, "the loop never reconnected: %r" % (attempts,)
    assert max(gaps) <= 6.0, (
        "a healthy upstream that blipped was made to wait: %r" % (gaps,))
