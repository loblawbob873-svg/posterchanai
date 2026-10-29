"""CONCORD TALKS ONLY TO THIS NODE -- the server half of the relay tunnel.

Asked for: "any way to make our concord implementation do that also so each client don't have to communicate
with other concord relays?" The relay mirror (nostr_relay/concord_mirror.py) answers what it holds; everything
else a Concord client still needed from another relay -- the membership vault on the CORD relays, invite
lookups, the directory, NIP-29 rooms, live rooms and voice signals of communities hosted elsewhere -- now goes
through ONE websocket endpoint on this node, `/ws/concord-relay?u=<relay>&t=<ticket>`, which opens the
connection to `<relay>` itself and carries the frames both ways unchanged. The client's own code is unchanged
above its socket: NIP-42 AUTH, REQ/EVENT/CLOSE all pass through, so the far relay sees the same conversation.

Rules, each one a reason the endpoint is not an open proxy:
  * a TICKET, minted only for a signed-in member (`instance_membership.require_user`), HMAC-signed with a key
    derived from the operator key, short-lived; it names the member, so limits are per person;
  * the target must be ws:// or wss:// to a PUBLIC host (search_service.is_safe_url on the http form) --
    never loopback, LAN or this node's own relay port;
  * outbound goes through the proxy's FALLBACK listener (Tor 1 -> Tor 2 -> direct); a relay that refuses Tor or
    times out over it is then dialled direct (that chain's own last hop, which it only takes on a connect error);
    with no proxy the endpoint refuses and the client dials the relay itself, exactly as before;
  * at most PER_MEMBER open tunnels per member and GLOBAL_MAX in all; frames are capped at MAX_FRAME.
"""
from __future__ import annotations

import asyncio
import hashlib
import hmac
import logging
import json
import time
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

TICKET_TTL = 3600
# Concord holds a LIVE subscription per relay per room it shows, and each one is a tunnel. 24 per member was full
# of them on one account (2026-10-13: 137 opened, 2,314 refused in an hour, every send among the refused), so the
# share is sized for the client as it is; a refusal is logged with the count it hit (`refused`).
PER_MEMBER = 128
GLOBAL_MAX = 3000
MAX_FRAME = 4 * 1024 * 1024
OPEN_TIMEOUT = 20
# A TOR DIAL GETS 6 SECONDS, NOT 20. Measured 2026-10-14 from server1: a relay Tor reaches opens in 3-5 s, and
# asia.vectorapp.io (a CORD relay) timed out over Tor 2 times in 3 at the full 20 s while answering DIRECT in ~1 s.
# The client gives a control-history read 10 s and a send 5 s, so a 20 s Tor attempt in front of the direct hop
# meant NO answer ever arrived in time: the room had no control events, its channels did not exist, and every
# send failed with "this channel is no longer available with your current community membership" -- to the
# community's own owner.
#
# TOR 1, TOR 2, ... THEN DIRECT -- EACH A WHOLE HANDSHAKE, AND NOTHING SKIPS AHEAD TO DIRECT (2026-10-27: "make sure our
# concord uses the http proxy correctly! tor1 tor2 then default"). The proxy listener only fails over on a CONNECT
# error, so a relay that timed out or refused over one exit went straight to this node's own address; and a host that
# had failed over Tor once was then dialled DIRECT FIRST for half an hour. jskitty.com and asia.vectorapp.io answered
# hundreds of those an hour from this IP and now 403 it with any User-Agent -- while jskitty answers every Tor exit and
# asia.vectorapp.io answers one of four. So every Tor daemon the proxy runs is tried in the proxy's own order
# (http_proxy_service.socks_backends), DNS through Tor too (socks5h); the circuit that last worked for a host goes
# FIRST for GOOD_FOR seconds (an order among Tor circuits, never a shortcut to direct); direct is the last hop only.
TOR_OPEN_TIMEOUT = 6
GOOD_FOR = 1800
_good: dict[str, tuple[str, float]] = {}
# A HOST NO HOP REACHED IS NOT DIALLED AGAIN FOR A WHILE. Every page of every member retries a relay it cannot reach
# every few seconds, and each retry is now every Tor circuit plus this node's address: asia.vectorapp.io, unreachable
# both ways on 2026-10-27, would have been five connections per retry from here. For UNREACHABLE_FOR seconds the
# tunnel answers "unreachable" without dialling; the first tunnel after that tries the whole chain again.
UNREACHABLE_FOR = 120
# ...doubling while it stays that way, up to UNREACHABLE_MAX: asia.vectorapp.io refused every Tor daemon and this
# node's address all day on 2026-10-27, and a flat two minutes still walked the whole chain ~19 times an hour.
UNREACHABLE_MAX = 1800
_unreachable: dict[str, float] = {}
_strikes: dict[str, int] = {}
# ONE DIAL AT A TIME TO A HOST NOT KNOWN TO ANSWER. Concord opens several tunnels to one relay at once, so when a
# failing host's pause ran out they ALL walked the whole chain together, before the first could record the failure:
# measured on poster.place 2026-10-29, asia.vectorapp.io six full chains in 9 s, fifteen in 20 s -- the backoff above
# never got the chance to work. A host something reached in the last GOOD_FOR seconds is dialled in parallel as before.
_reached: dict[str, float] = {}
_dialing: dict[str, asyncio.Lock] = {}

_open: dict[str, int] = {}
_total = 0


def _key() -> bytes:
    from app.services import settings_store
    sk = settings_store._operator_seckey(None)
    if not sk:
        raise RuntimeError("no operator key")
    raw = sk if isinstance(sk, (bytes, bytearray)) else bytes.fromhex(str(sk))
    return hashlib.sha256(b"pc-concord-tunnel\x00" + bytes(raw)).digest()


def issue(pubkey: str, now: int | None = None) -> str:
    exp = int(now or time.time()) + TICKET_TTL
    msg = f"{pubkey}.{exp}"
    sig = hmac.new(_key(), msg.encode(), hashlib.sha256).hexdigest()[:40]
    return f"{msg}.{sig}"


def verify(ticket: str, now: int | None = None) -> str | None:
    """The member's pubkey, or None for a forged, malformed or expired ticket."""
    try:
        pk, exp, sig = str(ticket or "").split(".")
        if len(pk) != 64 or int(exp) < int(now or time.time()):
            return None
        want = hmac.new(_key(), f"{pk}.{exp}".encode(), hashlib.sha256).hexdigest()[:40]
        return pk if hmac.compare_digest(want, sig) else None
    except Exception:
        return None


def check_target(url: str) -> tuple[bool, str]:
    """ws/wss to a public host only (the SSRF guard every other outbound fetch uses, on the http form)."""
    try:
        p = urlparse(str(url or ""))
    except Exception:
        return False, "bad url"
    if p.scheme not in ("ws", "wss") or not p.hostname:
        return False, "not a relay url"
    from app.services.nostr.peer_ua import never_dial
    if never_dial(str(url)):
        return False, "this node never dials that relay"
    from app.services.search_service import is_safe_url
    http = ("https" if p.scheme == "wss" else "http") + str(url)[len(p.scheme):]
    ok, why = is_safe_url(http)
    if not ok:
        return False, why or "refused"
    if p.hostname.lower() in ("poster.place",) or (p.port and p.port in (3051, 3052)):
        return False, "this node's own relay is reached directly"
    return True, ""


def proxy_url() -> str:
    """The proxy's Tor 1 -> Tor 2 -> direct listener, or '' when this node runs no proxy (the relay's rule)."""
    from app.services import settings_store
    from app.services.nostr_relay.thread import _fallback_proxy_url

    def g(k, d=None):
        v = settings_store.get(k, None)
        return d if v in (None, "") else v

    def gi(k, d=0):
        try:
            return int(g(k, d))
        except (TypeError, ValueError):
            return d

    def gb(k, d=False):
        v = g(k, None)
        return d if v is None else str(v).strip().lower() in ("1", "true", "yes", "on")
    return _fallback_proxy_url(g, gi, gb)


_refused_logged: dict[str, float] = {}


def refused(pk: str, why: str) -> None:
    """One log line per member per minute: which limit, and how many tunnels they held (no target, no content)."""
    now = time.monotonic()
    if now - _refused_logged.get(pk, -1e9) < 60:
        return
    _refused_logged[pk] = now
    logger.info("[concord-tunnel] refused %s… (%s): member holds %d, %d open in all",
                pk[:8], why, _open.get(pk, 0), _total)


# HOW OFTEN WE DIAL SOMEBODY ELSE'S RELAY, said every 10 minutes (2026-10-28: jskitty blocked this node for
# "churning connections/websockets"). Counted per far host when a tunnel actually reaches it.
DIAL_REPORT_EVERY = 600
SHORT_TUNNEL_S = 15      # a tunnel closed this soon after its dial carried one read or one send, not a subscription
_dials: dict[str, int] = {}
_short: dict[str, int] = {}
_shapes: dict[tuple, int] = {}
_dials_since = time.monotonic()


def frame_shape(frame: str) -> str:
    """What a client's FIRST frame asked for, as a verb and kinds -- never a filter value or any content."""
    try:
        msg = json.loads(frame)
        verb = str(msg[0])[:8]
        if verb == "REQ":
            fs = [f for f in msg[2:] if isinstance(f, dict)]
            kinds = sorted({int(k) for f in fs for k in (f.get("kinds") or [])})
            shape = "REQ " + (",".join(map(str, kinds[:4])) + ("+" if len(kinds) > 4 else "") or "any")
            # Counts and flags only -- which CLIENT PATH sent it (a live room stream has authors + since, a peer lobby
            # a limit, ...), never an author, a tag or a time.
            authors = sum(len(f.get("authors") or []) for f in fs)
            return shape + (" a%d" % authors if authors else "") + (" since" if any("since" in f for f in fs) else "") \
                + (" limit" if any("limit" in f for f in fs) else "")
        if verb == "EVENT" and isinstance(msg[1], dict):
            return "EVENT %d" % int(msg[1].get("kind", -1))
        return verb
    except Exception:
        return "?"


def _report() -> None:
    global _dials_since
    if time.monotonic() - _dials_since < DIAL_REPORT_EVERY:
        return
    mins = int((time.monotonic() - _dials_since) / 60)
    top = sorted(_dials.items(), key=lambda x: -x[1])[:8]
    logger.info("[concord-tunnel] dialled in the last %d min: %s", mins,
                ", ".join("%s %d (%d short)" % (k, v, _short.get(k, 0)) for k, v in top))
    shapes = sorted(_shapes.items(), key=lambda x: -x[1])[:8]
    if shapes:
        logger.info("[concord-tunnel] short tunnels by first frame: %s",
                    ", ".join("%s %s x%d" % (h, sh, n) for (h, sh), n in shapes))
    longs = sorted(_long.items(), key=lambda x: -len(x[1]))[:8]
    if longs:
        logger.info("[concord-tunnel] long tunnels closed, by who ended them (median s): %s",
                    ", ".join("%s by %s %s x%d (%ds)" % (h, who, sh, len(v), sorted(v)[len(v) // 2])
                              for (h, who, sh), v in longs))
    _dials.clear()
    _short.clear()
    _shapes.clear()
    _long.clear()
    _dials_since = time.monotonic()


def dialled(url: str) -> None:
    h = _host(url)
    _dials[h] = _dials.get(h, 0) + 1
    _report()


_long: dict[tuple, list] = {}


def client_kind(user_agent: str, origin: str = "") -> str:
    """Which KIND of client opened a tunnel -- the APK's WebView, the desktop app, or a browser. The platform only,
    never the device or the person: enough to say which build churns a far relay. The ORIGIN decides first: the
    desktop app strips Electron/ from its user agent on purpose (Google sign-in), so by UA alone it reads as a
    browser; it loads from app://posterchan, the APK from https://localhost (Capacitor)."""
    o = str(origin or "").lower()
    if o.startswith("app://"):
        return "desktop"
    if o in ("https://localhost", "http://localhost", "capacitor://localhost"):
        return "apk"
    ua = str(user_agent or "")
    if "Electron/" in ua:
        return "desktop"
    if "; wv)" in ua or "PosterChanApp" in ua:
        return "apk"
    return "browser" if ua else "?"


def closed(url: str, first_frame: str, seconds: float, ended_by: str = "", client: str = "") -> None:
    """A dialled tunnel ended. The SHORT ones are the churn a far relay sees: one websocket per read or send. The
    long ones are subscriptions; when they are re-dialled again and again, who ended them and after how long is what
    says why (2026-10-28: 61 of jskitty's 69 dials in ten minutes were long)."""
    h = _host(url)
    if seconds >= SHORT_TUNNEL_S:
        k = (h, ended_by or "?", frame_shape(first_frame) + (" [%s]" % client if client else ""))
        _long.setdefault(k, []).append(seconds)
        return
    _short[h] = _short.get(h, 0) + 1
    key = (h, frame_shape(first_frame))
    _shapes[key] = _shapes.get(key, 0) + 1


def acquire(pk: str) -> bool:
    global _total
    if _total >= GLOBAL_MAX or _open.get(pk, 0) >= PER_MEMBER:
        refused(pk, "node full" if _total >= GLOBAL_MAX else "member share full")
        return False
    _open[pk] = _open.get(pk, 0) + 1
    _total += 1
    return True


def release(pk: str) -> None:
    global _total
    n = _open.get(pk, 0) - 1
    if n > 0:
        _open[pk] = n
    else:
        _open.pop(pk, None)
    _total = max(0, _total - 1)


def _host(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def tor_hops() -> list:
    """socks5h URLs of this node's Tor daemons, in the proxy's order; [] when it runs none (or cannot say)."""
    try:
        from app.services.http_proxy_service import socks_backends
        return [f"socks5h://127.0.0.1:{int(port)}" for port, _label in socks_backends()]
    except Exception:
        return []


def egress_hops(url: str, tors=None) -> list:
    """The order to dial `url`: every Tor daemon (the one that last worked for this host first), then direct (None).
    `tors` is the relay process's list (from its config); None reads this process's settings."""
    tors = list(tor_hops() if tors is None else tors)
    good = _good.get(_host(url))
    if good and good[1] > time.monotonic() and good[0] in tors:
        tors.remove(good[0])
        tors.insert(0, good[0])
    return tors + [None]


def unreachable(url: str) -> bool:
    """True while `url`'s host is inside its UNREACHABLE_FOR window."""
    until = _unreachable.get(_host(url))
    if until is None:
        return False
    if until <= time.monotonic():
        _unreachable.pop(_host(url), None)
        return False
    return True


def all_hops_failed(url: str) -> None:
    host = _host(url)
    n = _strikes[host] = _strikes.get(host, 0) + 1
    _unreachable[host] = time.monotonic() + min(UNREACHABLE_MAX, UNREACHABLE_FOR * 2 ** (n - 1))
    if len(_unreachable) > 4096:
        now = time.monotonic()
        for h in [h for h, t in _unreachable.items() if t <= now]:
            _unreachable.pop(h, None)
            _strikes.pop(h, None)


def hop_result(url: str, hop: str | None, ok: bool) -> None:
    """Remember the Tor circuit that reached `url`'s host; forget it when it stops working."""
    host = _host(url)
    if ok:
        _strikes.pop(host, None)                     # reached by any hop: the next failure starts the pause over
        _reached[host] = time.monotonic() + GOOD_FOR
        if len(_reached) > 4096:
            now = time.monotonic()
            for h in [h for h, t in _reached.items() if t <= now]:
                _reached.pop(h, None)
    if not hop:
        return
    if ok:
        _good[host] = (hop, time.monotonic() + GOOD_FOR)
        if len(_good) > 4096:                        # bounded: drop what has expired
            now = time.monotonic()
            for h in [h for h, (_, t) in _good.items() if t <= now]:
                _good.pop(h, None)
    elif _good.get(host, ("",))[0] == hop:
        _good.pop(host, None)


def open_timeout(proxy: str | None) -> float:
    return TOR_OPEN_TIMEOUT if proxy else OPEN_TIMEOUT


def connect(url: str, proxy: str | None):
    """The outbound socket, through the fallback proxy, or direct with `proxy=None` (patched in tests)."""
    from app.services.nostr import peer_ua   # PosterChan/Server, dropped for a relay that refuses it (jskitty: 403)
    return peer_ua.connect(url, proxy=proxy or None, open_timeout=open_timeout(proxy), max_size=MAX_FRAME,
                           ping_interval=None)


async def pipe(client, upstream, info: dict | None = None) -> None:
    """Frames both ways, unchanged, until either side closes. `client` is a Starlette WebSocket.
    `info` (a dict) receives `first` (the client's first frame) and `ended_by` ("relay"/"client") for `closed()`."""
    async def down():
        async for msg in upstream:
            await client.send_text(msg if isinstance(msg, str) else msg.decode("utf-8", "replace"))

    async def up():
        while True:
            data = await client.receive_text()
            if len(data) > MAX_FRAME:
                return
            if info is not None and "first" not in info:
                info["first"] = data
            await upstream.send(data)
    tasks = [asyncio.ensure_future(down()), asyncio.ensure_future(up())]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        if info is not None:
            # Who ended it, for closed(): the far relay (its stream finished) or the member's client.
            info["ended_by"] = "relay" if tasks[0].done() else "client"
    finally:
        for t in tasks:
            t.cancel()


def _known_to_answer(host: str) -> bool:
    t = _reached.get(host)
    return bool(t and t > time.monotonic())


async def _dial_chain(target: str):
    """Every hop in order (egress_hops); the first that opens wins. (context manager, socket), or (None, None)."""
    for hop in egress_hops(target):
        cm = None
        try:
            cm = connect(target, hop)
            up = await asyncio.wait_for(cm.__aenter__(), timeout=open_timeout(hop) + 1)
            hop_result(target, hop, True)
            dialled(target)
            return cm, up
        except Exception as e:
            hop_result(target, hop, False)
            logger.info("[concord-tunnel] could not reach %s %s: %s", _host(target),
                        "through Tor " + hop.rsplit(":", 1)[-1] if hop else "directly", type(e).__name__)
    if not unreachable(target):
        all_hops_failed(target)
    return None, None


async def open_upstream(target: str):
    """The far relay's socket for a tunnel, or (None, None) when no hop reaches it (or it is inside its pause).

    A host nothing has reached lately is dialled by ONE tunnel at a time: the others wait for that attempt and then
    either dial in parallel (it got through) or answer "unreachable" without dialling (it did not)."""
    host = _host(target)
    if unreachable(target):
        return None, None
    if not _known_to_answer(host):
        lock = _dialing.get(host)
        if lock is None:
            lock = _dialing[host] = asyncio.Lock()
            if len(_dialing) > 4096:
                for h in [h for h, lk in _dialing.items() if not lk.locked() and h != host]:
                    _dialing.pop(h, None)
        async with lock:
            if unreachable(target):
                return None, None
            if not _known_to_answer(host):
                return await _dial_chain(target)
    return await _dial_chain(target)
