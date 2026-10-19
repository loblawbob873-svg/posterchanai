"""Async Nostr relay client over websockets (NIP-01 client messages).

publish() best-effort fans an event out to every relay; query() opens a short
REQ subscription on each relay, merges + dedups events across all of them, and
returns when every relay sends EOSE or the timeout elapses. Bounded timeouts so
a dead relay can't stall a poll (mirrors the fedi bridge's defensive timeouts).
"""

import json
import os
import sys
import time
import uuid
import asyncio
import contextlib
import contextvars
import logging
from urllib.parse import urlparse

import websockets


def _tolerate_unfinished_connections() -> None:
    """websockets 17: when a TLS handshake fails (through the Tor proxy, most often), asyncio calls connection_lost()
    on a Connection whose connection_made() never ran -- so `recv_messages` does not exist and the callback raises
    AttributeError. The connect call itself already failed and reported it; this was only the cleanup, logged as a
    traceback ~130 times an hour by the relay (2026-10-18), burying real errors. A connection that was never set up is
    settled and left alone; every other one runs the library's own connection_lost unchanged."""
    try:
        from websockets.asyncio import connection as _c
    except Exception:       # noqa: BLE001 -- another websockets layout: nothing to patch
        return
    orig = _c.Connection.connection_lost
    if getattr(orig, "_pc_tolerant", False):
        return

    def connection_lost(self, exc):
        if not hasattr(self, "recv_messages"):
            try:
                self.protocol.receive_eof()
            except Exception:       # noqa: BLE001
                pass
            w = getattr(self, "connection_lost_waiter", None)
            if w is not None and not w.done():
                w.set_result(None)
            return None
        return orig(self, exc)
    connection_lost._pc_tolerant = True
    _c.Connection.connection_lost = connection_lost


_tolerate_unfinished_connections()

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


def _guard_proxy_parser():
    """websockets 17.1's HTTP-proxy handshake parses the proxy's reply from data_received, eof_received
    AND connection_lost. When the proxy hangs up before answering, the first of those already failed the
    response future and the next one fails it AGAIN -- `set_exception` on a done future raises
    InvalidStateError inside an event-loop callback, which asyncio logs as a full traceback. Measured on
    server1: ~720 of them in three hours, one per relay connect that fell back from Tor to direct, burying
    every real error in the journal. Nothing was wrong except the second set: the connect had already
    failed (and been retried direct). So: once the future is settled, a later parse is a no-op."""
    try:
        from websockets.asyncio.client import HTTPProxyConnection
    except Exception:                       # another websockets version: nothing to guard
        return
    if getattr(HTTPProxyConnection.run_parser, "_pc_guarded", False):
        return
    original = HTTPProxyConnection.run_parser

    def run_parser(self):
        if self.response.done():
            return
        original(self)
    run_parser._pc_guarded = True
    HTTPProxyConnection.run_parser = run_parser


_guard_proxy_parser()
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
# How long to wait for ONE relay's `OK` before giving up on hearing a verdict and assuming the event
# landed. MEASURED, not picked: replaying a real event to this node's 24 upstreams, every relay that
# was ever going to answer did so within 1.9s — accept, duplicate and reject alike. The only one that
# took longer took 7.7s and then said nothing at all, ever.
#
# It was 5s, and the cost was not the 5s. The whole publish is wrapped in _PUBLISH_TIMEOUT by
# publish_to, and the drain waits for the LAST relay before starting the next event — so one relay
# that never answers set the pace for all 24, and under that pressure the outer timeout began firing
# on relays that WOULD have answered: nos.lol's `pow:` verdict never arrived, so the breaker could
# not learn it, and auth.nostr1.com's NIP-42 round trip (challenge → sign → re-send → OK) was cut
# off 163 times out of 243. Bounding the silence is what gives the round trip room inside the same
# 10s budget.
_OK_WAIT = 3
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


# THE BREAKER REMEMBERS ACROSS A RESTART. Its pauses lived in memory only, so every restart (each deploy) re-dialled
# every dead upstream from scratch -- ~400 failed TLS handshakes in the relay's first two minutes on server1
# (2026-10-18), re-learning what it had known a minute before. Pauses and their escalation streaks are written to a
# small file when a pause starts or a paused relay recovers, and read back at import. Advisory state: a lost or
# stale file only means re-learning, so any error reading or writing it is ignored.
def _breaker_path() -> str:
    p = os.environ.get("PC_RELAY_BREAKER_FILE")
    if p is not None:
        return p                                        # "" turns persistence off
    if "pytest" in sys.modules:
        return ""                                       # tests never write the working tree's state
    return os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))),
                        "data", "relay_breaker.json")


def _save_breaker() -> None:
    path = _breaker_path()
    if not path:
        return
    now = time.time()
    state = {r: [until, _relay_pause_streak.get(r, 0)] for r, until in _relay_paused_until.items() if until > now}
    try:
        tmp = "%s.%d.tmp" % (path, os.getpid())
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(state, f)
        os.replace(tmp, path)
    except OSError:
        pass


def _load_breaker() -> None:
    path = _breaker_path()
    if not path:
        return
    try:
        with open(path, encoding="utf-8") as f:
            state = json.load(f)
        now = time.time()
        for r, (until, streak) in (state or {}).items():
            if isinstance(r, str) and float(until) > now:
                _relay_paused_until[r] = float(until)
                _relay_pause_streak[r] = int(streak)
    except (OSError, ValueError, TypeError):
        pass


def _note_relay_ok(relay: str) -> None:
    """A successful connect clears the relay's failure streak / pause / escalation."""
    _relay_fail.pop(relay, None)
    had = _relay_pause_streak.pop(relay, None) is not None
    had = _relay_paused_until.pop(relay, None) is not None or had
    _relay_last_fail.pop(relay, None)
    if had:
        _save_breaker()


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
    _save_breaker()


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
        _save_breaker()
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
        # ...and no permessage-deflate: compressing bytes that never leave this machine is CPU on both ends -- the
        # relay deflated every answer to the push service, the notification poller and the bots (~8% of its CPU,
        # py-spy 2026-10-18). nostr_store/relay_reader made the same choice (relay_reader.loopback_kw).
        return {"proxy": None, "compression": None}
    return {} if direct else _proxy_kw()


# RELAYS THAT REFUSE TOR, REMEMBERED. Measured on server1: wss://asia.vectorapp.io/nostr was tried through
# the Tor proxy 174 times in an hour, timed out after the full 8s every time, and connected DIRECT straight
# after; relay.damus.io answers Tor exits with HTTP 503. Each of those was 8s of a federation task waiting
# on an attempt already known to fail. A relay whose proxied connect failed and whose direct retry then
# SUCCEEDED goes direct for _TOR_REFUSED_TTL -- which is exactly where the fallback took it 8s later anyway,
# so nothing goes direct that did not already. A relay that fails both ways is the breaker's business.
_TOR_REFUSED_TTL = 6 * 3600
_TOR_REFUSED_MAX = 512
_tor_refused: dict = {}


def _tor_refuses(relay: str) -> bool:
    until = _tor_refused.get(relay)
    if until is None:
        return False
    if until <= time.time():
        _tor_refused.pop(relay, None)
        return False
    return True


def _note_tor_refused(relay: str) -> None:
    if len(_tor_refused) >= _TOR_REFUSED_MAX and relay not in _tor_refused:
        _tor_refused.pop(min(_tor_refused, key=_tor_refused.get), None)   # the one expiring soonest
    _tor_refused[relay] = time.time() + _TOR_REFUSED_TTL


async def _open(relay: str, direct: bool, **kw):
    """Open a relay websocket with PROXY-FIRST, FALL-BACK-TO-DIRECT resilience: try the configured
    built-in proxy (→ Tor) first, and if that connect fails, retry the SAME relay directly. So a
    flaky/down Tor proxy degrades to a direct connection instead of dropping federation entirely.
    Loopback relays never proxy; `direct=True` callers skip the proxy attempt. Extra kwargs (e.g.
    max_size for the firehose) pass through."""
    # Identify PosterChan server traffic even when a peer is reached through a public proxy.
    kw.setdefault("user_agent_header", "PosterChan/Server")
    base = _conn_kw(relay, direct)
    if base.get("proxy") and _tor_refuses(relay):
        base = {"proxy": None}
    try:
        try:
            ws = await websockets.connect(relay, open_timeout=_CONNECT_TIMEOUT, **base, **kw)
        except Exception as e:
            if base.get("proxy"):
                logger.warning("[nostr] proxy connect to %s failed (%s) — retrying direct", relay, e)
                ws = await websockets.connect(relay, open_timeout=_CONNECT_TIMEOUT, proxy=None, **kw)
                _note_tor_refused(relay)
                logger.info("[nostr] %s connected DIRECT (refuses Tor; direct for %dh)", relay,
                            _TOR_REFUSED_TTL // 3600)
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
    return ws


@contextlib.asynccontextmanager
async def _connect(relay: str, direct: bool, **kw):
    """`_open`, closed when the block ends."""
    ws = await _open(relay, direct, **kw)
    try:
        yield ws
    finally:
        try:
            await ws.close()
        except Exception:
            pass


# QUERIES REUSE THEIR CONNECTION. Every query opened a websocket -- a TLS handshake, through Tor -- to every upstream
# relay and closed it after EOSE: the relay's sync sweep asks about the web of trust in author batches, so server1
# made ~1,200 outbound connections a minute and TLS (handshakes + record reads) was ~17% of the relay process's CPU,
# the largest single term (py-spy, 2026-10-18). An unauthenticated query now borrows an idle connection to the same
# relay (same proxy route), CLOSEs its subscription after EOSE and hands it back. One idle connection per relay per
# event loop; one unused for _POOL_IDLE_MAX is closed on the next return. A pooled socket that turns out dead is
# replaced by a fresh one, once. Authenticated queries are not pooled: a socket's AUTH belongs to one key.
_POOL: dict = {}               # loop -> {(relay, direct): (ws, idle_since)}
_POOL_IDLE_MAX = 90.0


def _pool() -> dict:
    loop = asyncio.get_running_loop()
    for lp in [lp for lp in _POOL if lp is not loop and lp.is_closed()]:
        _POOL.pop(lp, None)                            # a finished asyncio.run(): its sockets died with it
    return _POOL.setdefault(loop, {})


def _usable(ws) -> bool:
    try:
        from websockets.protocol import State
        return ws.state is State.OPEN
    except Exception:       # noqa: BLE001
        return False


async def _borrow(relay: str, direct: bool):
    """(ws, pooled) -- an idle pooled connection to `relay`, or a new one."""
    p = _pool()
    got = p.pop((relay, direct), None)
    if got is not None:
        ws, since = got
        if _usable(ws) and time.monotonic() - since < _POOL_IDLE_MAX:
            return ws, True
        await _close_quietly(ws)
    return await _open(relay, direct), False


async def _give_back(relay: str, direct: bool, ws) -> None:
    p = _pool()
    now = time.monotonic()
    for k, (old, since) in list(p.items()):
        if now - since >= _POOL_IDLE_MAX:
            p.pop(k, None)
            await _close_quietly(old)
    if _usable(ws) and (relay, direct) not in p:
        p[(relay, direct)] = (ws, now)
    else:
        await _close_quietly(ws)


async def _close_quietly(ws) -> None:
    try:
        await ws.close()
    except Exception:       # noqa: BLE001
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
                    raw = await asyncio.wait_for(ws.recv(), timeout=_OK_WAIT)
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


def publishable(relays) -> list[str]:
    """The relays a publish should actually be SENT to: the configured set minus the ones this
    process already knows will not take it (paused after connect failures, or refusing our writes).

    Exists because "we didn't send it" and "we sent it and it was refused" are different facts, and
    conflating them cost all three of the outbox's numbers at once. A paused relay left in the target
    list is counted as a target, never acks, and so reads as a MISS — which meant it was retried
    twice per event forever (pinning the retry pool at its cap, so genuine misses got no retry at
    all) and dragged the delivery rate down to a number that described our own circuit breaker rather
    than the network. Measured on this node: 5 paused relays appearing in 243 of 243 give-ups."""
    return [r for r in normalize_relays(relays) if _is_local(r) or not (_relay_paused(r) or _relay_refuses(r))]


async def publish_to(relays, event: dict, direct: bool = False) -> set:
    """Publish an event to all relays; return the SET of relay URLs that accepted/received it.

    Lets callers (e.g. the relay outbox) compute the misses and retry just those. Callers that
    compute misses should take their target list from `publishable()`, not from the raw config."""
    relays = normalize_relays(relays)
    if not relays:
        return set()
    results = await asyncio.gather(
        *[asyncio.wait_for(_publish_one(r, event, direct), timeout=_PUBLISH_TIMEOUT) for r in relays],
        return_exceptions=True,
    )
    # A TIMEOUT HERE IS NOT SILENT ANY MORE. This wait_for is OUTSIDE _publish_one, so its
    # TimeoutError is swallowed by return_exceptions and never reached the log inside — and that is
    # how the biggest failure on this node stayed invisible: relay.wellorder.net missed 243 of 243
    # events while logging only 64 errors, and auth.nostr1.com missed 163 while logging NONE, so the
    # NIP-42 handshake looked broken when it was simply being cut off mid-round-trip.
    for r, res in zip(relays, results):
        if isinstance(res, asyncio.TimeoutError):
            logger.warning("[nostr] publish to %s timed out after %ss (no verdict — counted as a miss)",
                           r, _PUBLISH_TIMEOUT)
    return {r for r, ok in zip(relays, results) if ok is True}


async def publish(relays, event: dict, direct: bool = False) -> int:
    """Publish an event to all relays. Returns how many accepted/received it."""
    return len(await publish_to(relays, event, direct))


async def _query_one(relay: str, filters: list, out: dict, timeout: float, direct: bool = False,
                     auth_seckey: bytes | None = None) -> None:
    if not _is_local(relay) and _relay_paused(relay):
        return   # circuit breaker: relay paused after repeated connect failures — skip silently
    if auth_seckey:
        try:
            async with _connect(relay, direct) as ws:
                await _run_query(ws, relay, filters, out, timeout, auth_seckey)
        except Exception as e:
            logger.warning(f"[nostr] query {relay} failed: {e}")
        return
    for attempt in (0, 1):
        ws = None
        try:
            ws, pooled = await _borrow(relay, direct)
        except Exception as e:
            logger.warning(f"[nostr] query {relay} failed: {e}")
            return
        try:
            done = await _run_query(ws, relay, filters, out, timeout, None)
        except asyncio.CancelledError:
            try:
                ws.transport.abort()                   # being cancelled: never leave it open, never pool it
            except Exception:       # noqa: BLE001
                pass
            raise
        except Exception as e:
            await _close_quietly(ws)
            if pooled and attempt == 0:
                continue                               # a stale pooled socket: once more, fresh
            logger.warning(f"[nostr] query {relay} failed: {e}")
            return
        if done:
            await _give_back(relay, direct, ws)        # answered in full: its subscription is CLOSEd
        else:
            await _close_quietly(ws)                   # timed out: a slow relay may still be sending
        return


# WHO PAYS FOR THE UPSTREAM READS. py-spy on server1 (2026-10-18): ~18% of the relay process was json-decoding
# upstream query answers, and nothing could say which feature asked for them. Tallied per caller (the first module
# outside this one: one frame walk per query, not per frame) and printed every 5 minutes as `upstream cost:`.
_UP_COST: dict = {}               # caller -> [queries, frames, bytes]
_UP_COST_AT = [0.0]
_UP_COST_EVERY = 300.0
_UP_CALLER = contextvars.ContextVar("pc_upstream_caller", default="?")


def _up_caller() -> str:
    f = sys._getframe(2)
    for _ in range(12):
        if f is None:
            break
        mod = f.f_globals.get("__name__", "")
        if mod and mod != __name__ and not mod.startswith("asyncio"):
            return "%s.%s" % (mod.rsplit(".", 1)[-1], f.f_code.co_name)
        f = f.f_back
    return "?"


def _up_note(frames: int, nbytes: int) -> None:
    c = _UP_COST.setdefault(_UP_CALLER.get(), [0, 0, 0])
    c[1] += frames
    c[2] += nbytes


def _up_report() -> None:
    now = time.monotonic()
    if not _UP_COST_AT[0]:
        _UP_COST_AT[0] = now
        return
    if now - _UP_COST_AT[0] < _UP_COST_EVERY or not _UP_COST:
        return
    span = now - _UP_COST_AT[0]
    top = sorted(_UP_COST.items(), key=lambda kv: -kv[1][2])[:6]
    logger.info("[nostr] upstream cost (%.0fs): %s", span, "; ".join(
        "%s q=%d frames=%d %.1fMB" % (k, q, fr, b / 1e6) for k, (q, fr, b) in top))
    _UP_COST.clear()
    _UP_COST_AT[0] = now


async def _run_query(ws, relay: str, filters: list, out: dict, timeout: float, auth_seckey) -> bool:
    """One REQ on `ws` until EOSE (True) or the timeout (False); the subscription is CLOSEd either way."""
    sub_id = uuid.uuid4().hex[:16]
    frames = nbytes = 0
    await ws.send(json.dumps(["REQ", sub_id] + filters))
    auth_id = None
    retried = False
    done = False
    deadline = asyncio.get_event_loop().time() + timeout
    while True:
        remaining = deadline - asyncio.get_event_loop().time()
        if remaining <= 0:
            break
        try:
            raw = await asyncio.wait_for(ws.recv(), timeout=remaining)
        except asyncio.TimeoutError:
            break
        frames += 1
        nbytes += len(raw)
        try:
            msg = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if not isinstance(msg, list) or not msg:
            continue
        if msg[0] == "AUTH" and len(msg) >= 2 and auth_seckey:
            from app.services.nostr.event import build_event
            auth = build_event(auth_seckey, 22242, "",
                               [["relay", relay], ["challenge", str(msg[1])]])
            auth_id = auth["id"]
            await ws.send(json.dumps(["AUTH", auth]))
        elif msg[0] == "OK" and len(msg) >= 3 and msg[1] == auth_id:
            if msg[2] and not retried:
                retried = True
                await ws.send(json.dumps(["REQ", sub_id] + filters))
        elif msg[0] == "CLOSED" and len(msg) >= 3 and msg[1] == sub_id \
                and str(msg[2]).startswith("auth-required") and auth_seckey:
            continue
        elif msg[0] == "CLOSED" and len(msg) >= 2 and msg[1] == sub_id:
            break      # refused -- often a per-connection rate limit: stop waiting, and do not pool this socket
        elif msg[0] == "EVENT" and len(msg) >= 3 and msg[1] == sub_id:
            ev = msg[2]
            if isinstance(ev, dict) and ev.get("id"):
                out[ev["id"]] = ev
        elif msg[0] == "EOSE" and len(msg) >= 2 and msg[1] == sub_id:
            done = True
            break
    _up_note(frames, nbytes)
    try:
        await ws.send(json.dumps(["CLOSE", sub_id]))
    except Exception:
        if done:
            raise
    return done


async def query(relays, filters: list, timeout: float = _DEFAULT_QUERY_TIMEOUT,
                direct: bool = False, auth_seckey: bytes | None = None) -> list[dict]:
    """Run a REQ with `filters` against all relays; return deduped events (newest-first)."""
    relays = normalize_relays(relays)
    if not relays:
        return []
    out: dict = {}
    caller = _up_caller()
    _UP_COST.setdefault(caller, [0, 0, 0])[0] += 1
    _up_report()
    tok = _UP_CALLER.set(caller)          # the gathered tasks copy this context, so their frames count here
    try:
        await asyncio.gather(
            *[_query_one(r, filters, out, timeout, direct, auth_seckey) for r in relays],
            return_exceptions=True,
        )
    finally:
        _UP_CALLER.reset(tok)
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
        wait = (_relay_paused_until.get(relay, 0) - time.time()) if not _is_local(relay) else 0
        if wait > 0:                       # the breaker paused it: wait that out, never dial a dead relay in a loop
            try:
                await asyncio.wait_for(stop.wait(), timeout=min(wait, 3600))
                break
            except asyncio.TimeoutError:
                continue
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


_load_breaker()
