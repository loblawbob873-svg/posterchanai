"""The Nostr half of the VM host: kinds 5310 → 6310/7310 on this node's own relay, plus the 31310
announcement.

A request is an event the REQUESTER signs with their real key, NIP-44-encrypted to this node's
operator key and p-tagged to it. Nothing about who is asking is taken from the content: the author
of a verified event is the requester, full stop.

    content (plaintext, ≤65KB) = {"v":1, "id":"<idempotency id>", "op":"vm.power", "ts":…, "args":{…}}
    result  = {"v":1, "id":…, "ok":true, "result":{…}} | {"v":1, "id":…, "ok":false, "error":{code,message}}

DROP ORDER — cheapest first, and a stranger never reaches the expensive half (the DVM worker's rule):
kind → addressed to this node → payload size → already-seen event id → requester's role, or a session key's owner's (a stranger
ends here, with NO reply) → that pubkey's token bucket (peek) → BIP-340 signature → clock skew and
expiration → token taken → marked seen → the role's busy budget (admins and peer hosts have their own)
→ decrypt.

Timing rules (`REQ_MAX_AGE`, `REQ_MAX_FUTURE`): a request older than two minutes or more than thirty
seconds in the future is dropped, as is one with no expiration or an expired one. That is what makes
a captured request useless to replay later — and the op journal is what makes an honest retry inside
that window harmless (see journal.py).
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from typing import Optional

from app.services.nostr import event as nostr_event
from app.services.nostr import nip44

from . import kinds
from .journal import SeenIds
from .service import PROTO_VERSION, VmHostService

logger = logging.getLogger(__name__)

REQ_MAX_AGE = 120
REQ_MAX_FUTURE = 30
RESULT_TTL = 300
PROGRESS_TTL = 600
MAX_PLAINTEXT = 65_000
ANNOUNCE_EVERY = 6 * 3600
INDEX_EVERY = 300
# Admission, in two stages. MAX_PENDING bounds events in the CHEAP stage (role, signature, clock), which
# a request leaves within milliseconds. MAX_BUSY bounds the EXPENSIVE stage (decrypt, libvirt) PER ROLE:
# with one shared budget a user flood of slow operations filled it and an admin's request — the person
# who could revoke the flooder — was dropped at the door.
#
# PEER HOSTS (phase 3, `vmhost_peer_hosts`) have a budget of their OWN, in both stages. A migration is a
# conversation between two hosts that must not stall halfway: the target's commit/status/ack and the
# source's abort arrive while the transfer runs, and if they shared the user slots or the user bucket a
# user flood (or an admin's own polling) could starve them, leaving both hosts locked until an admin
# force-reclaims. The peer set is a handful of configured node keys, so a generous per-key bucket costs
# nothing a stranger can reach.
MAX_PENDING = 256
MAX_BUSY = {"user": 32, "admin": 16, "peer": 8}
# Per-pubkey token buckets (burst, refill per second), spent BEFORE anything is decrypted.
BUCKETS = {"user": (10, 1.0), "admin": (30, 3.0), "peer": (60, 5.0)}


# ------------------------------------------------------------------------------------ builders
def build_request(requester_sk: bytes, host_pubkey: str, op: str, args: dict, req_id: str,
                  created_at: Optional[int] = None, ttl: int = 120) -> dict:
    """What a client sends. Used by tests and scripts; the web client builds the same shape in
    static/js/client/vmrpc.js."""
    ts = int(created_at if created_at is not None else time.time())
    body = json.dumps({"v": PROTO_VERSION, "id": req_id, "op": op, "ts": ts, "args": args})
    enc = nip44.encrypt_to(requester_sk, bytes.fromhex(host_pubkey), body)
    tags = [["p", host_pubkey], ["expiration", str(ts + ttl)], ["nofederate"]]
    return nostr_event.build_event(requester_sk, kinds.REQ_KIND, enc, tags, created_at=ts)


def build_reply(node_sk: bytes, req_event_id: str, requester: str, payload: dict,
                kind: int = kinds.RES_KIND, now: Optional[int] = None) -> dict:
    ts = int(now if now is not None else time.time())
    ttl = RESULT_TTL if kind == kinds.RES_KIND else PROGRESS_TTL
    enc = nip44.encrypt_to(node_sk, bytes.fromhex(requester), json.dumps(payload))
    tags = [["e", req_event_id], ["p", requester], ["nofederate"], ["expiration", str(ts + ttl)]]
    return nostr_event.build_event(node_sk, kind, enc, tags, created_at=ts)


def build_announcement(node_sk: bytes, cfg, now: Optional[int] = None) -> dict:
    relays = [r for r in [cfg.public_relay] if r]
    content = {"v": 1, "name": cfg.display_name or "PosterChan VM host", "https": cfg.public_url,
               "relays": relays, "proto": [PROTO_VERSION],
               "features": ["novnc", "hardware", "snapshots", "iso-fetch", "sessions", "cold-migrate"]}
    tags = [["d", kinds.ANNOUNCE_D], ["alt", "PosterChan VM host"]] + [["relay", r] for r in relays]
    return nostr_event.build_event(node_sk, kinds.ANNOUNCE_KIND, json.dumps(content), tags,
                                   created_at=int(now if now is not None else time.time()))


# ------------------------------------------------------------------------------------ transport
# One seen-set per storage directory, for the life of the PROCESS — not per Transport. A settings Save
# stops the host and starts a new Transport, and the relay replays every stored request to the new
# subscription; a fresh seen-set would handle the last two minutes of requests a second time.
_SEEN: dict = {}


def seen_for(service: VmHostService) -> SeenIds:
    path = service.storage.state_dir / "seen.log"
    key = str(path)
    s = _SEEN.get(key)
    if s is None:
        s = _SEEN[key] = SeenIds(ttl=REQ_MAX_AGE + REQ_MAX_FUTURE, path=path)
    return s


class TokenBuckets:
    """Per-pubkey token buckets. `peek` before the signature check (a forgery wearing somebody's pubkey
    must not spend their tokens), `take` after it."""

    MAX_KEYS = 10_000

    def __init__(self, now=time.monotonic):
        self.now = now
        self._b: dict = {}           # pubkey -> (tokens, at)

    def _level(self, pk: str, role: str) -> tuple:
        burst, rate = BUCKETS.get(role, BUCKETS["user"])
        t = self.now()
        tok, at = self._b.get(pk, (float(burst), t))
        return min(float(burst), tok + max(0.0, t - at) * rate), t

    def peek(self, pk: str, role: str) -> bool:
        return self._level(pk, role)[0] >= 1.0

    def take(self, pk: str, role: str) -> bool:
        tok, t = self._level(pk, role)
        if tok < 1.0:
            self._b[pk] = (tok, t)
            return False
        self._b[pk] = (tok - 1.0, t)
        if len(self._b) > self.MAX_KEYS:
            # Only allowed pubkeys ever get here; forget the ones that have refilled completely.
            for k in [k for k in self._b if k != pk and self._level(k, "user")[0] >= BUCKETS["user"][0]]:
                self._b.pop(k, None)
        return True


class Transport:
    def __init__(self, service: VmHostService, node_sk: bytes, publish, now=time.time,
                 seen: SeenIds | None = None):
        from app.services.nostr import bip340
        self.service = service
        self.node_sk = node_sk
        self.node_pk = bip340.pubkey_from_seckey(node_sk).hex()
        self.publish = publish                    # async (event) -> bool
        self.now = now
        self.seen = seen if seen is not None else seen_for(service)
        self.buckets = TokenBuckets()
        self._pending = 0
        self._busy = {r: 0 for r in MAX_BUSY}

    def _is_peer(self, pk: str) -> bool:
        m = getattr(self.service, "migrator", None)
        try:
            return bool(m is not None and m.is_peer(pk))
        except Exception:
            return False

    def _drop(self, why: str, ev: dict) -> None:
        logger.debug("[vmhost] dropped %s: %s", str(ev.get("id", ""))[:12], why)
        return None

    async def on_event(self, ev: dict, on_admitted=None) -> Optional[dict]:
        """Handle one delivered event. Returns the RESULT event it published, or None when it
        dropped the request (by design, most of the time a silent None). `on_admitted` is called when
        the request leaves the cheap stage for its role's busy budget (spawn's intake counter)."""
        if not isinstance(ev, dict) or ev.get("kind") != kinds.REQ_KIND:
            return self._drop("kind", ev or {})
        if self.node_pk not in kinds.tag_values(ev, "p"):
            return self._drop("not addressed to this host", ev)
        content = ev.get("content")
        if not isinstance(content, str) or len(content) > kinds.MAX_CONTENT:
            return self._drop("payload too large", ev)
        eid = ev.get("id")
        if not isinstance(eid, str) or eid in self.seen:
            return self._drop("already handled", ev)
        requester = str(ev.get("pubkey", "")).lower()
        # The AUTHOR is who we talk to (decrypt from, reply to). The ACTOR is whose rights apply: the
        # author itself, or — for a session key (sessions.py) — the real key that opened the session.
        # Budgets (token bucket, busy slots) are the ACTOR's: opening eight sessions does not buy eight
        # buckets.
        actor, session, session_state = requester, None, None
        role = await self.service.role_of(requester)
        if role is None:
            owner, session_state = self.service._sessions().lookup(requester)
            role = await self.service.role_of(owner) if owner is not None else None
            if role is None:
                return self._drop("not on this host's lists", ev)    # a stranger: no reply at all
            actor, session = owner, requester
        # Which BUDGET, not which rights (service.handle decides those): a configured peer host that is
        # also on a user list still gets the peer budget when it signs with its own key, so a migration
        # never waits on user traffic. A session key is never a peer's (a peer cannot open one).
        budget = role
        if role == "user" and session is None and self._is_peer(actor):
            budget = "peer"
        if not self.buckets.peek(actor, budget):
            return self._drop("rate limited", ev)
        if not nostr_event.verify_event(ev):
            return self._drop("bad signature", ev)
        now = int(self.now())
        try:
            created = int(ev.get("created_at", 0))
        except (TypeError, ValueError):
            return self._drop("created_at", ev)
        if created < now - REQ_MAX_AGE or created > now + REQ_MAX_FUTURE:
            return self._drop("outside the clock window", ev)
        exp = kinds.expiration_of(ev)
        if exp is None or exp < now or exp > created + kinds.REQ_MAX_EXPIRATION:
            return self._drop("expiration", ev)
        # Recorded only once it is inside the window (outside it the window itself refuses a replay),
        # and atomically with the check: two deliveries of one event racing through the awaits above
        # must not both get here.
        if not self.buckets.take(actor, budget):
            return self._drop("rate limited", ev)
        marked = self.seen.add(eid)
        if marked != "ok":
            return self._drop("already handled" if marked == "dup" else "replay table full", ev)
        busy_role = budget if budget in MAX_BUSY else "user"
        if self._busy[busy_role] >= MAX_BUSY.get(busy_role, 16):
            return self._drop(f"{busy_role} budget full", ev)
        self._busy[busy_role] += 1
        try:
            if on_admitted is not None:
                on_admitted()
            if self.seen.path is not None:
                await asyncio.to_thread(self.seen.flush)
            return await self._execute(eid, requester, content, actor, session, session_state)
        finally:
            self._busy[busy_role] -= 1

    async def _execute(self, eid: str, requester: str, content: str, actor: str | None = None,
                       session: str | None = None, session_state: str | None = None) -> Optional[dict]:
        actor = actor or requester
        async def reply(payload: dict, kind: int = kinds.RES_KIND) -> Optional[dict]:
            out = build_reply(self.node_sk, eid, requester, payload, kind=kind, now=int(self.now()))
            try:
                ok = await self.publish(out)
            except Exception as e:
                logger.warning("[vmhost] publishing a reply failed: %s", e)
                ok = False
            return out if ok else None

        try:
            plain = nip44.decrypt_from(self.node_sk, bytes.fromhex(requester), content)
            if len(plain.encode("utf-8")) > MAX_PLAINTEXT:
                raise ValueError("too large")
            body = json.loads(plain)
            if not isinstance(body, dict):
                raise ValueError("not an object")
        except Exception:
            return await reply({"v": PROTO_VERSION, "id": "", "ok": False,
                                "error": {"code": "bad_request", "message": "could not read the request"}})
        rid = body.get("id") if isinstance(body.get("id"), str) else ""
        if body.get("v") != PROTO_VERSION:
            return await reply({"v": PROTO_VERSION, "id": rid, "ok": False,
                                "error": {"code": "version", "message": "unsupported protocol version"}})

        async def progress(p: dict) -> None:
            await reply({"v": PROTO_VERSION, "id": rid, "progress": p}, kind=kinds.PROGRESS_KIND)

        if session and session_state != "live":
            # Answered, not dropped: silence here would read as "the host is offline" to the client.
            return await reply({"v": PROTO_VERSION, "id": rid, "ok": False,
                                "error": {"code": "session_expired",
                                          "message": "this session has ended — open a new one"}})
        res = await self.service.handle(actor, body.get("op"), body.get("args", {}), rid, progress,
                                        session=session)
        if res is None:
            return None
        return await reply(res)

    def spawn(self, ev: dict) -> None:
        """Listener callback: never block the recv loop on libvirt."""
        if self._pending >= MAX_PENDING:
            return
        self._pending += 1
        released = [False]

        def release():
            if not released[0]:
                released[0] = True
                self._pending -= 1

        async def _run():
            try:
                await self.on_event(ev, on_admitted=release)
            except Exception as e:
                logger.warning("[vmhost] request failed: %s", e)
            finally:
                release()
        _track(asyncio.create_task(_run()))


_tasks: set = set()


def _track(t) -> None:
    _tasks.add(t)
    t.add_done_callback(_tasks.discard)


# ------------------------------------------------------------------------------------ lifecycle
_state: dict = {"stop": None, "tasks": [], "lock_fd": None, "transport": None, "error": ""}


def status() -> dict:
    t = _state.get("transport")
    return {"running": bool(t), "error": _state.get("error", ""),
            "node_pubkey": t.node_pk if t else ""}


def _take_lock(path: str):
    import fcntl
    fd = os.open(path, os.O_RDWR | os.O_CREAT, 0o600)
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        os.close(fd)
        return None
    return fd


def _release_lock(fd) -> None:
    if fd is None:
        return
    try:
        import fcntl
        fcntl.flock(fd, fcntl.LOCK_UN)
    except Exception:
        pass
    try:
        os.close(fd)
    except OSError:
        pass


def start() -> None:
    """Start the host (idempotent; no-op unless vmhost_enabled). Called from app startup on port 3051."""
    try:
        _state["loop"] = asyncio.get_running_loop()   # remembered even when disabled, for request_reload
    except RuntimeError:
        return
    if _state["stop"] is not None:
        return
    from . import config
    cfg = config.current()
    if not cfg.enabled:
        _state["error"] = ""
        return
    _state["stop"] = asyncio.Event()
    _track(asyncio.create_task(_run(cfg, _state["stop"])))


async def _run(cfg, stop: asyncio.Event) -> None:
    from app.services import nostr_dvm
    from app.services.nostr import relay as nostr_relay
    from . import service as service_mod
    from .backend import BackendError, make_backend
    from .storage import Storage

    sk = nostr_dvm.node_seckey()
    if not sk:
        _state["error"] = "this node has no relay identity (operator key) yet"
        logger.warning("[vmhost] enabled but %s — not started", _state["error"])
        _state["stop"] = None
        return
    storage = Storage(cfg.storage_dir)
    try:
        await asyncio.to_thread(storage.ensure)
        fd = await asyncio.to_thread(_take_lock, str(storage.state_dir / "lock"))
    except OSError as e:
        _state["error"] = f"storage directory {cfg.storage_dir} is not usable: {e}"
        logger.warning("[vmhost] %s — not started", _state["error"])
        _state["stop"] = None
        return
    if fd is None:
        _state["error"] = "another process already runs the VM host on this storage directory"
        logger.warning("[vmhost] %s — not started", _state["error"])
        _state["stop"] = None
        return
    _state["lock_fd"] = fd
    try:
        backend = make_backend(cfg)
    except BackendError as e:
        _state["error"] = str(e)
        _release_lock(fd)
        _state["lock_fd"] = None
        _state["stop"] = None
        return
    relay = nostr_dvm.relay_url()
    svc = VmHostService(cfg, backend, node_pubkey=nostr_dvm.node_pubkey() or "", storage=storage)
    service_mod.set_current(svc)

    async def publish(ev: dict) -> bool:
        return bool(await nostr_relay.publish(relay, ev, direct=True))

    tr = Transport(svc, sk, publish)
    _state["transport"] = tr
    # Phase 3: cold migration (peers from vmhost_peer_hosts; resumes unfinished migrations from the journal).
    try:
        from app.services import settings_store
        from . import migrate
        migrator = migrate.attach(svc, sk, publish, settings_store.all_settings())
        await migrator.resume()
    except Exception as e:
        migrator = None
        logger.warning("[vmhost] migration support not started: %s", e)
    _state["error"] = ""
    logger.info("[vmhost] VM host listening on %s as %s (libvirt %s, storage %s)",
                relay, tr.node_pk[:16], cfg.libvirt_uri, cfg.storage_dir)

    async def housekeeping():
        last_announce = 0.0
        while not stop.is_set():
            try:
                await svc.refresh_index()
            except Exception as e:
                logger.warning("[vmhost] could not read libvirt domains: %s", e)
            if migrator is not None:
                try:
                    await migrator.housekeeping()
                except Exception as e:
                    logger.warning("[vmhost] migration housekeeping failed: %s", e)
            if cfg.announce and time.time() - last_announce > ANNOUNCE_EVERY:
                try:
                    await publish(build_announcement(sk, cfg))
                    last_announce = time.time()
                except Exception as e:
                    logger.debug("[vmhost] announcement failed: %s", e)
            try:
                await asyncio.wait_for(stop.wait(), INDEX_EVERY)
            except asyncio.TimeoutError:
                pass

    # `since`: the relay stores requests until they expire and replays every stored match to a new
    # subscription. Anything older than the clock window is refused anyway, so asking for it only costs
    # the relay; what IS inside the window is caught by the process-wide seen-set.
    filters = [{"kinds": [kinds.REQ_KIND], "#p": [tr.node_pk], "since": int(time.time()) - REQ_MAX_AGE}]

    async def _handler(ev):
        tr.spawn(ev)

    tasks = [asyncio.create_task(housekeeping()),
             asyncio.create_task(nostr_relay.subscribe(relay, filters, _handler, stop, direct=True))]
    _state["tasks"] = tasks
    for t in tasks:
        _track(t)


async def stop() -> None:
    ev = _state.get("stop")
    if ev is not None:
        ev.set()
    t = _state.get("transport")
    m = getattr(t.service, "migrator", None) if t else None
    if m is not None:
        try:
            await m.close()
        except Exception:
            pass
    for t in list(_state.get("tasks") or []):
        t.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(t), timeout=3)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass
    _release_lock(_state.get("lock_fd"))
    try:
        from . import service as service_mod
        old = service_mod.current()
        if old is not None:
            # The next start builds a new service with a new console registry; this is the last moment
            # anything can reach the sockets opened under the old configuration (whose access lists
            # the Save may just have narrowed).
            old.consoles.close_all()
        service_mod.set_current(None)
    except Exception:
        pass
    _state.update({"stop": None, "tasks": [], "lock_fd": None, "transport": None})


async def reload() -> None:
    """Settings changed: stop and start again with the new configuration."""
    await stop()
    start()


def request_reload() -> bool:
    """Thread-safe reload for the admin Save route, which runs in a worker thread. Only the process
    that started the host (port 3051) has a loop recorded; anywhere else this is a no-op."""
    loop = _state.get("loop")
    if loop is None or loop.is_closed():
        return False
    asyncio.run_coroutine_threadsafe(reload(), loop)
    return True
