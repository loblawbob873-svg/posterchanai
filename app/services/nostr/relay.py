"""Async Nostr relay client over websockets (NIP-01 client messages).

publish() best-effort fans an event out to every relay; query() opens a short
REQ subscription on each relay, merges + dedups events across all of them, and
returns when every relay sends EOSE or the timeout elapses. Bounded timeouts so
a dead relay can't stall a poll (mirrors the fedi bridge's defensive timeouts).
"""

import json
import time
import uuid
import asyncio
import contextlib
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

# Per-relay circuit breaker: after this many consecutive connect failures (Tor proxy AND direct both
# failed), stop querying/publishing that relay for _RELAY_PAUSE_SEC so a dead/blocked upstream doesn't
# slow every sync. A single successful connect clears the streak.
_RELAY_FAIL_THRESHOLD = 3
_RELAY_PAUSE_SEC = 600   # 10 minutes
_FAIL_DEBOUNCE = 30      # count at most ONE failure per relay per this many seconds (ignore bursts:
                         # a backfill pages many queries, so a brief blip shouldn't pause a relay)
_relay_fail: dict = {}            # relay -> failure count (spaced >= _FAIL_DEBOUNCE apart)
_relay_paused_until: dict = {}    # relay -> unix ts; skip the relay until then
_relay_last_fail: dict = {}       # relay -> unix ts of the last counted failure (debounce)


def _relay_paused(relay: str) -> bool:
    return time.time() < _relay_paused_until.get(relay, 0)


def _note_relay_ok(relay: str) -> None:
    """A successful connect clears the relay's failure streak / pause."""
    _relay_fail.pop(relay, None)
    _relay_paused_until.pop(relay, None)
    _relay_last_fail.pop(relay, None)


def _note_relay_fail(relay: str) -> None:
    """A connect attempt (Tor+direct) failed. Count at most one per _FAIL_DEBOUNCE so a burst of
    failing queries (e.g. a backfill's pages, or a transient blip) doesn't instantly trip the
    breaker — only PERSISTENT failure over time pauses the relay. Logged once, on the transition."""
    now = time.time()
    if now - _relay_last_fail.get(relay, 0) < _FAIL_DEBOUNCE:
        return   # within the debounce window — this burst already counted
    _relay_last_fail[relay] = now
    n = _relay_fail.get(relay, 0) + 1
    _relay_fail[relay] = n
    if n >= _RELAY_FAIL_THRESHOLD:
        _relay_paused_until[relay] = time.time() + _RELAY_PAUSE_SEC
        _relay_fail.pop(relay, None)
        logger.warning("[nostr] pausing sync with %s for %dm — %d failures over ~%ds (Tor+direct)",
                       relay, _RELAY_PAUSE_SEC // 60, n, _RELAY_FAIL_THRESHOLD * _FAIL_DEBOUNCE)


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
    """True if the relay URL points at this host or the LAN — these must NEVER be sent through the
    outbound (Tor/SOCKS) proxy, which can't reach loopback OR a private LAN address and rejects with
    502. Covers loopback, RFC-1918 private ranges, and *.lan, so pointing a node's upstream at another
    box on the LAN (e.g. ws://192.168.0.102:3052) connects DIRECTLY instead of failing through Tor."""
    try:
        host = (urlparse(relay).hostname or "").lower()
    except Exception:
        return False
    if host in ("localhost", "127.0.0.1", "::1", "0.0.0.0") or host.endswith(".lan"):
        return True
    import re as _re
    return bool(_re.match(r"^(?:10\.|192\.168\.|172\.(?:1[6-9]|2\d|3[01])\.)", host))


def _conn_kw(relay: str, direct: bool) -> dict:
    """Connection kwargs for websockets.connect. Loopback relays pass `proxy=None` to
    EXPLICITLY disable proxying — websockets otherwise reads HTTPS/ALL_PROXY from the env
    (the bot's Tor proxy) and tries to tunnel localhost through it (502 / handshake timeout).
    `direct=True` (the relay's own upstream) omits the kwarg; otherwise use the configured proxy."""
    if _is_local(relay):
        return {"proxy": None}
    return {} if direct else _proxy_kw()


@contextlib.asynccontextmanager
async def _connect(relay: str, direct: bool, **kw):
    """Open a relay websocket with PROXY-FIRST, FALL-BACK-TO-DIRECT resilience: try the configured
    built-in proxy (→ Tor) first, and if that connect fails, retry the SAME relay directly. So a
    flaky/down Tor proxy degrades to a direct connection instead of dropping federation entirely.
    Loopback relays never proxy; `direct=True` callers skip the proxy attempt. Extra kwargs (e.g.
    max_size for the firehose) pass through."""
    base = _conn_kw(relay, direct)
    try:
        try:
            ws = await websockets.connect(relay, open_timeout=_CONNECT_TIMEOUT, **base, **kw)
        except Exception as e:
            if base.get("proxy"):
                logger.warning("[nostr] proxy connect to %s failed (%s) — retrying direct", relay, e)
                ws = await websockets.connect(relay, open_timeout=_CONNECT_TIMEOUT, proxy=None, **kw)
            else:
                raise
    except Exception:
        if not _is_local(relay):
            _note_relay_fail(relay)   # both Tor + direct failed (never circuit-break the local relay)
        raise
    if not _is_local(relay):
        _note_relay_ok(relay)         # connected → clear the failure streak / pause
    try:
        yield ws
    finally:
        try:
            await ws.close()
        except Exception:
            pass


async def _publish_one(relay: str, event: dict, direct: bool = False) -> bool:
    if not _is_local(relay) and _relay_paused(relay):
        return False   # circuit breaker: relay paused after repeated connect failures
    try:
        async with _connect(relay, direct) as ws:
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
    if not _is_local(relay) and _relay_paused(relay):
        return   # circuit breaker: relay paused after repeated connect failures — skip silently
    sub_id = uuid.uuid4().hex[:16]
    try:
        async with _connect(relay, direct) as ws:
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
