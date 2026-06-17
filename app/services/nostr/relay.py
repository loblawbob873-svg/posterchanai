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

import websockets

logger = logging.getLogger(__name__)

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


async def _publish_one(relay: str, event: dict) -> bool:
    try:
        async with websockets.connect(relay, open_timeout=_CONNECT_TIMEOUT) as ws:
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


async def publish(relays, event: dict) -> int:
    """Publish an event to all relays. Returns how many accepted/received it."""
    relays = normalize_relays(relays)
    if not relays:
        return 0
    results = await asyncio.gather(
        *[asyncio.wait_for(_publish_one(r, event), timeout=_PUBLISH_TIMEOUT) for r in relays],
        return_exceptions=True,
    )
    return sum(1 for r in results if r is True)


async def _query_one(relay: str, filters: list, out: dict, timeout: float) -> None:
    sub_id = uuid.uuid4().hex[:16]
    try:
        async with websockets.connect(relay, open_timeout=_CONNECT_TIMEOUT) as ws:
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


async def query(relays, filters: list, timeout: float = _DEFAULT_QUERY_TIMEOUT) -> list[dict]:
    """Run a REQ with `filters` against all relays; return deduped events (newest-first)."""
    relays = normalize_relays(relays)
    if not relays:
        return []
    out: dict = {}
    await asyncio.gather(
        *[_query_one(r, filters, out, timeout) for r in relays],
        return_exceptions=True,
    )
    return sorted(out.values(), key=lambda e: e.get("created_at", 0), reverse=True)
