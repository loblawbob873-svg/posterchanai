"""A relay that refuses Tor is not retried through Tor on every connect.

Measured on server1: asia.vectorapp.io was tried via the Tor proxy 174 times an hour, timed out after the
full 8s each time, then connected direct. Drives the shipped _connect against a fake websockets.connect.
"""
import asyncio

import pytest

from app.services.nostr import relay as R


class _WS:
    async def close(self):
        pass


@pytest.fixture
def net(monkeypatch):
    calls = []
    state = {"direct_ok": True}

    async def connect(url, open_timeout=None, proxy="ENV", **kw):
        calls.append("tor" if proxy else "direct")
        if proxy:
            raise TimeoutError("timed out during opening handshake")
        if not state["direct_ok"]:
            raise OSError("unreachable")
        return _WS()
    monkeypatch.setattr(R.websockets, "connect", connect)
    monkeypatch.setattr(R, "_proxy_kw", lambda: {"proxy": "http://127.0.0.1:8118"})
    monkeypatch.setattr(R, "_tor_refused", {})
    for name in ("_note_relay_ok", "_note_relay_fail", "_note_relay_429"):
        monkeypatch.setattr(R, name, lambda *a: None)
    return calls, state


async def _open(relay):
    async with R._connect(relay, False):
        pass


def test_after_one_proven_refusal_the_relay_goes_direct(net):
    calls, _ = net
    for _ in range(3):
        asyncio.run(_open("wss://asia.example/nostr"))
    assert calls == ["tor", "direct", "direct", "direct"], calls


def test_a_relay_that_fails_both_ways_is_not_remembered(net):
    calls, state = net
    state["direct_ok"] = False
    with pytest.raises(OSError):
        asyncio.run(_open("wss://down.example"))
    assert not R._tor_refuses("wss://down.example")


def test_the_memory_expires(net, monkeypatch):
    calls, _ = net
    asyncio.run(_open("wss://asia.example/nostr"))
    t = R.time.time() + R._TOR_REFUSED_TTL + 1
    monkeypatch.setattr(R.time, "time", lambda: t)
    calls.clear()
    asyncio.run(_open("wss://asia.example/nostr"))
    assert calls[0] == "tor", "an expired refusal still skipped Tor"
