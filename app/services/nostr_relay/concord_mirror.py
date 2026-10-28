"""CONCORD ON THIS NODE'S RELAY — the server holds a member's communities, the way it holds Telegram.

A Concord (CORD) community lives on the relays its invite names. A member on an instance used to talk
to every one of them from every device: eight sockets per room, a room that goes blank when one of
them rate-limits a phone, and a client whose address every community relay learns. This module lets
the node's own relay stand in for them:

  * A member's client REGISTERS a community it has joined (an ephemeral kind-27059 event, signed by
    the member, sent only to this relay, never stored or fanned out). The registration names exactly
    two things: the community's relay URLs and the derived STREAM PUBKEYS whose kind-1059 wraps carry
    it. That is precisely what every one of those relays already learns from any member's REQ — the
    node learns no community id, no name, no key, nothing it could decrypt with. The `group` label is
    an opaque hash the client chooses, used only so "leave" can name what it is leaving.
  * The relay keeps ONE connection per upstream relay (deduplicated across every registration), asks
    it for those authors' kind-1059 history and live wraps, and STORES THEM AS THEY ARRIVED — opaque,
    byte-for-byte, signature-checked and nothing else. Nothing here imports a decryption routine, and
    nothing ever could: the keys are not on this machine.
  * A member's post goes to this relay; the relay stores it, fans it out and FORWARDS it to the
    community's relays, and only then answers OK — true when an upstream relay took it, false (with
    the event still stored here) when none did, so the client knows to deliver it itself.

THE RULE THAT KEEPS IT HONEST: "could not ask" is never "empty". A REQ for a registered stream that
this node is not currently in step with — no upstream connected, history not yet read, an upstream
that refused us (auth-required), a community over its cap — is answered CLOSED `unavailable:`, never
EOSE. The client then reads the community's own relays exactly as it did before. A stream nobody
registered is not this module's business at all: it is served from the store as always (that is how
communities CREATED on this node, whose only relay is this one, are read).

WHO MAY REGISTER: an account of this instance (`gate.is_operator`) — the same set this relay already
treats as its own users. A stranger cannot make this node open connections anywhere.

OUTBOUND CONNECTIONS go through the node's HTTP proxy FALLBACK listener (`proxy_fallback_port`, Tor 1 →
Tor 2 → direct, the direct leg itself refusing private addresses), never the Tor-only :8118 that
torrents use and never a raw socket. With no proxy configured the mirror does not run, and every
registration is refused with a reason, so clients keep reading the community relays themselves.
Every relay URL is also judged before it is stored: wss:// on 443, a public host (rss_service's SSRF
guard), not on the relay's blocklist, not this node itself.

BUDGET: per registration ≤8 relays and ≤128 streams; ≤20 registrations per member, ≤500 in all, ≤64
distinct upstream relays; ≤25,000 stored wraps per community and ≤250,000 in all. A community over
its budget stops being mirrored (and says so: its REQs are refused, not quietly truncated) instead of
filling the store. A member who leaves, or a registration nobody has refreshed for 14 days, stops the
mirror and removes the copies (only copies: a stream registered here is by construction one whose
home is elsewhere).

THE COMMUNITY DIRECTORY rides the same connection path: Concord's Discover list is a pair of NIP-50
searches for invite notes on a directory relay. This relay answers exactly those two searches from a
copy it fetched itself (kept in memory, never in the store — they are strangers' kind-1 notes and do
not belong in this node's timeline), and refuses them with `unavailable:` until it has one.
"""
from __future__ import annotations

import asyncio
import collections
import json
import logging
import random
import re
import time
import uuid
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

KIND_REGISTER = 27059          # ephemeral range: never stored, and intercepted before any fan-out
WRAP_KIND = 1059
LIVE_KIND = 21059              # CORD-07 presence/typing: relayed live, never stored
STREAM_KINDS = frozenset({WRAP_KIND, LIVE_KIND})

DIRECTORY_RELAYS = ("wss://relay.dreamith.to",)
DIRECTORY_SEARCHES = ("armada.buzz/invite", "poster.place/invite")

MAX_RELAYS_PER_REGISTRATION = 8
MAX_AUTHORS_PER_REGISTRATION = 128
MAX_REGISTRATIONS_PER_MEMBER = 20
MAX_REGISTRATIONS = 500
MAX_UPSTREAM_RELAYS = 64
MAX_EVENTS_PER_COMMUNITY = 25_000
MAX_EVENTS_TOTAL = 250_000
MAX_DROPPED = 20_000          # streams remembered as "copy deleted here" (see ConcordMirror.dropped)
MAX_WRAP_BYTES = 128 * 1024
TTL_SECONDS = 14 * 86400
REGISTER_SKEW = 600
REGISTRATIONS_PER_HOUR = 60
FORWARD_TIMEOUT = 4.0
HISTORY_PAGE = 500
HISTORY_PAGES_MAX = 200
AUTHOR_CHUNK = 250
MAX_CHUNKS = 20
LINK_STOP_WAIT = 5.0      # how long anything waits for an old link to finish; after that it is left to die alone
DIRECTORY_FRESH = 600
DIRECTORY_STALE = 6 * 3600
DIRECTORY_MAX = 400
DIRECTORY_FIRST_WAIT = 8.0
KV_KEY = "concord_mirror_v1"
# A REQ whose subscription id starts with this comes from a client that reads, itself, the relays the registration
# reply listed after READ_DIRECTLY -- so this node serves it what it holds even where one of them refused us.
PARTIAL_SUB_PREFIX = "pcmp"
READ_DIRECTLY = "read directly:"
REGISTER_SETTLE_SECONDS = 2.5
# FEDERATION OF COMMUNITIES HOSTED HERE (2026-10-21: "make sure posterchan room federates"). A community created on
# this node lists ONE relay -- this one -- so it lived nowhere else: lose this relay and it is gone, for us and for
# every other app. A member's client asks for it to be REPLICATED (`op: replicate`, the same signed member-only
# registration, naming only its stream pubkeys); this node then copies every wrap of those streams to these relays
# and reads back anything posted there. Opaque copies -- the node holds no key and decrypts nothing -- and the
# client still talks to this relay alone, so a phone does no extra work. Chosen from the CORD relays this mirror
# already syncs both ways. 2026-10-26: jskitty, relay.ditto.pub and asia.vectorapp.io by default (asked for by name)
# plus relay.dreamith.to (Discover's relay) -- four, the most a community's relay list may carry beside this one. Each
# was measured answering an author-scoped kind-1059 read, which is the read the backfill walk makes: the note that
# asia.vectorapp.io "gates kind-1059 reads behind an AUTH" described a FILTER without authors, which jskitty refuses
# just the same. nostr.computingcache.com is still read (CORD_RELAYS), it just no longer fits the backup list.
BACKUP_RELAYS = ("wss://jskitty.com/nostr", "wss://relay.ditto.pub", "wss://asia.vectorapp.io/nostr", "wss://relay.dreamith.to")
BACKFILL_PAGE = 500
BACKFILL_PACE = 0.05        # seconds between pushed wraps: ~20/s per backup, so a long history is no burst

_HEX64 = re.compile(r"^[0-9a-f]{64}$")


class Unavailable(Exception):
    """The mirror cannot reach anything (switched off, no proxy). Never "nothing there"."""


def _host_blocked(host: str, domains) -> bool:
    host = (host or "").lower().rstrip(".")
    for d in domains or ():
        d = str(d or "").lower().strip().lstrip(".")
        if d and (host == d or host.endswith("." + d)):
            return True
    return False


def _own_hosts(cfg: dict) -> set:
    out = set()
    for o in cfg.get("posterchan_origins") or ():
        h = (urlparse(str(o)).hostname or "").lower()
        if h and h not in ("localhost",):
            out.add(h)
    d = str(cfg.get("nip05_domain") or "").strip().lstrip("@").lower()
    if d:
        out.add(d)
    return out


BLOCKLISTED = "relay is on this node's blocklist"


def default_url_ok(url: str, cfg: dict) -> str:
    """'' when `url` may be dialled for a member, else the reason it may not. Blocking (DNS) — call
    it off the event loop."""
    from app.services.rss_service import is_safe_host
    u = str(url or "").strip()
    p = urlparse(u)
    if p.scheme != "wss" or not p.hostname:
        return "only wss:// relays are mirrored"
    if p.port not in (None, 443):
        return "only relays on port 443 are mirrored"
    host = p.hostname.lower()
    from app.services.nostr.peer_ua import never_dial
    if never_dial(u):
        return "this node never dials that relay (rate-limited)"
    if _host_blocked(host, cfg.get("blocked_relays")):
        return BLOCKLISTED
    if host in _own_hosts(cfg):
        return "that is this node's own relay"
    if not is_safe_host("https://" + host):
        return "relay host is not a public address"
    return ""


def normalize_relay(url: str) -> str:
    u = str(url or "").strip()
    if not u:
        return ""
    p = urlparse(u)
    if not p.scheme or not p.netloc:
        return ""
    path = p.path or ""
    if path == "/":
        path = ""
    return f"{p.scheme.lower()}://{p.netloc.lower()}{path}"


class _TorThenDirect:
    """`async with` a websocket: every Tor daemon in the proxy's order, then direct -- concord_tunnel's order."""

    def __init__(self, url: str, proxy: str, tors=None):
        self.url, self.proxy, self.tors, self._cm = url, proxy, tors, None

    async def __aenter__(self):
        from app.services import concord_tunnel as tun
        from app.services.nostr import peer_ua   # PosterChan/Server, dropped for a relay that refuses it (jskitty: 403)
        last = None
        # Tor 1, Tor 2, ... then direct: the tunnel's order and memory (concord_tunnel.egress_hops). With no proxy
        # configured this node dials direct, as before.
        # `tors`: this node's Tor daemons from the relay's config; without that list, the fallback proxy listener.
        hops = tun.egress_hops(self.url, self.tors or [self.proxy]) if self.proxy else [None]
        for hop in hops:
            cm = peer_ua.connect(self.url, proxy=hop or None, open_timeout=tun.open_timeout(hop),
                                 max_size=4 * 1024 * 1024, ping_interval=None)
            try:
                ws = await cm.__aenter__()
            except Exception as e:
                tun.hop_result(self.url, hop, False)
                last = e
                continue
            tun.hop_result(self.url, hop, True)
            self._cm = cm
            return ws
        raise last or ConnectionError("unreachable")

    async def __aexit__(self, *exc):
        if self._cm is not None:
            return await self._cm.__aexit__(*exc)
        return False


class _Link:
    """One upstream relay: ONE socket, the history of every registered stream it carries, then live."""

    def __init__(self, mirror: "ConcordMirror", url: str, authors):
        self.mirror = mirror
        self.url = url
        self.authors = frozenset(authors)
        self.connected = False
        self.synced = False          # every chunk's history read to the end on THIS connection
        self.refused = ""            # the upstream refused our subscription (e.g. auth-required)
        self.last_error = ""
        self.ws = None
        self.task = None
        self.stopped = False
        self.pending: dict = {}      # event id -> Future[bool] (an OK we are waiting for)
        self.held: set = set()       # wrap ids this upstream showed us on this connection (bounded: one history walk)
        self.backfilled = 0

    def start(self) -> None:
        self.task = asyncio.create_task(self._run())

    async def stop(self) -> None:
        """Cancel, and wait a BOUNDED time. A task that lost its cancellation (asyncio.wait_for on 3.11 can swallow
        one that lands as the awaited call completes) must not hold its caller -- the reconcile lock, i.e. every
        registration on the node -- for ever: measured 2026-10-14, nine minutes of nothing, and the busiest
        relays left with no link. `stopped` makes such a task leave at its next turn instead of reconnecting.
        The caller's own cancellation is NOT swallowed here any more (it used to be, by `except BaseException`)."""
        self.stopped = True
        t, self.task = self.task, None
        if t:
            t.cancel()
            await asyncio.wait({t}, timeout=LINK_STOP_WAIT)
        self._settle_pending()
        self.connected = self.synced = False

    def _settle_pending(self) -> None:
        for fut in list(self.pending.values()):
            if not fut.done():
                fut.set_result(False)
        self.pending.clear()

    @property
    def ready(self) -> bool:
        return self.connected and self.synced and not self.refused

    async def _run(self) -> None:
        backoff = 2.0
        while not self.stopped:
            opened = time.monotonic()
            try:
                async with self.mirror._open(self.url) as ws:
                    self.ws = ws
                    self.connected = True
                    self.refused = ""
                    self.last_error = ""
                    await self._session(ws)
            except asyncio.CancelledError:
                raise
            except Exception as e:                       # a drop, a refused CONNECT, a timeout
                self.last_error = f"{type(e).__name__}: {e}"[:200]
                logger.debug("[concord-mirror] %s dropped: %s", self.url, self.last_error)
            finally:
                self.ws = None
                self.connected = False
                self.synced = False
                self._settle_pending()
            if time.monotonic() - opened >= 60:
                backoff = 2.0
            await asyncio.sleep(backoff + random.uniform(0, backoff))
            backoff = min(backoff * 2, 300.0)

    async def _session(self, ws) -> None:
        self.held = set()          # what THIS connection's history walk shows: a backup that lost data is refilled
        authors = sorted(self.authors)
        chunks = [authors[i:i + AUTHOR_CHUNK] for i in range(0, len(authors), AUTHOR_CHUNK)][:MAX_CHUNKS]
        now = int(time.time())
        live, wrap_subs = set(), set()
        for ch in chunks:
            for flt in ({"kinds": [WRAP_KIND], "authors": ch, "since": now - 120},
                        {"kinds": [LIVE_KIND], "authors": ch, "since": now - 60}):
                sid = "l" + uuid.uuid4().hex[:12]
                live.add(sid)
                if flt["kinds"] == [WRAP_KIND]:
                    wrap_subs.add(sid)
                await ws.send(json.dumps(["REQ", sid, flt]))
        # HISTORY: one chunk at a time, paged backwards to the beginning. Walked in full on every
        # connection — "the newest page is already here" says nothing about the pages behind it when
        # the last session was cut off half-way, and a mirror that stops there serves a room with a
        # hole in it as if it were whole. Bounded by HISTORY_PAGES_MAX and by the community budget.
        todo = list(chunks)
        hist = {"sid": None, "chunk": None, "until": None, "got": 0, "oldest": None, "pages": 0}

        async def next_page():
            if hist["sid"]:
                try:
                    await ws.send(json.dumps(["CLOSE", hist["sid"]]))
                except Exception:
                    pass
            done = (hist["chunk"] is None or hist["got"] == 0 or hist["pages"] >= HISTORY_PAGES_MAX
                    or self.mirror._chunk_full(hist["chunk"]))
            if done:
                if not todo:
                    hist["sid"] = None
                    self.synced = not self.refused
                    if self.synced:
                        self.mirror._schedule_backfill(self)
                    return
                hist.update(chunk=todo.pop(0), until=None, pages=0)
            else:
                oldest = hist["oldest"]
                hist["until"] = oldest if hist["until"] is None or oldest < hist["until"] else oldest - 1
            flt = {"kinds": [WRAP_KIND], "authors": hist["chunk"], "limit": HISTORY_PAGE}
            if hist["until"] is not None:
                flt["until"] = hist["until"]
            hist.update(sid="h" + uuid.uuid4().hex[:12], got=0, oldest=None)
            hist["pages"] += 1
            wrap_subs.add(hist["sid"])
            await ws.send(json.dumps(["REQ", hist["sid"], flt]))

        await next_page()
        while not self.stopped:
            # asyncio.timeout, NOT asyncio.wait_for: on Python 3.11 wait_for can swallow a cancellation that arrives
            # as recv() completes -- on a relay that streams constantly that is not rare, and the link then never
            # stops (see stop()).
            try:
                async with asyncio.timeout(45):
                    raw = await ws.recv()
            except TimeoutError:
                async with asyncio.timeout(10):
                    await ws.ping()
                continue
            try:
                msg = json.loads(raw)
            except (ValueError, TypeError):
                continue
            if not isinstance(msg, list) or not msg:
                continue
            typ = msg[0]
            if typ == "EVENT" and len(msg) >= 3 and (msg[1] in live or msg[1] == hist["sid"]):
                if isinstance(msg[2], dict) and isinstance(msg[2].get("id"), str) and len(self.held) < 300_000:
                    self.held.add(msg[2]["id"])
                await self.mirror._ingest(msg[2], self)
                if msg[1] == hist["sid"] and isinstance(msg[2], dict):
                    try:
                        ca = int(msg[2].get("created_at"))
                    except (TypeError, ValueError):
                        continue
                    hist["got"] += 1
                    hist["oldest"] = ca if hist["oldest"] is None else min(hist["oldest"], ca)
            elif typ == "EOSE" and len(msg) >= 2 and msg[1] == hist["sid"]:
                await next_page()
            elif typ == "CLOSED" and len(msg) >= 2 and msg[1] in wrap_subs:
                # A refused WRAP subscription (auth-required is the common one: a relay that gates
                # gift wraps behind an AUTH as the stream key, which this node does not hold and must
                # not). Never "in step" on this connection; readers are told to go direct.
                if not self.refused:
                    self.refused = str(msg[2] if len(msg) > 2 else "closed")[:200] or "closed"
                    logger.info("[concord-mirror] %s refused the mirror subscription: %s", self.url, self.refused)
                self.synced = False
                if msg[1] == hist["sid"]:
                    hist["sid"] = None
            elif typ == "OK" and len(msg) >= 3:
                fut = self.pending.pop(str(msg[1]), None)
                if fut is not None and not fut.done():
                    fut.set_result(bool(msg[2]))

    async def publish(self, ev: dict) -> bool:
        ws = self.ws
        if ws is None or not self.connected:
            return False
        fut = asyncio.get_running_loop().create_future()
        self.pending[ev["id"]] = fut
        try:
            await ws.send(json.dumps(["EVENT", ev]))
            return await asyncio.wait_for(asyncio.shield(fut), timeout=FORWARD_TIMEOUT)
        except Exception:
            return False
        finally:
            if self.pending.get(ev["id"]) is fut:
                self.pending.pop(ev["id"], None)

    async def send_ephemeral(self, ev: dict) -> None:
        ws = self.ws
        if ws is not None and self.connected:
            try:
                await ws.send(json.dumps(["EVENT", ev]))
            except Exception:
                pass

    def status(self) -> dict:
        return {"relay": self.url, "streams": len(self.authors), "connected": self.connected,
                "synced": self.synced, "refused": self.refused, "error": self.last_error,
                **({"backfilled": self.backfilled} if self.backfilled else {})}


class _Directory:
    def __init__(self, mirror: "ConcordMirror"):
        self.mirror = mirror
        self.events: dict = {}
        self.ok_at = 0.0
        self.tried_at = 0.0
        self.error = ""
        self._fetch_task = None

    @staticmethod
    def is_directory_req(filters) -> bool:
        if not filters:
            return False
        for f in filters:
            if not isinstance(f, dict) or str(f.get("search") or "") not in DIRECTORY_SEARCHES:
                return False
            if [int(k) for k in (f.get("kinds") or []) if str(k).isdigit()] != [1]:
                return False
        return True

    def _start_fetch(self):
        if self._fetch_task is None or self._fetch_task.done():
            self._fetch_task = asyncio.create_task(self._fetch())
        return self._fetch_task

    async def answer(self, filters) -> tuple:
        """(events, '') or (None, reason). Never an empty answer that was not read."""
        if not self.mirror.enabled():
            return None, "unavailable: this node does not mirror the Concord community directory"
        now = time.time()
        if now - self.ok_at > DIRECTORY_FRESH:
            task = self._start_fetch()
            if not self.ok_at:
                try:
                    await asyncio.wait_for(asyncio.shield(task), timeout=DIRECTORY_FIRST_WAIT)
                except Exception:
                    pass
        if not self.ok_at or time.time() - self.ok_at > DIRECTORY_STALE:
            return None, ("unavailable: the community directory could not be reached from this node"
                          + (f" ({self.error})" if self.error else ""))
        out = {}
        for f in filters:
            needle = str(f.get("search") or "").lower()
            lim = max(1, min(int(f.get("limit") or 100), DIRECTORY_MAX))
            hits = sorted((e for e in self.events.values() if needle in str(e.get("content", "")).lower()),
                          key=lambda e: (e.get("created_at", 0), e.get("id", "")), reverse=True)[:lim]
            for e in hits:
                out[e["id"]] = e
        return sorted(out.values(), key=lambda e: (e.get("created_at", 0), e.get("id", "")), reverse=True), ""

    async def _fetch(self) -> None:
        from app.services.nostr.event import verify_event
        self.tried_at = time.time()
        filters = [{"kinds": [1], "search": s, "limit": 200} for s in DIRECTORY_SEARCHES]
        last = ""
        for url in DIRECTORY_RELAYS:
            got, eosed = {}, False
            try:
                async with self.mirror._open(url) as ws:
                    sid = "d" + uuid.uuid4().hex[:12]
                    await ws.send(json.dumps(["REQ", sid, *filters]))
                    deadline = time.monotonic() + 15
                    while time.monotonic() < deadline:
                        raw = await asyncio.wait_for(ws.recv(), timeout=max(0.1, deadline - time.monotonic()))
                        try:
                            msg = json.loads(raw)
                        except (ValueError, TypeError):
                            continue
                        if not isinstance(msg, list) or len(msg) < 2 or msg[1] != sid:
                            continue
                        if msg[0] == "EVENT" and len(msg) >= 3 and isinstance(msg[2], dict):
                            ev = msg[2]
                            if ev.get("kind") == 1 and "/invite/" in str(ev.get("content", "")) \
                                    and len(json.dumps(ev)) <= MAX_WRAP_BYTES and verify_event(ev):
                                got[ev["id"]] = ev
                        elif msg[0] == "EOSE":
                            eosed = True
                            break
                        elif msg[0] == "CLOSED":
                            last = str(msg[2] if len(msg) > 2 else "closed")[:120]
                            break
            except Exception as e:
                last = type(e).__name__
                continue
            if eosed:
                keep = sorted(got.values(), key=lambda e: (e.get("created_at", 0), e["id"]), reverse=True)[:DIRECTORY_MAX]
                self.events = {e["id"]: e for e in keep}
                self.ok_at = time.time()
                self.error = ""
                return
        self.error = last or "no answer"


class ConcordMirror:
    PARTIAL_SUB_PREFIX = PARTIAL_SUB_PREFIX
    def __init__(self, store, server=None, cfg: dict | None = None, *, url_ok=None, open_ws=None):
        self.store = store
        self.server = server
        self.cfg = cfg if cfg is not None else {}
        self._url_ok = url_ok or default_url_ok
        self._open_override = open_ws
        self.regs: dict = {}          # member pubkey -> {group: {"relays": [...], "authors": [...], "at": ts}}
        self.counts: dict = {}        # author -> wraps this mirror stored for it
        self.links: dict = {}         # relay url -> _Link
        self._stopping: set = set()   # old links being stopped in the background (kept so they are not collected)
        self.directory = _Directory(self)
        self._seen = collections.OrderedDict()   # wrap id -> its stream: "already here", forgotten with the copies
        self._recent_regs: dict = {}  # member -> deque of registration times
        self._loaded = False
        self._save_task = None
        self._sweep_task = None
        self._reconcile_lock = asyncio.Lock()
        self._last_named: set = set()  # every stream registered at the last reconcile (so a stopped one is noticed)
        # Streams of communities HOSTED here, ever: their wraps are this node's originals, never mirror copies, so they
        # are never deleted when a registration lapses, never counted against a budget, never refused on a read.
        self.hosted_ever: set = set()
        # Streams whose copy this node DELETED because no registration names them any more -> when. A device that
        # registered one still reads it here; it is told `unavailable:`, never an EOSE over the emptied store.
        self.dropped: dict = {}

    # -- configuration --------------------------------------------------------------------------------
    def enabled(self) -> bool:
        return bool(self.cfg.get("concord_mirror", True)) and bool(self._proxy() or self._open_override)

    def _proxy(self) -> str:
        return str(self.cfg.get("concord_mirror_proxy") or "")

    def _open(self, url: str):
        """A websocket to `url`: each of this node's Tor daemons in the proxy's order, then DIRECT, last.

        The proxy's own chain falls back only on a CONNECT error, never on a Tor dial that TIMES OUT or is refused,
        so each Tor daemon is a whole handshake here (concord_tunnel.egress_hops, "tor1 tor2 then default",
        2026-10-27). Still no mirror at all without a proxy (the operator's opt-in)."""
        if self._open_override is not None:
            return self._open_override(url)
        proxy = self._proxy()
        if not proxy:
            raise Unavailable("no outbound proxy configured")
        return _TorThenDirect(url, proxy, self.cfg.get("concord_mirror_tor") or None)

    # -- lifecycle --------------------------------------------------------------------------------------
    async def start(self) -> None:
        try:
            raw = await self.store.kv_get(KV_KEY)
            data = json.loads(raw) if raw else {}
            self.regs = {m: dict(g) for m, g in (data.get("regs") or {}).items() if isinstance(g, dict)}
            self.counts = {a: int(n) for a, n in (data.get("counts") or {}).items()}
            self.hosted_ever = {str(a) for a in (data.get("hosted") or []) if _HEX64.match(str(a))}
            self.dropped = {str(a): float(t) for a, t in (data.get("dropped") or {}).items() if _HEX64.match(str(a))}
        except Exception as e:
            logger.warning("[concord-mirror] could not read the saved registrations (%s) — starting empty", e)
        self._loaded = True
        self._last_named = self.registered_authors() | {a for g in self.regs.values() for r in g.values() for a in r.get("authors", ())}
        await self._expire()
        await self._reconcile()
        self._sweep_task = asyncio.create_task(self._sweeper())

    async def stop(self) -> None:
        if self._sweep_task:
            self._sweep_task.cancel()
        for link in list(self.links.values()):
            await link.stop()
        self.links.clear()
        await self._save_now()

    async def _sweeper(self) -> None:
        while True:
            await asyncio.sleep(3600)
            try:
                if await self._expire():
                    await self._reconcile()
            except Exception as e:
                logger.debug("[concord-mirror] sweep failed: %s", e)

    # -- the registry ---------------------------------------------------------------------------------
    def _active(self):
        now = time.time()
        for member, groups in self.regs.items():
            for group, reg in groups.items():
                if now - float(reg.get("at", 0)) <= TTL_SECONDS:
                    yield member, group, reg

    def hosted_authors(self) -> set:
        """Streams of communities hosted HERE that members asked to be replicated (and still refresh)."""
        return {a for _m, _g, reg in self._active() if reg.get("hosted") for a in reg.get("authors", ())}

    def is_hosted(self, author: str) -> bool:
        return author in self.hosted_ever

    def registered_authors(self) -> set:
        return {a for _m, _g, reg in self._active() for a in reg.get("authors", ())}

    def _relays_for(self, author: str) -> set:
        return {r for _m, _g, reg in self._active() if author in reg.get("authors", ()) for r in reg.get("relays", ())}

    def _desired(self) -> dict:
        want: dict = {}
        for _m, _g, reg in self._active():
            for r in reg.get("relays", ()):
                if r not in want and len(want) >= MAX_UPSTREAM_RELAYS:
                    continue
                want.setdefault(r, set()).update(reg.get("authors", ()))
        return want

    async def _expire(self) -> bool:
        now, gone = time.time(), False
        for member in list(self.regs):
            for group in list(self.regs[member]):
                if now - float(self.regs[member][group].get("at", 0)) > TTL_SECONDS:
                    del self.regs[member][group]
                    gone = True
            if not self.regs[member]:
                del self.regs[member]
        if gone:
            self._dirty()
        return gone

    async def _reconcile(self) -> None:
        async with self._reconcile_lock:
            want = self._desired() if self.enabled() else {}
            # REPLACE FIRST, STOP AFTER, AND NEVER WAIT ON IT HERE. The old link used to be popped and AWAITED
            # before its replacement existed; one that would not stop left its relay with no link and this lock
            # held -- every registration on the node stuck behind it (poster.place 2026-10-14 21:37-21:46).
            stale = []
            for url in list(self.links):
                if url not in want or self.links[url].authors != frozenset(want[url]):
                    stale.append(self.links.pop(url))
            for url, authors in want.items():
                if url not in self.links:
                    link = _Link(self, url, authors)
                    self.links[url] = link
                    link.start()
            for old in stale:
                old.stopped = True
                t = asyncio.create_task(old.stop())
                self._stopping.add(t)
                t.add_done_callback(self._stopping.discard)
            # Streams nobody registers any more: their copies go, and so does their count.
            named = self.registered_authors()
            dropped = [a for a in (set(self.counts) | self._last_named) if a not in named and a not in self.hosted_ever]
            self._last_named = set(named)
            # Named again (or expired from memory): no longer "stopped". Bounded by age and by count.
            cutoff = time.time() - TTL_SECONDS
            for a in [a for a, t in self.dropped.items() if a in named or t < cutoff]:
                del self.dropped[a]
            while len(self.dropped) > MAX_DROPPED:
                del self.dropped[min(self.dropped, key=self.dropped.get)]
            if dropped:
                # WHAT IS DELETED IS NO LONGER "ALREADY HERE". `_seen` let a history walk skip a wrap this process
                # had stored or found stored; with the copies gone it made the next walk skip every one of them, so
                # a stream dropped by one registration and named again by the next was served EMPTY, as if whole.
                # Sorsmenedék, poster.place 2026-10-22: a device registered its 4 control streams, then 11 s later
                # all 22 -- 5 of ~200 wraps held here until the relay restarted, every channel blank.
                gone = set(dropped)
                for eid in [e for e, a in self._seen.items() if a in gone]:
                    del self._seen[eid]
                now = time.time()
                for a in dropped:
                    self.dropped[a] = now
                try:
                    await self.store.delete_pubkeys(dropped, spare_preserved=True)
                except Exception as e:
                    logger.warning("[concord-mirror] could not remove %d stopped stream(s): %s", len(dropped), e)
                for a in dropped:
                    self.counts.pop(a, None)
                self._dirty()

    def _dirty(self) -> None:
        if self._save_task is None or self._save_task.done():
            self._save_task = asyncio.create_task(self._save_soon())

    async def _save_soon(self) -> None:
        await asyncio.sleep(2)
        await self._save_now()

    async def _save_now(self) -> None:
        if not self._loaded:
            return
        try:
            await self.store.kv_set(KV_KEY, json.dumps({"regs": self.regs, "counts": self.counts,
                                                        "hosted": sorted(self.hosted_ever),
                                                        "dropped": self.dropped}))
        except Exception as e:
            logger.warning("[concord-mirror] could not save the registrations: %s", e)

    # -- budgets --------------------------------------------------------------------------------------
    def total(self) -> int:
        return sum(self.counts.values())

    def _community_count(self, reg: dict) -> int:
        return sum(self.counts.get(a, 0) for a in reg.get("authors", ()))

    def _author_full(self, author: str) -> bool:
        if self.total() >= MAX_EVENTS_TOTAL:
            return True
        return any(author in reg.get("authors", ()) and self._community_count(reg) >= MAX_EVENTS_PER_COMMUNITY
                   for _m, _g, reg in self._active())

    def _chunk_full(self, chunk) -> bool:
        return bool(chunk) and all(self._author_full(a) for a in chunk)

    def author_ready(self, author: str, partial: bool = False) -> bool:
        if self._author_full(author):
            return False
        # A relay that REFUSED our subscription (auth-required) holds what it holds and we will never see it: serving
        # the room from this copy because ANOTHER relay synced answered members with whatever was posted only there
        # missing ("isolated to this room", poster.place 2026-10-13). Refused = could not ask = unavailable.
        # UNLESS the reader said it reads those relays itself (`partial`, see req_refusal): then this copy is half of
        # the answer and the refused relay the other half -- not "read all eight of the community's relays".
        for url in ([] if partial else self._relays_for(author)):
            link = self.links.get(url)
            if link is not None and author in link.authors and link.refused:
                return False
        for url in self._relays_for(author):
            link = self.links.get(url)
            if link is not None and author in link.authors and link.ready:
                return True
        return False

    # -- what the relay server asks -------------------------------------------------------------------
    def req_refusal(self, filters, partial: bool = False) -> str:
        """'' to serve this REQ from the store, else the CLOSED reason. Only REQs that ask for a
        REGISTERED stream are ever refused, and only while this node is not in step with it.

        `partial`: the subscription id starts with PARTIAL_SUB_PREFIX, which only a client that READS THE RELAYS
        THIS NODE CANNOT ITSELF sends (the registration reply names them: `unreadable()`). For it a relay that
        refused the mirror does not make the stream unavailable. Measured 2026-10-14: two of the owner's
        communities list relay.damus.io, which gates kind-1059 behind an AUTH as the stream key; the strict
        answer sent each device to ALL of those communities' relays, through the tunnel, once a minute per
        stream (~64 tunnels a minute per device) to cover the one relay the mirror could not read."""
        named = None
        for f in filters or ():
            if not isinstance(f, dict):
                continue
            authors = f.get("authors") or ()
            kinds = f.get("kinds")
            if not authors or (kinds and not (set(int(k) for k in kinds if str(k).lstrip("-").isdigit()) & STREAM_KINDS)):
                continue
            if named is None:
                named = self.registered_authors()
                if not named and not self.dropped:
                    return ""
            for a in authors:
                if a in self.hosted_ever:
                    continue        # this relay IS the community's home: what it holds is the answer
                if a in named and not self.author_ready(a, partial):
                    return ("unavailable: this node is still catching up with this community's relays "
                            "(or cannot reach them) — read them directly")
                if a not in named and a in self.dropped:
                    # Its copy was deleted here: whatever the store holds now is not the room. (Sorsmenedék,
                    # 2026-10-22: one device's 4-stream registration emptied 18 channels another device read here.)
                    return ("unavailable: this node no longer mirrors this stream "
                            "— read the community's relays directly")
        return ""

    def member_state(self, member: str) -> dict:
        """{group: {authors, direct, at}} for one member's live registrations -- what that member's registration
        replies said, asked again by a device that has none (members-only: routers/concord_tunnel.py).

        A COLD DEVICE READS THIS NODE'S COPY AT ONCE (2026-10-22, Firefox over Tor: "no rooms are loading", every
        room tunnelling to every relay it lists). Which rooms this node mirrors lived only in the browser that had
        registered them, so a new device, a cleared cache, or Tor Browser (which forgets every session) knew of
        none and tunnelled everywhere until its own registrations came back. `direct` is None for a registration
        recorded before it was kept (the client then treats it as it did before). Called from the stats thread:
        reads a snapshot and never blocks the loop."""
        out = {}
        try:
            mine = dict(self.regs.get(member) or {})
        except RuntimeError:                       # resized mid-copy by the loop: the next ask sees it
            return {}
        now = time.time()
        for group, reg in mine.items():
            if not isinstance(reg, dict) or reg.get("hosted") or now - float(reg.get("at", 0)) > TTL_SECONDS:
                continue
            skipped = reg.get("skipped")
            out[group] = {"authors": list(reg.get("authors") or []), "at": int(reg.get("at", 0)),
                          "direct": None if skipped is None else list(skipped) + self.unreadable(reg.get("relays") or [])}
        return out

    def unreadable(self, relays) -> list:
        """The relays among `relays` this node does not read: one that refused the mirror, or none is held for."""
        out = []
        for url in relays:
            link = self.links.get(url)
            if link is None or link.refused:
                out.append(url)
        return out

    async def _settle(self, relays, seconds: float) -> None:
        """Wait (bounded) until each link has either refused or finished its history -- a link a registration has
        just (re)started has not had the chance to be refused yet, and the reply names the relays it cannot read."""
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            links = [self.links.get(u) for u in relays]
            if all(link is None or link.refused or link.synced for link in links):
                return
            await asyncio.sleep(0.2)

    def mirrors(self, author: str) -> bool:
        return author in self.registered_authors()

    # WHAT WAS ALREADY DELIVERED IS NOT SENT AGAIN. A member's app re-publishes the same signed wrap (a retry whose OK
    # was slow, a second device, the outbox replaying) and every copy used to go back out to every relay of the
    # community. One accepted delivery is remembered per id; a FAILED one is not, so a retry still goes out.
    _FORWARDED_MAX = 20000

    # DELIVERED IS PER RELAY. "At least one relay accepted it" used to mark a wrap done everywhere, so a relay whose link
    # timed out or was reconnecting never got it from the forward -- only from the slower history copy, minutes later
    # or not at all: measured 2026-10-22 by scripts/check_concord_federation_live.py, a message on ONE of a community's
    # three relays after 40-71s, the other two still without it a minute later. Armada members reading those relays
    # did not see PosterChan members' messages. Each relay that has not accepted is retried, a few times, in the
    # background; a member's re-publish only goes to the relays still missing it.
    _FORWARD_RETRIES = (5.0, 30.0)

    async def forward(self, ev: dict, _retrying: bool = False) -> bool:
        """Deliver a member's wrap to the community's relays. True when at least one accepted it."""
        eid = ev.get("id", "")
        done = self.__dict__.setdefault("_forwarded", collections.OrderedDict())
        wanted = [u for u in self._relays_for(ev.get("pubkey", "")) if u in self.links]
        accepted = done.get(eid) if eid else None
        if accepted is not None:
            done.move_to_end(eid)
        missing = [u for u in wanted if not accepted or u not in accepted]
        if not missing:
            return bool(accepted)
        results = await asyncio.gather(*(self.links[u].publish(ev) for u in missing), return_exceptions=True)
        now_ok = {u for u, r in zip(missing, results) if r is True}
        if eid and now_ok:
            done[eid] = (accepted or set()) | now_ok
            while len(done) > self._FORWARDED_MAX:
                done.popitem(last=False)
        # A relay that refused or timed out while another accepted: retried in the background, bounded. A wrap NO relay
        # took is the caller's failure to retry (as before) -- the background never owns it.
        if eid and not _retrying and len(now_ok) < len(missing) and (accepted or now_ok):
            self._retry_forward_later(ev)
        return bool(now_ok or accepted)

    def _retry_forward_later(self, ev: dict) -> None:
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        tasks = self.__dict__.setdefault("_forward_retries", set())
        if len(tasks) > 2000:                       # bounded: a long relay outage is the history copy's job
            return

        async def later():
            for delay in self._FORWARD_RETRIES:
                await asyncio.sleep(delay)
                await self.forward(ev, _retrying=True)
                have = (self.__dict__.get("_forwarded") or {}).get(ev.get("id", "")) or set()
                if all(u in have for u in self._relays_for(ev.get("pubkey", "")) if u in self.links):
                    return
        t = loop.create_task(later())
        tasks.add(t)
        t.add_done_callback(tasks.discard)

    async def forward_ephemeral(self, ev: dict) -> None:
        for u in self._relays_for(ev.get("pubkey", "")):
            link = self.links.get(u)
            if link is not None:
                await link.send_ephemeral(ev)

    async def register(self, ev: dict, *, is_local: bool) -> tuple:
        """Handle a signed kind-27059 registration (the caller has verified id + signature)."""
        if not is_local:
            return False, "restricted: only accounts on this instance can ask it to mirror a community"
        if not self.cfg.get("concord_mirror", True):
            return False, "unavailable: Concord mirroring is switched off on this node"
        if not self.enabled():
            return False, "unavailable: this node has no outbound proxy, so it cannot mirror communities"
        try:
            if abs(int(ev.get("created_at", 0)) - time.time()) > REGISTER_SKEW:
                return False, "invalid: registration is too old or from the future"
            body = json.loads(ev.get("content") or "{}")
        except (TypeError, ValueError):
            return False, "invalid: registration content is not JSON"
        member = ev["pubkey"]
        group = str(body.get("group") or "").lower()
        if not _HEX64.match(group):
            return False, "invalid: group must be 64 hex characters"
        now = time.time()
        recent = self._recent_regs.setdefault(member, collections.deque())
        while recent and now - recent[0] > 3600:
            recent.popleft()
        if len(recent) >= REGISTRATIONS_PER_HOUR:
            return False, "rate-limited: too many mirror registrations this hour"
        recent.append(now)
        if body.get("op") == "leave":
            if self.regs.get(member, {}).pop(group, None) is not None:
                if not self.regs[member]:
                    del self.regs[member]
                self._dirty()
                await self._reconcile()
            return True, "left: this community is no longer mirrored for you"
        authors = sorted({str(a).lower() for a in (body.get("authors") or []) if _HEX64.match(str(a).lower())})
        hosted = body.get("op") == "replicate"
        relays_in = [normalize_relay(r) for r in ((self.cfg.get("concord_backup_relays") or BACKUP_RELAYS) if hosted
                                                  else (body.get("relays") or []))]
        relays_in = list(dict.fromkeys(r for r in relays_in if r))
        if not authors:
            return False, "invalid: no stream pubkeys"
        if len(authors) > MAX_AUTHORS_PER_REGISTRATION:
            return False, f"blocked: a community may register at most {MAX_AUTHORS_PER_REGISTRATION} streams"
        if not relays_in:
            return False, "invalid: no relays"
        if len(relays_in) > MAX_RELAYS_PER_REGISTRATION:
            relays_in = relays_in[:MAX_RELAYS_PER_REGISTRATION]
        reasons, relays, skipped = [], [], []
        for r in relays_in:
            why = await asyncio.to_thread(self._url_ok, r, self.cfg)
            if why:
                reasons.append(f"{r}: {why}")
                # A relay the operator BLOCKED is one this node will not read, not one it cannot: the community's
                # other relays carry the same streams and are mirrored. Named under "read directly" it sent every
                # device to it through the tunnel, once per stream on every refresh, often for full history --
                # relay.ditto.pub, ~2,700 tunnels an hour from one account (2026-10-22). Only relays a CLIENT can
                # reach and this node cannot (an odd port, a refused subscription below) are read directly.
                if why != BLOCKLISTED:
                    skipped.append(r)
            else:
                relays.append(r)
        if not relays:
            return False, "blocked: none of this community's relays can be mirrored (" + "; ".join(reasons)[:300] + ")"
        mine = self.regs.get(member, {})
        if group not in mine and len(mine) >= MAX_REGISTRATIONS_PER_MEMBER:
            return False, f"blocked: at most {MAX_REGISTRATIONS_PER_MEMBER} mirrored communities per account"
        if group not in mine and sum(1 for _ in self._active()) >= MAX_REGISTRATIONS:
            return False, "blocked: this node mirrors as many communities as it can"
        known = set(self._desired())
        if len(known | set(relays)) > MAX_UPSTREAM_RELAYS and not set(relays) <= known:
            return False, "blocked: this node already mirrors from as many relays as it can"
        if self.total() >= MAX_EVENTS_TOTAL:
            return False, "blocked: this node's Concord mirror is full"
        self.regs.setdefault(member, {})[group] = {"relays": relays, "authors": authors, "at": int(now),
                                                   "skipped": skipped, **({"hosted": True} if hosted else {})}
        if hosted:
            self.hosted_ever.update(authors)
        self._dirty()
        await self._reconcile()
        logger.info("[concord-mirror] %s… %s %s…: %d stream(s) %s %s%s", member[:8], "replicates" if hosted else "mirrors",
                    group[:8], len(authors), "to" if hosted else "from",
                    ", ".join(relays), (" (not mirrored: " + "; ".join(reasons) + ")") if reasons else "")
        if hosted:
            return True, f"replicating: {len(authors)} stream(s) to {len(relays)} relay(s)"
        await self._settle(relays, REGISTER_SETTLE_SECONDS)
        direct = skipped + self.unreadable(relays)
        return True, (f"mirroring: {len(authors)} stream(s) from {len(relays)} relay(s)"
                      + (f"; {READ_DIRECTLY} " + " ".join(direct) if direct else ""))

    # -- hosted -> backups -------------------------------------------------------------------------------
    def _schedule_backfill(self, link: _Link) -> None:
        if not (self.hosted_authors() & link.authors):
            return
        t = asyncio.create_task(self._backfill(link))
        self._stopping.add(t)
        t.add_done_callback(self._stopping.discard)

    async def _backfill(self, link: _Link) -> int:
        """Copy every wrap of the hosted streams this backup does not hold, oldest first, at a steady pace.

        Run each time the link finishes reading the backup's history -- that walk is what tells us what it holds
        (`link.held`), so a backup that lost data, or one added later, is filled again. Stops when the link stops."""
        authors = sorted(self.hosted_authors() & link.authors)
        if not authors:
            return 0
        # Newest first, a page at a time, `until` = the oldest timestamp seen (INCLUSIVE, so wraps sharing that second
        # are not skipped); ids already collected are dropped, and a page that adds nothing new is the end.
        missing, seen, until = [], set(), None
        while True:
            flt = {"kinds": [WRAP_KIND], "authors": authors, "limit": BACKFILL_PAGE}
            if until is not None:
                flt["until"] = until
            try:
                page = await self.store.query([flt])
            except Exception as e:
                logger.debug("[concord-mirror] backfill read failed: %s", e)
                return 0
            fresh = [ev for ev in page if isinstance(ev, dict) and ev.get("id") not in seen]
            if not fresh:
                break
            for ev in fresh:
                seen.add(ev["id"])
                if ev["id"] not in link.held:
                    missing.append(ev)
            if len(page) < BACKFILL_PAGE:
                break
            until = min(int(ev.get("created_at", 0)) for ev in page)
        missing.sort(key=lambda ev: int(ev.get("created_at", 0)))
        sent = 0
        for ev in missing:
            if link.stopped or not link.connected:
                break
            if await link.publish(ev):
                link.held.add(ev["id"])
                sent += 1
                link.backfilled += 1
            await asyncio.sleep(BACKFILL_PACE)
        if sent:
            logger.info("[concord-mirror] %s: copied %d hosted wrap(s) it did not hold", link.url, sent)
        return sent

    # -- upstream -> store ------------------------------------------------------------------------------
    def _seen_add(self, eid: str, author: str) -> None:
        self._seen[eid] = author
        if len(self._seen) > 50_000:
            self._seen.popitem(last=False)

    async def _ingest(self, ev, link: _Link) -> bool:
        """Store one upstream wrap AS RECEIVED. Returns True when it was new here."""
        from app.services.nostr.event import verify_event
        if not isinstance(ev, dict):
            return False
        try:
            kind = int(ev.get("kind"))
        except (TypeError, ValueError):
            return False
        author, eid = ev.get("pubkey"), ev.get("id")
        if kind not in STREAM_KINDS or author not in link.authors or not isinstance(eid, str):
            return False                    # only what we asked for; an upstream cannot add to it
        if eid in self._seen:
            return False
        try:
            if len(json.dumps(ev)) > MAX_WRAP_BYTES:
                return False
        except (TypeError, ValueError):
            return False
        if not verify_event(ev):
            return False
        server = self.server
        if kind == LIVE_KIND:
            self._seen_add(eid, author)
            if server is not None:
                server.subs.fanout(ev, server._send, server._can_serve_event)
            return True
        hosted = author in self.hosted_ever
        if not hosted and self._author_full(author):
            return False
        try:
            if await self.store.has_event(eid):
                self._seen_add(eid, author)
                return False
            stored = await self.store.add_event(ev, origin="wot")
        except Exception as e:
            logger.debug("[concord-mirror] store failed: %s", e)
            return False
        if not stored:
            return False
        self._seen_add(eid, author)
        if not hosted:
            self.counts[author] = self.counts.get(author, 0) + 1
            self._dirty()
        if server is not None:
            server.subs.fanout(ev, server._send, server._can_serve_event)
        return True

    def status(self) -> dict:
        return {"enabled": self.enabled(), "registrations": sum(1 for _ in self._active()),
                "streams": len(self.registered_authors()), "stored": self.total(),
                "links": [link.status() for link in self.links.values()],
                "directory": {"events": len(self.directory.events), "ok_at": int(self.directory.ok_at),
                              "error": self.directory.error}}
