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
# subscribe(live_only=True): how long to wait for EOSE before handing events on regardless. A relay
# that never EOSEs would otherwise gate us forever, and silence is the one failure nobody notices.
_EOSE_FALLBACK = 15

# Per-relay circuit breaker: after this many consecutive connect failures (Tor proxy AND direct both
# failed), stop querying/publishing that relay for _RELAY_PAUSE_SEC so a dead/blocked upstream doesn't
# slow every sync. A single successful connect clears the streak.
_RELAY_FAIL_THRESHOLD = 3
_RELAY_PAUSE_SEC = 600   # base pause: 10 minutes
_RELAY_PAUSE_MAX = 14400   # cap the escalating pause at 4h — a persistently dead/paid relay (e.g. a
                           # relay that always rejects our writes) is then probed ~6x/day, not ~144x/day,
                           # cutting the wasted Tor+direct dial churn that dominates federation CPU.
_RELAY_429_PAUSE_SEC = 900   # a 429 is an EXPLICIT rate-limit — pause 15m immediately (no ramp-up)
_FAIL_DEBOUNCE = 30      # count at most ONE failure per relay per this many seconds (ignore bursts:
                         # a backfill pages many queries, so a brief blip shouldn't pause a relay)

# A relay can ACCEPT the connection, take the event, and answer `OK false <reason>` — healthy by every
# connect-level measure while never storing a single thing we send. The breaker above never saw those,
# so each such relay cost a full publish + two 15s retries on EVERY event, forever. Measured on this
# node's own 24 upstreams: 8 of them refuse everything, and the outbox's retry pool sat pinned at its
# 50-task cap because of it — which silently meant most events got no retry at all.
#
# NIP-01 gives these reasons a machine-readable prefix, and the split that matters is whether a RETRY
# could ever succeed:
#   pow:        we do not mine (28 bits ≈ 7 minutes of CPU per note) — never
#   blocked:    geo-block / pubkey not allowed to publish here — never, not by retrying
#   restricted: paid or whitelist-only relay — never, not by retrying
# Deliberately NOT here:
#   auth-required: we answer it now (NIP-42) — pausing would give up on a relay that works
#   rate-limited:  temporary by definition; the 429 path already handles the transport-level case
#   invalid:/error: EVENT-specific, not relay-specific. One malformed event of ours would otherwise
#                   blacklist a perfectly good relay for hours — the worst trade in this file.
_PERMANENT_REFUSALS = ("pow:", "blocked:", "restricted:")
_REFUSAL_PAUSE_SEC = 21600   # 6h. These are policy decisions, not outages: re-probing every 10 minutes
                             # learns nothing and costs a connect + publish each time. Long enough to
                             # stop the bleeding, short enough that a policy change is picked up daily.
_relay_fail: dict = {}            # relay -> failure count (spaced >= _FAIL_DEBOUNCE apart)
_relay_paused_until: dict = {}    # relay -> unix ts; skip the relay until then
_relay_last_fail: dict = {}       # relay -> unix ts of the last counted failure (debounce)
_relay_pause_streak: dict = {}    # relay -> consecutive pause cycles (no success between) → exp. backoff


def _relay_paused(relay: str) -> bool:
    return time.time() < _relay_paused_until.get(relay, 0)


def _note_relay_ok(relay: str) -> None:
    """A successful connect clears the relay's failure streak / pause / escalation."""
    _relay_fail.pop(relay, None)
    _relay_pause_streak.pop(relay, None)
    _relay_paused_until.pop(relay, None)
    _relay_last_fail.pop(relay, None)


def _is_429(e) -> bool:
    """True if a connect exception is an HTTP 429 (rate limited). websockets surfaces it as an
    InvalidStatus with a .response.status_code, and its str() carries 'HTTP 429'."""
    try:
        if getattr(getattr(e, "response", None), "status_code", None) == 429:
            return True
    except Exception:
        pass
    return "429" in str(e)


def _note_relay_429(relay: str) -> None:
    """A relay explicitly rate-limited us (HTTP 429). Honor it IMMEDIATELY — pause sends rather than
    grinding through the 3-fails-over-90s ramp (which lets a 429 storm through while it counts up).
    Logged once on the transition; while paused _publish_one/_sync short-circuit so we stop dialing it."""
    if not _relay_paused(relay):
        logger.warning("[nostr] %s rate-limited us (HTTP 429) — pausing sends for %dm",
                       relay, _RELAY_429_PAUSE_SEC // 60)
    _relay_paused_until[relay] = time.time() + _RELAY_429_PAUSE_SEC
    _relay_fail.pop(relay, None)
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
        # Escalating backoff: each pause cycle without a success in between doubles the pause (10m, 20,
        # 40, … capped at _RELAY_PAUSE_MAX), so a permanently dead/paid relay stops being re-dialed every
        # 10 minutes. A single success (_note_relay_ok) resets the streak.
        streak = _relay_pause_streak.get(relay, 0) + 1
        _relay_pause_streak[relay] = streak
        pause = min(_RELAY_PAUSE_SEC * (2 ** (streak - 1)), _RELAY_PAUSE_MAX)
        _relay_paused_until[relay] = time.time() + pause
        _relay_fail.pop(relay, None)
        logger.warning("[nostr] pausing sync with %s for %dm — %d failures (Tor+direct), streak %d",
                       relay, pause // 60, n, streak)


def _refusal_is_permanent(reason: str) -> bool:
    """Whether an `OK false <reason>` can ever be fixed by sending the same event again."""
    r = (reason or "").strip().lower()
    return r.startswith(_PERMANENT_REFUSALS)


# A refusal is a WRITE policy, so it gets its own clock rather than reusing _relay_paused_until.
# Two reasons, both of which made the first version of this useless or wrong:
#   * _connect() calls _note_relay_ok() on every successful connect, which POPS _relay_paused_until.
#     The firehose reads from these same upstreams continuously, so a publish ban stored there would
#     be wiped by the next read — minutes after being set, forever.
#   * _relay_paused() also short-circuits _sync(). Storing it there would stop us READING from a
#     relay that merely won't accept our writes, which is the opposite of what we want: nos.lol
#     refuses our events and is still one of the better read sources on the network.
# Nothing clears this but time. A relay's answer to "may I publish here" is not evidence about
# connectivity, and a successful socket is not evidence the policy changed.
_relay_refuse_until: dict = {}


def _relay_refuses(relay: str) -> bool:
    """True while a relay is known to refuse our WRITES (reads are unaffected)."""
    return time.time() < _relay_refuse_until.get(relay, 0)


def _note_relay_refusal(relay: str, reason: str) -> None:
    """A relay took the event and refused it for a reason retrying cannot change. Stop publishing.

    Separate from _note_relay_fail's ramp on purpose: that one debounces and needs three strikes,
    because a connect failure is usually a blip. This is not a blip — the relay told us its policy in
    words. One answer is all the evidence there is going to be, so act on the first one."""
    if not _relay_refuses(relay):
        logger.warning("[nostr] %s refuses our events (%s) — no publishes for %dh (reads continue)",
                       relay, (reason or "").strip()[:80], _REFUSAL_PAUSE_SEC // 3600)
    _relay_refuse_until[relay] = time.time() + _REFUSAL_PAUSE_SEC


# NIP-42: signed with the OPERATOR key, which is this node's own identity and the one key the relay
# process actually holds. It authenticates the CONNECTION, not the author — measured against the real
# relays, auth.nostr1.com and relay.froth.zone both accept events authored by OTHER pubkeys once the
# socket is authenticated, which is exactly what an outbox relaying its users' events needs.
# (eden.nostr.land does not: it answers `restricted: Pay …` authenticated or not, so the breaker above
# is what handles that one.)
_AUTH_SK = None   # None = not looked up yet; b"" = looked up and unavailable (don't retry the import)


def _auth_seckey():
    global _AUTH_SK
    if _AUTH_SK is None:
        try:
            from app.services import keystore
            from app.services.nostr import nostr_service
            nsec = keystore.get_operator_nsec()
            _AUTH_SK = nostr_service.decode_seckey(nsec) if nsec else b""
        except Exception as e:
            logger.warning("[nostr] no operator key for NIP-42 AUTH: %s", e)
            _AUTH_SK = b""
    return _AUTH_SK or None


def _auth_event(relay: str, challenge: str):
    """Build the kind-22242 response to a relay's AUTH challenge. None if we have no key to sign it."""
    sk = _auth_seckey()
    if not sk:
        return None
    try:
        from app.services.nostr.event import build_event
        return build_event(sk, 22242, "", [["relay", relay], ["challenge", challenge]])
    except Exception as e:
        logger.warning("[nostr] could not build the AUTH event for %s: %s", relay, e)
        return None


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
                logger.info("[nostr] %s connected DIRECT (Tor proxy unavailable)", relay)
            else:
                raise
    except Exception as e:
        if not _is_local(relay):
            # A 429 is an explicit rate-limit → pause immediately; any other failure counts toward the
            # debounced 3-strikes breaker. (Never circuit-break the local relay.)
            if _is_429(e):
                _note_relay_429(relay)
            else:
                _note_relay_fail(relay)   # both Tor + direct failed
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
    if not _is_local(relay) and (_relay_paused(relay) or _relay_refuses(relay)):
        return False   # paused after repeated connect failures, or known to refuse our writes
    eid = event.get("id") or ""
    try:
        async with _connect(relay, direct) as ws:
            await ws.send(json.dumps(["EVENT", event]))
            authed = False
            # How many EVENT frames we have sent whose verdict we have not taken yet. It matters
            # because BOTH copies (before and after authenticating) carry the SAME event id, so their
            # OKs are indistinguishable — the pre-auth `auth-required` refusal usually arrives after
            # we have already authenticated and re-sent. Counting is the only way to know that an
            # `auth-required` is answering the copy we already gave up on rather than the new one.
            pending = 1
            # READ UNTIL WE SEE THE OK FOR *THIS* EVENT, rather than returning on the first message.
            # A relay interleaves NOTICEs, an AUTH challenge, and the OK for our own AUTH event — and
            # that last one is the trap: it is a well-formed `OK true` for a DIFFERENT event id, so
            # taking the first OK would report "accepted" for an event the relay may have refused.
            # Bounded by _PUBLISH_TIMEOUT at the caller and by this message budget, so a chatty or
            # broken relay cannot hold the drain open.
            for _ in range(12):
                try:
                    raw = await asyncio.wait_for(ws.recv(), timeout=5)
                    msg = json.loads(raw)
                except (asyncio.TimeoutError, json.JSONDecodeError):
                    return True  # event was sent; some relays don't send OK promptly
                if not isinstance(msg, list) or not msg:
                    continue
                if msg[0] == "AUTH" and not authed and len(msg) > 1:
                    # NIP-42 challenge. Sign it, then RE-SEND the event: a relay that challenged us
                    # has already refused (or ignored) the first copy.
                    ev = _auth_event(relay, str(msg[1]))
                    if not ev:
                        continue   # no operator key — nothing to answer with; wait for its OK/timeout
                    authed = True
                    await ws.send(json.dumps(["AUTH", ev]))
                    await ws.send(json.dumps(["EVENT", event]))
                    pending += 1
                    continue
                if msg[0] == "OK":
                    if len(msg) > 1 and eid and msg[1] != eid:
                        continue   # the OK for our AUTH event, not for this one
                    ok = bool(msg[2]) if len(msg) > 2 else True
                    reason = str(msg[3]) if len(msg) > 3 else ""
                    pending -= 1
                    if ok:
                        return True
                    # `auth-required` is the relay ASKING, not refusing — some send it instead of an
                    # AUTH frame, and the copy it refers to is usually the one we sent before we
                    # could answer. Only the verdict on the LAST copy we sent is final.
                    if reason.strip().lower().startswith("auth-required") and pending > 0:
                        continue
                    if not _is_local(relay) and _refusal_is_permanent(reason):
                        _note_relay_refusal(relay, reason)
                    return False
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


async def await_one(relays, filters: list, timeout: float = 60.0, direct: bool = False) -> dict | None:
    """Open a REQ and return the FIRST event matching `filters` (already-stored OR live-arriving),
    else None on timeout. Unlike query() (which returns after EOSE), this keeps the subscription open
    waiting for a live event — used to await an async job RESULT that may not exist yet at subscribe
    time. Races all relays; first match wins."""
    relays = normalize_relays(relays)
    if not relays:
        return None
    result: dict = {}
    done = asyncio.Event()

    async def _one(relay: str) -> None:
        if not _is_local(relay) and _relay_paused(relay):
            return
        sub_id = uuid.uuid4().hex[:16]
        try:
            async with _connect(relay, direct) as ws:
                await ws.send(json.dumps(["REQ", sub_id] + filters))
                deadline = asyncio.get_event_loop().time() + timeout
                while not done.is_set():
                    remaining = deadline - asyncio.get_event_loop().time()
                    if remaining <= 0:
                        break
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=min(remaining, 25))
                    except asyncio.TimeoutError:
                        try:
                            await ws.ping()   # keepalive: a long-running job may take minutes
                        except Exception:
                            break
                        continue
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if (isinstance(msg, list) and len(msg) >= 3 and msg[0] == "EVENT"
                            and msg[1] == sub_id and isinstance(msg[2], dict) and msg[2].get("id")):
                        if not done.is_set():
                            result.update(msg[2])
                            done.set()
                        break
                    # EOSE is ignored on purpose — keep the sub open for a live result.
                with contextlib.suppress(Exception):
                    await ws.send(json.dumps(["CLOSE", sub_id]))
        except Exception as e:
            logger.debug("[nostr] await_one %s failed: %s", relay, e)

    tasks = [asyncio.create_task(_one(r)) for r in relays]
    try:
        await asyncio.wait_for(done.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass
    for t in tasks:
        t.cancel()
    with contextlib.suppress(Exception):
        await asyncio.gather(*tasks, return_exceptions=True)
    return dict(result) if result else None


async def subscribe(relay: str, filters: list, handler, stop: asyncio.Event, direct: bool = False,
                    since_now: bool = False, live_only: bool = False) -> None:
    """Persistent subscription to ONE relay: REQ `filters`, await handler(ev) per live EVENT,
    auto-reconnecting (capped backoff) until `stop` is set. Used by the DVM worker loop.

    since_now: stamp each filter's `since` with the CURRENT time on every (re)connect, so a reconnect
    after a drop does NOT replay old / already-handled events — only live ones from now forward.

    live_only: drop everything the relay sends BEFORE its EOSE, i.e. the stored backlog, and handle
    only what arrives afterwards. Use this instead of since_now when the events carry timestamps you
    cannot trust: a NIP-59 gift wrap is deliberately backdated by up to two days to defeat timing
    analysis, so `since=now` silently discards real, newly-arrived messages, while no filter at all
    replays the entire mailbox on every reconnect — as a notification per message.
    ARRIVAL is the only honest signal there, and EOSE is where it changes.

    If the relay never sends EOSE the gate opens anyway after _EOSE_FALLBACK seconds: degrading to
    "handle the backlog too" is recoverable, and silence is not."""
    backoff = 1
    while not stop.is_set():
        sub_id = uuid.uuid4().hex[:16]
        req_filters = [{**f, "since": int(time.time())} for f in filters] if since_now else filters
        try:
            async with _connect(relay, direct) as ws:
                await ws.send(json.dumps(["REQ", sub_id] + req_filters))
                backoff = 1
                # Per-CONNECTION, not per-subscription: a reconnect re-REQs and the relay replays its
                # backlog again, so the gate has to close again with it.
                gated = live_only
                opened_at = time.time()
                while not stop.is_set():
                    try:
                        raw = await asyncio.wait_for(ws.recv(), timeout=30)
                    except asyncio.TimeoutError:
                        try:
                            await ws.ping()
                        except Exception:
                            break   # dead connection → reconnect
                        continue
                    try:
                        msg = json.loads(raw)
                    except json.JSONDecodeError:
                        continue
                    if (isinstance(msg, list) and len(msg) >= 2 and msg[0] == "EOSE"
                            and msg[1] == sub_id):
                        gated = False       # backlog delivered; everything after this is live
                        continue
                    # CLOSED ends the SUBSCRIPTION but leaves the socket up (NIP-01: "too many
                    # subscriptions", a rejected filter, a restarting relay). Ignored, that is the
                    # worst failure this loop can have: the connection still answers pings, so nothing
                    # reconnects, and we sit forever on a subscription the relay has already forgotten
                    # — DM and call notifications simply stop, node-wide, until someone restarts the
                    # process. Break out and let the reconnect loop re-REQ.
                    if (isinstance(msg, list) and len(msg) >= 2 and msg[0] == "CLOSED"
                            and msg[1] == sub_id):
                        logger.warning("[nostr] %s CLOSED our subscription (%s) — resubscribing",
                                       relay, (msg[2] if len(msg) > 2 else ""))
                        break
                    if (isinstance(msg, list) and len(msg) >= 3 and msg[0] == "EVENT"
                            and msg[1] == sub_id and isinstance(msg[2], dict)):
                        if gated:
                            if time.time() - opened_at < _EOSE_FALLBACK:
                                continue    # stored backlog — not news, don't hand it on
                            gated = False   # no EOSE in time: open up rather than stay silent forever
                            logger.warning("[nostr] %s sent no EOSE in %ss — ungating %s",
                                           relay, _EOSE_FALLBACK, sub_id)
                        try:
                            await handler(msg[2])
                        except Exception as e:
                            logger.warning("[nostr] subscribe handler error: %s", e)
        except Exception as e:
            if not stop.is_set():
                logger.debug("[nostr] subscribe %s dropped: %s — reconnecting", relay, e)
        if stop.is_set():
            break
        await asyncio.sleep(min(backoff, 30))
        backoff = min(backoff * 2, 30)
