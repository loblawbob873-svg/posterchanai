"""The relay's two keepalives fire TOGETHER, so a phone's radio wakes once per period instead of on two schedules.

Battery review 2026-10-15 ("align the wakeups"). Measured through Cloudflare on wss://relay.poster.place: a PING frame
every 30s (websockets) and a NOTICE every 40s on its own timer -- wake-ups at 30, 40, 60, 80, 90, 120s ... for a
phone's background signer socket. Both now run on ONE period (RelayServer.KEEPALIVE_S = 75s, the push socket's
ws_ping_interval too), the NOTICE at whole multiples from the connection's start -- the instant the ping timer starts.
"""
import asyncio
import re
from pathlib import Path

from app.services.nostr_relay.server import RelayServer

ROOT = Path(__file__).resolve().parents[1]


class _Conn:
    pass


def _times(period, grace, signer_only, used, n=3):
    sent = []

    class Fake:
        KEEPALIVE_S = period
        SIGNER_GRACE = grace

        def _send(self, conn, msg):
            sent.append((asyncio.get_running_loop().time(), msg))

    async def go():
        conn = _Conn()
        conn._pcai_signer_only = signer_only
        conn._pcai_signer_used = used
        t0 = asyncio.get_running_loop().time()
        task = asyncio.create_task(RelayServer._keepalive(Fake(), conn))
        deadline = t0 + grace + period * (n + 2)          # bounded: a keepalive on its own timer must FAIL, not hang
        while len(sent) < n and asyncio.get_running_loop().time() < deadline:
            await asyncio.sleep(0.01)
        task.cancel()
        assert len(sent) >= n, ('the keepalive does not follow KEEPALIVE_S', sent)
        return [(t - t0, m) for t, m in sent]
    return asyncio.run(go())


def test_the_notice_lands_on_whole_periods_from_the_connections_start():
    got = _times(0.2, 0.05, False, False)
    for k, (t, msg) in enumerate(got, 1):
        assert msg == ["NOTICE", "keepalive"]
        assert abs(t - 0.2 * k) < 0.05, ('the NOTICE drifted off the shared clock', got)


def test_a_signer_socket_after_its_grace_is_still_on_the_same_clock():
    # The grace (here 0.05 of a 0.2 period) used to shift every later NOTICE by itself: 30 + 40k.
    got = _times(0.2, 0.05, True, True)
    for k, (t, _m) in enumerate(got, 1):
        assert abs(t - 0.2 * k) < 0.05, ('the signer grace shifted the keepalive off the ping clock', got)


def test_the_relay_ping_the_notice_and_the_push_ping_share_one_period_inside_the_limits():
    thread = (ROOT / 'app/services/nostr_relay/thread.py').read_text()
    assert 'ping_interval=RelayServer.KEEPALIVE_S' in thread, 'the relay ping frame is on its own clock again'
    run = (ROOT / 'run.py').read_text()
    push = int(re.search(r'ws_ping_interval=(\d+)', run).group(1))
    assert RelayServer.KEEPALIVE_S == push, ('relay keepalive and push ping differ', RelayServer.KEEPALIVE_S, push)
    # Cloudflare closes an idle WebSocket at 100s; the native signer calls a socket stale at 90s.
    assert RelayServer.KEEPALIVE_S < 90
