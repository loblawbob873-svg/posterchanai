"""Live firehose sync — the real-time path.

Instead of polling the WoT author set batch by batch (which lags by a full sweep cycle), we
keep ONE persistent subscription open per upstream relay for the recent firehose
(`REQ {kinds, since}` with no `until`, so it stays open and streams new events). Every event
is checked against the WoT with an O(1) membership test; **non-WoT events are dropped before
any verify or DB work**, so receiving the global stream is cheap. WoT events are verified,
stored, and fanned out to connected clients instantly.

The windowed sweep (ingest.py) remains for history backfill and to fill any gap from a
firehose disconnect; the firehose handles freshness.
"""

import json
import time
import uuid
import random
import asyncio
import logging

import websockets

from app.services.nostr.relay import _connect, _CONNECT_TIMEOUT

logger = logging.getLogger(__name__)

# Spread N upstreams' initial connects over this many seconds so their `since`-window replays don't
# all land at once and starve the local /client WS handshake (the cold-start CPU-peg). A live
# reload passes a shorter span (see thread._spawn_firehose) since only a reconnect, not a full boot.
_STAGGER_SPAN = 6.0

# Live per-stream state for the status file → Server Stats' relay panel: {(url, label): {...}}.
# Bumped on the receive path (one dict lookup + an int), so it costs nothing per event. It is
# process-local and rebuilt from scratch on every (re)spawn, which is why `_prune_status` runs at
# the top of run_firehose: after an upstream change the removed relays' entries would otherwise sit
# there forever reading "disconnected" and make the panel report streams that no longer exist.
_STATUS: dict = {}


def _mark(url: str, label: str, connected: bool = None, event: bool = False) -> None:
    st = _STATUS.setdefault((url, label), {"connected": False, "events": 0, "since": 0, "last": 0})
    if connected is not None and connected != st["connected"]:
        st["connected"] = connected
        st["since"] = int(time.time()) if connected else 0
    if event:
        st["events"] += 1
        st["last"] = int(time.time())


def _prune_status(label: str, relays: list) -> None:
    keep = set(relays)
    for key in [k for k in _STATUS if k[1] == label and k[0] not in keep]:
        _STATUS.pop(key, None)


def firehose_status() -> list:
    """One row per open stream, newest counts. Sorted so the panel's order is stable between polls."""
    return [{"relay": url, "label": (label or "").strip(), **st}
            for (url, label), st in sorted(_STATUS.items())]


async def _run_one(relay_url: str, kinds: list, on_event, stop: asyncio.Event, direct: bool,
                   extra: dict = None, start_delay: float = 0.0, label: str = "") -> None:
    """Maintain one persistent firehose subscription to `relay_url`, reconnecting forever. `extra`
    adds filter fields (e.g. {'#p': [operator pubkeys]} for the targeted DM inbox). `start_delay`
    staggers this stream's FIRST connect so N relays don't all replay their `since` window at the
    same instant — that synchronized burst pegs CPU and starves the local WS server's handshake at
    (re)start (symptom: '/client can't connect' for ~a minute after a relay restart)."""
    if start_delay:
        try:
            await asyncio.wait_for(stop.wait(), timeout=start_delay)
            return   # stopped during the stagger delay — never connected
        except asyncio.TimeoutError:
            pass
    backoff = 2
    while not stop.is_set():
        try:
            # Generous frame cap: long-form articles (kind 30023) can be large; too small a
            # cap would raise on a big event and drop the whole upstream connection.
            async with _connect(relay_url, direct, max_size=4 * 1024 * 1024) as ws:
                sub = uuid.uuid4().hex[:16]
                # Small look-back on (re)connect so a brief drop doesn't lose events.
                flt = {"kinds": kinds, "since": int(time.time()) - 120}
                if extra:
                    flt.update(extra)
                await ws.send(json.dumps(["REQ", sub, flt]))
                logger.info("[nostr-relay] firehose connected: %s%s", relay_url, label)
                _mark(relay_url, label, connected=True)
                backoff = 2
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=45)
                    except asyncio.TimeoutError:
                        try:
                            await asyncio.wait_for(ws.ping(), timeout=10)  # keepalive
                            continue
                        except Exception:
                            break
                    try:
                        msg = json.loads(raw)
                    except (ValueError, TypeError):
                        continue
                    if (isinstance(msg, list) and len(msg) >= 3
                            and msg[0] == "EVENT" and msg[1] == sub):
                        _mark(relay_url, label, event=True)
                        try:
                            await on_event(msg[2])
                        except Exception as e:
                            logger.debug("[nostr-relay] firehose on_event error: %s", e)
        except Exception as e:
            logger.debug("[nostr-relay] firehose %s dropped: %s", relay_url, e)
        finally:
            # Whatever ended the stream — a drop, a cancel on reload, or shutdown — it is no longer
            # connected. Marking here rather than only in the except branch is what stops a cancelled
            # task from leaving a permanently "connected" row behind after an upstream change.
            _mark(relay_url, label, connected=False)
        if stop.is_set():
            break
        # Jittered backoff: a network/proxy blip drops every upstream at once, and without jitter
        # they'd all reconnect in lockstep and replay their look-back windows together — re-pegging
        # CPU and starving local /client handshakes (the same symptom the startup stagger targets,
        # but on every mass reconnect). The jitter desynchronises the reconnect storm.
        try:
            await asyncio.wait_for(stop.wait(), timeout=backoff + random.uniform(0, backoff))
            break   # stop signalled while backing off
        except asyncio.TimeoutError:
            pass
        backoff = min(backoff * 2, 60)  # exponential backoff on repeated failures


async def run_firehose(upstream, kinds: list, on_event, stop: asyncio.Event, direct: bool,
                       max_relays: int = 0, extra: dict = None, stagger_span: float = _STAGGER_SPAN,
                       label: str = "") -> None:
    """Run a persistent firehose subscription against the upstream relays until `stop`.

    `max_relays` caps how many relays to subscribe to (0 = ALL). The firehose is now the sole
    real-time ingestion path (the windowed sync sweep is off by default), so by default we
    stream from EVERY upstream — a WoT post that only lands on a less-popular relay would
    otherwise be missed. The global stream is redundant (popular notes arrive on every relay),
    but non-WoT events are dropped before any verify/DB work and WoT events are has_event-
    deduped, so the extra cost is just parsing each stream. Lower max_relays to trade
    completeness for idle CPU if a node is constrained."""
    relays = list(upstream)[:max_relays] if max_relays and max_relays > 0 else list(upstream)
    _prune_status(label, relays)   # drop rows for relays this group no longer streams from
    # Stagger each upstream's first connect so the initial `since`-window replays don't all land at
    # once — keeps the event loop responsive to local /client handshakes during (re)start instead of
    # CPU-pegged on backfill. Spread the fleet over `stagger_span` seconds total.
    step = (stagger_span / len(relays)) if relays else 0.0
    tasks = [asyncio.create_task(_run_one(u, kinds, on_event, stop, direct, extra, start_delay=i * step, label=label))
             for i, u in enumerate(relays)]
    logger.info("[nostr-relay] firehose started on %d/%d upstream relays%s",
                len(tasks), len(upstream), label or "")
    try:
        await stop.wait()
    finally:
        for t in tasks:
            t.cancel()
        # Await the cancelled children so their websockets are actually torn down before this
        # coroutine returns — a live reload gathers on run_firehose, so without this the old
        # connections could linger and double-subscribe alongside the freshly-spawned group.
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
