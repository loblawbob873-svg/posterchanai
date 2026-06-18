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
import asyncio
import logging

import websockets

from app.services.nostr.relay import _conn_kw, _CONNECT_TIMEOUT

logger = logging.getLogger(__name__)


async def _run_one(relay_url: str, kinds: list, on_event, stop: asyncio.Event, direct: bool) -> None:
    """Maintain one persistent firehose subscription to `relay_url`, reconnecting forever."""
    backoff = 2
    while not stop.is_set():
        try:
            # Generous frame cap: long-form articles (kind 30023) can be large; too small a
            # cap would raise on a big event and drop the whole upstream connection.
            async with websockets.connect(relay_url, open_timeout=_CONNECT_TIMEOUT,
                                          max_size=4 * 1024 * 1024, **_conn_kw(direct)) as ws:
                sub = uuid.uuid4().hex[:16]
                # Small look-back on (re)connect so a brief drop doesn't lose events.
                flt = {"kinds": kinds, "since": int(time.time()) - 120}
                await ws.send(json.dumps(["REQ", sub, flt]))
                logger.info("[nostr-relay] firehose connected: %s", relay_url)
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
                        try:
                            await on_event(msg[2])
                        except Exception as e:
                            logger.debug("[nostr-relay] firehose on_event error: %s", e)
        except Exception as e:
            logger.debug("[nostr-relay] firehose %s dropped: %s", relay_url, e)
        if stop.is_set():
            break
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 60)  # exponential backoff on repeated failures


async def run_firehose(upstream, kinds: list, on_event, stop: asyncio.Event, direct: bool) -> None:
    """Run a persistent firehose subscription against every upstream relay until `stop`."""
    tasks = [asyncio.create_task(_run_one(u, kinds, on_event, stop, direct)) for u in upstream]
    logger.info("[nostr-relay] firehose started on %d upstream relays", len(tasks))
    try:
        await stop.wait()
    finally:
        for t in tasks:
            t.cancel()
