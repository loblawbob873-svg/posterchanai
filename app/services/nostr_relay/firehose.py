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

# HOW LONG A SESSION HAS TO LAST BEFORE IT COUNTS AS ONE THAT WORKED. Longer than the 45s recv
# timeout below, so a stream that survives one keepalive cycle qualifies and one that is dropped
# on sight does not.
_STABLE_AFTER = 60.0

# Live per-stream state for the status file → Server Stats' relay panel: {(url, label): {...}}.
# Bumped on the receive path (one dict lookup + an int), so it costs nothing per event. It is
# process-local and rebuilt from scratch on every (re)spawn, which is why `_prune_status` runs at
# the top of run_firehose: after an upstream change the removed relays' entries would otherwise sit
# there forever reading "disconnected" and make the panel report streams that no longer exist.
_STATUS: dict = {}

# A REQ IS A MESSAGE WITH A SIZE LIMIT, AND THE DM INBOX'S FILTER OUTGREW IT.
#
# The targeted streams (DM inbox, DVM) filter on `#p` = every operator pubkey — which is three keys
# per registered user, so it GROWS with the user base. At 2,090 users that is 4,180 keys and a
# 284 KB REQ, and measured on 2026-09-11 against all 30 configured upstreams, **29 of them refused
# it**: thirteen killed the socket without a word, the rest said why —
#   `bad req: total filter items too large`   (strfry's maxFilterLimit)
#   `message too large (284303 > 262144)`     (a 256 KB frame cap)
# The same filter cut to 500 or 1,000 keys was accepted by 22/30, the other eight failing for
# unrelated reasons (auth-required, DNS, HTTP status) at EVERY size.
#
# Nothing said so. The subscription reported "firehose connected" on every reconnect, the WoT
# stream (which carries no `#p`) kept delivering ~36k events a day, and the only visible symptom
# was that the relay stopped receiving NIP-17 gift wraps entirely — no inbound DMs, no Concord room
# traffic from other relays — from 2026-09-09 10:54 onwards. The status file is where it showed:
# 0 events EVER on (DM inbox) and (DVM) against 67,165 on (WoT).
#
# So an oversized filter is SPLIT into several REQs on the SAME socket (relays allow many
# subscriptions per connection; strfry's default is 20). 500 is half the largest size measured good
# here and matches strfry's own default limit, which is the number a relay we have never probed is
# most likely to use.
_MAX_FILTER_ITEMS = 500

# Chunking cannot be unbounded: a connection has a subscription cap too. Past this the stream says
# so and subscribes to what fits — LOUDLY, because the alternative is the silence that hid this bug
# for two days. `_MAX_FILTER_ITEMS * _MAX_SUBS` = 10,000 keys, i.e. ~3,300 users at three keys each.
_MAX_SUBS = 20

# Filter fields whose length is a SET SIZE and must never be chunked — splitting `kinds` would ask
# each sub-REQ for a different kind, which is a different question, not a smaller one.
_NEVER_CHUNKED = frozenset({"kinds", "since", "until", "limit", "search"})


def _chunks(flt: dict) -> list:
    """Recursive half: bound every array field, no cap. Chunks the LONGEST oversized field and
    recurses, so a filter with two long lists is still split correctly."""
    long = [(len(v), k) for k, v in flt.items()
            if k not in _NEVER_CHUNKED and isinstance(v, (list, tuple))
            and len(v) > _MAX_FILTER_ITEMS]
    if not long:
        return [flt]
    _, key = max(long)
    vals = list(flt[key])
    out = []
    for i in range(0, len(vals), _MAX_FILTER_ITEMS):
        out.extend(_chunks({**flt, key: vals[i:i + _MAX_FILTER_ITEMS]}))
    return out


def _split_filter(flt: dict, label: str = "", url: str = "") -> list:
    """One filter in, one-or-more REQ-able filters out — every array field bounded by
    `_MAX_FILTER_ITEMS` and the whole set bounded by `_MAX_SUBS`. The cap is applied ONCE, here,
    rather than inside the recursion, so the warning is one line about the real filter and not one
    per level of a split nobody asked about."""
    out = _chunks(flt)
    if len(out) > _MAX_SUBS:
        big = max(((len(v), k) for k, v in flt.items()
                   if k not in _NEVER_CHUNKED and isinstance(v, (list, tuple))), default=(0, "?"))
        logger.warning("[nostr-relay] firehose %s%s: filter needs %d subscriptions (%s has %d "
                       "items); subscribing to the first %d — events for the rest will NOT arrive",
                       url, label, len(out), big[1], big[0], _MAX_SUBS)
        out = out[:_MAX_SUBS]
    return out


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
        opened = time.monotonic()
        try:
            # Generous frame cap: long-form articles (kind 30023) can be large; too small a
            # cap would raise on a big event and drop the whole upstream connection.
            async with _connect(relay_url, direct, max_size=4 * 1024 * 1024) as ws:
                # Small look-back on (re)connect so a brief drop doesn't lose events.
                flt = {"kinds": kinds, "since": int(time.time()) - 120}
                if extra:
                    flt.update(extra)
                # One REQ per chunk, all on THIS socket (see _split_filter). Ordinary filters split
                # into exactly one, so the common path is unchanged.
                subs, refused = set(), False
                for part in _split_filter(flt, label, relay_url):
                    sub = uuid.uuid4().hex[:16]
                    subs.add(sub)
                    await ws.send(json.dumps(["REQ", sub, part]))
                logger.info("[nostr-relay] firehose connected: %s%s", relay_url, label)
                _mark(relay_url, label, connected=True)
                # THE BACKOFF IS NOT RESET HERE, and that one line was a reconnect storm.
                #
                # Resetting on `connected` treats opening a socket as success, so an upstream that
                # ACCEPTS us and then drops the stream a second later — a rate limiter, a relay that
                # dislikes the REQ, a proxy closing idle tunnels — resets the delay to 2s on every
                # single attempt and the exponential backoff can never engage. Measured on this
                # node: `wss://nostr.openhoofd.nl/` reconnected 102 times in ten minutes, one every
                # six seconds, while every other upstream reconnected once or twice. Each attempt
                # replays a 120s look-back, and this process is also the local relay every client
                # and the app itself talks to — which is what "timed out during opening handshake"
                # on a files-index save actually was.
                #
                # Reset below instead, on a session that LASTED. Connecting is not succeeding.
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
                            and msg[0] == "EVENT" and msg[1] in subs):
                        _mark(relay_url, label, event=True)
                        try:
                            await on_event(msg[2])
                        except Exception as e:
                            logger.debug("[nostr-relay] firehose on_event error: %s", e)
                    elif isinstance(msg, list) and msg and msg[0] in ("CLOSED", "NOTICE"):
                        # A REFUSED SUBSCRIPTION LOOKED EXACTLY LIKE A QUIET ONE, and that is what
                        # cost two days of inbound DMs: the relay answered "total filter items too
                        # large", we never read the message, and the stream sat there logged as
                        # `firehose connected` delivering nothing. An upstream that bothers to say
                        # why is the cheapest diagnosis there is — say it once per stream per
                        # session (a relay that NOTICEs every event must not fill the journal).
                        if msg[0] == "NOTICE" or (len(msg) >= 2 and msg[1] in subs):
                            if not refused:
                                refused = True
                                logger.warning("[nostr-relay] firehose %s%s refused a "
                                               "subscription: %s", relay_url, label,
                                               str(msg[-1])[:200])
        except Exception as e:
            logger.debug("[nostr-relay] firehose %s dropped: %s", relay_url, e)
        finally:
            # Whatever ended the stream — a drop, a cancel on reload, or shutdown — it is no longer
            # connected. Marking here rather than only in the except branch is what stops a cancelled
            # task from leaving a permanently "connected" row behind after an upstream change.
            _mark(relay_url, label, connected=False)
        if stop.is_set():
            break
        # A SESSION THAT LASTED IS THE SUCCESS SIGNAL, not one that merely opened. Anything
        # shorter leaves the backoff climbing, so a hostile or broken upstream ends up retried once
        # a minute rather than ten times a minute, and a healthy one that blips still comes back
        # immediately.
        if time.monotonic() - opened >= _STABLE_AFTER:
            backoff = 2
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
