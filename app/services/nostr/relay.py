"""Async Nostr relay client over websockets (NIP-01 client messages).

publish() best-effort fans an event out to every relay; query() opens a short
REQ subscription on each relay, merges + dedups events across all of them, and
returns when every relay sends EOSE or the timeout elapses. Bounded timeouts so
a dead relay can't stall a poll (mirrors the fedi bridge's defensive timeouts).
"""

import json
import uuid
import asyncio
import logging
from urllib.parse import urlparse

import websockets

logger = logging.getLogger(__name__)


def _proxy_kw() -> dict:
    """websockets connect kwargs to route relays through the built-in HTTP proxy (→ Tor),
    when one is configured (env for bots, settings for the app). Empty = direct."""
    try:
        from app.services.proxy_utils import get_outbound_proxy
        p = get_outbound_proxy()
        return {"proxy": p} if p else {}
    except Exception:
        return {}

_CONNECT_TIMEOUT = 8
_DEFAULT_QUERY_TIMEOUT = 12
_PUBLISH_TIMEOUT = 10


def normalize_relays(relays) -> list[str]:
    """Accept a list or a comma/newline-separated string; return clean wss/ws URLs."""
    if isinstance(relays, str):
        relays = relays.replace(",", "\n").split("\n")
    out = []
    for r in relays or []:
        r = (r or "").strip()
        if r and r.startswith(("ws://", "wss://")) and r not in out:
            out.append(r)
    return out


def _is_local(relay: str) -> bool:
    """True if the relay URL points at this host — a loopback connection must NEVER be sent
    through the outbound (Tor/SOCKS) proxy, which can't reach localhost (it rejects with 502).
    Lets bots point their relay list at ws://127.0.0.1:3052 (the relay binds IPv4-only, so 127.0.0.1 not localhost)."""
    try:
        host = urlparse(relay).hostname or ""
    except Exception:
        return False
    return host in ("localhost", "127.0.0.1", "::1", "0.0.0.0")


def _conn_kw(relay: str, direct: bool) -> dict:
    """Connection kwargs for websockets.connect. Loopback relays pass `proxy=None` to
    EXPLICITLY disable proxying — websockets otherwise reads HTTPS/ALL_PROXY from the env
    (the bot's Tor proxy) and tries to tunnel localhost through it (502 / handshake timeout).
    `direct=True` (the relay's own upstream) omits the kwarg; otherwise use the configured proxy."""
    if _is_local(relay):
        return {"proxy": None}
    return {} if direct else _proxy_kw()


async def _publish_one(relay: str, event: dict, direct: bool = False) -> bool:
    try:
        async with websockets.connect(relay, open_timeout=_CONNECT_TIMEOUT, **_conn_kw(relay, direct)) as ws:
            await ws.send(json.dumps(["EVENT", event]))
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=5)
                msg = json.loads(raw)
                if isinstance(msg, list) and msg and msg[0] == "OK":
                    return bool(msg[2]) if len(msg) > 2 else True
            except (asyncio.TimeoutError, json.JSONDecodeError):
                return True  # event was sent; some relays don't send OK promptly
            return True
    except Exception as e:
        logger.warning(f"[nostr] publish to {relay} failed: {e}")
        return False


async def publish_to(relays, event: dict, direct: bool = False) -> set:
    """Publish an event to all relays; return the SET of relay URLs that accepted/received it.

    Lets callers (e.g. the relay outbox) compute the misses and retry just those."""
    relays = normalize_relays(relays)
    if not relays:
        return set()
    results = await asyncio.gather(
        *[asyncio.wait_for(_publish_one(r, event, direct), timeout=_PUBLISH_TIMEOUT) for r in relays],
        return_exceptions=True,
    )
    return {r for r, ok in zip(relays, results) if ok is True}


async def publish(relays, event: dict, direct: bool = False) -> int:
    """Publish an event to all relays. Returns how many accepted/received it."""
    return len(await publish_to(relays, event, direct))


async def _query_one(relay: str, filters: list, out: dict, timeout: float, direct: bool = False) -> None:
    sub_id = uuid.uuid4().hex[:16]
    try:
        async with websockets.connect(relay, open_timeout=_CONNECT_TIMEOUT, **_conn_kw(relay, direct)) as ws:
            await ws.send(json.dumps(["REQ", sub_id] + filters))
            deadline = asyncio.get_event_loop().time() + timeout
            while True:
                remaining = deadline - asyncio.get_event_loop().time()
                if remaining <= 0:
                    break
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
                except asyncio.TimeoutError:
                    break
                try:
                    msg = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if not isinstance(msg, list) or not msg:
                    continue
                if msg[0] == "EVENT" and len(msg) >= 3 and msg[1] == sub_id:
                    ev = msg[2]
                    if isinstance(ev, dict) and ev.get("id"):
                        out[ev["id"]] = ev
                elif msg[0] == "EOSE" and len(msg) >= 2 and msg[1] == sub_id:
                    break
            try:
                await ws.send(json.dumps(["CLOSE", sub_id]))
            except Exception:
                pass
    except Exception as e:
        logger.warning(f"[nostr] query {relay} failed: {e}")


async def query(relays, filters: list, timeout: float = _DEFAULT_QUERY_TIMEOUT,
                direct: bool = False) -> list[dict]:
    """Run a REQ with `filters` against all relays; return deduped events (newest-first)."""
    relays = normalize_relays(relays)
    if not relays:
        return []
    out: dict = {}
    await asyncio.gather(
        *[_query_one(r, filters, out, timeout, direct) for r in relays],
        return_exceptions=True,
    )
    return sorted(out.values(), key=lambda e: e.get("created_at", 0), reverse=True)
