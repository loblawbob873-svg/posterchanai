"""The Nostr half of the VM host: kinds 5310 → 6310/7310 on this node's own relay, plus the 31310
announcement.

A request is an event the REQUESTER signs with their real key, NIP-44-encrypted to this node's
operator key and p-tagged to it. Nothing about who is asking is taken from the content: the author
of a verified event is the requester, full stop.

    content (plaintext, ≤65KB) = {"v":1, "id":"<idempotency id>", "op":"vm.power", "ts":…, "args":{…}}
    result  = {"v":1, "id":…, "ok":true, "result":{…}} | {"v":1, "id":…, "ok":false, "error":{code,message}}

DROP ORDER — cheapest first, and a stranger never reaches the expensive half (the DVM worker's rule):
kind → addressed to this node → payload size → already-seen event id → requester's role (a stranger
ends here, with NO reply) → BIP-340 signature → clock skew and expiration → decrypt.

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
MAX_PENDING = 64


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
               "features": ["novnc", "hardware", "snapshots", "iso-fetch", "sessions"]}
    tags = [["d", kinds.ANNOUNCE_D], ["alt", "PosterChan VM host"]] + [["relay", r] for r in relays]
    return nostr_event.build_event(node_sk, kinds.ANNOUNCE_KIND, json.dumps(content), tags,
                                   created_at=int(now if now is not None else time.time()))


# ------------------------------------------------------------------------------------ transport
class Transport:
    def __init__(self, service: VmHostService, node_sk: bytes, publish, now=time.time):
        from app.services.nostr import bip340
        self.service = service
        self.node_sk = node_sk
        self.node_pk = bip340.pubkey_from_seckey(node_sk).hex()
        self.publish = publish                    # async (event) -> bool
        self.now = now
        self.seen = SeenIds()
        self._pending = 0

    def _drop(self, why: str, ev: dict) -> None:
        logger.debug("[vmhost] dropped %s: %s", str(ev.get("id", ""))[:12], why)
        return None

    async def on_event(self, ev: dict) -> Optional[dict]:
        """Handle one delivered event. Returns the RESULT event it published, or None when it
        dropped the request (by design, most of the time a silent None)."""
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
        actor, session, session_state = requester, None, None
        if await self.service.role_of(requester) is None:
            owner, session_state = self.service._sessions().lookup(requester)
            if owner is None or await self.service.role_of(owner) is None:
                return self._drop("not on this host's lists", ev)    # a stranger: no reply at all
            actor, session = owner, requester
        if not nostr_event.verify_event(ev):
            return self._drop("bad signature", ev)
        self.seen.add(eid)
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

        async def _run():
            try:
                await self.on_event(ev)
            except Exception as e:
                logger.warning("[vmhost] request failed: %s", e)
            finally:
                self._pending -= 1
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

    filters = [{"kinds": [kinds.REQ_KIND], "#p": [tr.node_pk]}]

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
    for t in list(_state.get("tasks") or []):
        t.cancel()
        try:
            await asyncio.wait_for(asyncio.shield(t), timeout=3)
        except (asyncio.TimeoutError, asyncio.CancelledError, Exception):
            pass
    _release_lock(_state.get("lock_fd"))
    try:
        from . import service as service_mod
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
