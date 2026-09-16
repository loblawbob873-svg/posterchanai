"""The VM host: roles, operations, locks and capacity — everything that decides what a request DOES.

Transport-agnostic on purpose. `handle(requester, op, args, req_id)` takes an already-verified,
already-decrypted request and returns the response dict (or None for "say nothing"), so the role
matrix, conflicts, capacity and confinement are tested directly against a fake hypervisor, and the
Nostr layer (transport.py) is tested separately for what only it does: signatures, expiry, replay.

ROLES — decided HERE, per request, from this node's own configuration and never from anything the
requester says about itself:
  * admin  = a `User.is_admin` account's linked npub ∪ `vmhost_admin_npubs` ∪ this node's operator key.
             Creates, deletes, assigns; sees and operates every VM.
  * user   = `vmhost_allowed_npubs` ∪ every pubkey some VM is assigned to (the assignment IS the grant).
             Sees ONLY the VMs assigned to it, and can power them and open their console. A VM that
             exists but is not theirs answers `not_found`, never `forbidden` — the second would confirm
             it exists.
  * anyone else is DROPPED: no result event at all. An answer of any kind tells a stranger this npub
    runs a VM host and costs this node a signature per probe.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import shutil
import time
from typing import Optional

from . import domainxml
from .backend import BackendError, DomainInfo, is_loopback
from .config import VmHostConfig
from .console import ConsoleRegistry
from .journal import OpJournal
from .storage import PathEscape, Storage, clean_name, new_uuid, valid_uuid
# Phase 2 ops live in their own modules, mixed into the service (so this file only grows by table rows).
from .access import AccessOps
from .hardware import HardwareOps
from .isolib import IsoOps
from .sessions import CURRENT_SESSION, SESSION_OPS, SessionOps

logger = logging.getLogger(__name__)

PROTO_VERSION = 1
FEATURES = ["novnc", "hardware", "snapshots", "iso-fetch", "iso-upload", "access", "sessions"]
LOCK_WAIT = 2.0
ADMIN_CACHE_SEC = 60
# A FAILED admin lookup (account table unreadable) is remembered this long, so a burst of requests —
# a stranger's included, since role_of runs before anything else — does not re-run it per request.
ADMIN_NEG_CACHE_SEC = 5
# One libvirt listing is shared by every read op for this long (single-flight). `virsh list` plus three
# calls per domain, per request, was the cheapest way for one allowed user to load the host.
DOMAIN_SNAPSHOT_SEC = 3

ERROR_CODES = ("bad_request", "forbidden", "not_found", "conflict", "busy", "insufficient_capacity",
               "unsupported", "rate_limited", "backend_error", "timeout", "version", "internal",
               "migrating", "session_expired", "step_up_required")

# op -> (minimum role, mutating?). Mutating ops go through the op journal (idempotent retries).
OPS = {
    "host.whoami":    ("user", False),
    "host.info":      ("user", False),
    "vm.list":        ("user", False),
    "vm.get":         ("user", False),
    "vm.power":       ("user", True),
    "console.ticket": ("user", False),
    "iso.list":       ("admin", False),
    "vm.create":      ("admin", True),
    "vm.delete":      ("admin", True),
    "vm.assign":      ("admin", True),
    "vm.unassign":    ("admin", True),
    # ---- phase 2 (hardware.py, isolib.py, access.py, sessions.py)
    "vm.update":           ("admin", True),
    "vm.snapshot.list":    ("admin", False),
    "vm.snapshot.create":  ("admin", True),
    "vm.snapshot.revert":  ("admin", True),
    "vm.snapshot.delete":  ("admin", True),
    "iso.fetch":           ("admin", True),
    "iso.fetch.status":    ("admin", False),
    "iso.fetch.cancel":    ("admin", False),
    "iso.upload_ticket":   ("admin", False),
    "iso.delete":          ("admin", True),
    "host.access.get":     ("admin", False),
    "host.access.set":     ("admin", True),
    "session.open":        ("user", False),
    "session.close":       ("user", False),
}
# Ops that must be signed by the REAL key even though a session could technically reach them.
REAL_KEY_ONLY = frozenset({"session.open"})

_REQ_ID = re.compile(r"^[A-Za-z0-9_-]{1,64}$")
_HEX64 = re.compile(r"^[0-9a-f]{64}$")


def _args_hash(args: dict) -> str:
    """Canonical hash of a request's arguments: key order and whitespace do not make a different op."""
    raw = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


class VmHostError(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code if code in ERROR_CODES else "internal"
        self.message = message


def db_admin_pubkeys() -> set:
    """Pubkeys of admin accounts with a linked npub. Short-lived session, released immediately."""
    from app.database import SessionLocal
    from app.models import User
    from app.services.nostr import nostr_service
    out = set()
    db = SessionLocal()
    try:
        for u in db.query(User).filter(User.is_admin == True, User.nostr_npub.isnot(None)).all():  # noqa: E712
            try:
                pk = nostr_service.to_pubkey_hex(u.nostr_npub)
                if pk:
                    out.add(pk.lower())
            except Exception:
                continue
    finally:
        db.close()
    return out


def _to_hex(pk) -> Optional[str]:
    s = str(pk or "").strip()
    low = s.lower()
    if _HEX64.match(low):
        return low
    if low.startswith("npub1"):
        try:
            from app.services.nostr import nostr_service
            h = nostr_service.to_pubkey_hex(s)
            return h.lower() if h and _HEX64.match(h.lower()) else None
        except Exception:
            return None
    return None


def _int_arg(args: dict, key: str, lo: int, hi: int, default=None) -> int:
    v = args.get(key, default)
    if isinstance(v, bool) or v is None:
        raise VmHostError("bad_request", f"{key} is required")
    try:
        n = int(v)
    except (TypeError, ValueError):
        raise VmHostError("bad_request", f"{key} must be a whole number")
    if n < lo:
        raise VmHostError("bad_request", f"{key} must be at least {lo}")
    if n > hi:
        raise VmHostError("insufficient_capacity", f"{key} may be at most {hi} on this host")
    return n


class VmHostService(HardwareOps, IsoOps, AccessOps, SessionOps):
    def __init__(self, cfg: VmHostConfig, backend, *, node_pubkey: str, admin_provider=None,
                 storage: Storage | None = None, journal: OpJournal | None = None,
                 consoles: ConsoleRegistry | None = None, now=time.time):
        self.cfg = cfg
        self.backend = backend
        self.node_pubkey = (node_pubkey or "").lower()
        self.storage = storage or Storage(cfg.storage_dir)
        self.journal = journal if journal is not None else OpJournal(
            self.storage.state_dir / "journal" / "ops.jsonl")
        self.consoles = consoles or ConsoleRegistry(now=now)
        self.now = now
        self._admin_provider = admin_provider if admin_provider is not None else db_admin_pubkeys
        self._admin_cache: tuple = (0.0, set(), 0)      # (fetched at, pubkeys, valid for seconds)
        self._admin_inflight: Optional[asyncio.Future] = None
        self._snap: tuple = (0.0, None)
        self._snap_gen = 0
        self._snap_inflight: Optional[asyncio.Future] = None
        self._assign: dict = {}          # uuid -> set(pubkey)
        self._index_ok = False
        self._vm_locks: dict = {}
        self._pw_locks: dict = {}
        self._host_lock = asyncio.Lock()

    # ---------------------------------------------------------------- identity & index
    async def admin_pubkeys(self) -> set:
        ts, cached, ttl = self._admin_cache
        if self.now() - ts < ttl:
            return cached
        fut = self._admin_inflight
        if fut is not None:                               # single-flight: one lookup, many waiters
            return await asyncio.shield(fut)
        fut = self._admin_inflight = asyncio.get_running_loop().create_future()
        try:
            out = await self._load_admin_pubkeys()
            fut.set_result(out)
            return out
        except BaseException:
            if not fut.done():
                fut.cancel()
            raise
        finally:
            self._admin_inflight = None

    async def _load_admin_pubkeys(self) -> set:
        base = set(self.cfg.admin_pubkeys) | ({self.node_pubkey} if self.node_pubkey else set())
        try:
            prov = self._admin_provider
            got = await prov() if asyncio.iscoroutinefunction(prov) else await asyncio.to_thread(prov)
            db = {p.lower() for p in (got or ()) if isinstance(p, str)}
        except Exception as e:
            # An unreadable account table must not lock the operator out: the settings list and the
            # node key still count. It must not keep a stale grant either, so the fallback holds only
            # those — and only briefly, so a recovered table is read again within seconds.
            logger.warning("[vmhost] admin lookup failed: %s", e)
            self._admin_cache = (self.now(), base, ADMIN_NEG_CACHE_SEC)
            return base
        out = db | base
        self._admin_cache = (self.now(), out, ADMIN_CACHE_SEC)
        return out

    async def domains(self) -> list:
        """Every domain, from a snapshot shared by all read ops for DOMAIN_SNAPSHOT_SEC (single-flight).
        Mutating ops invalidate it, so nobody reads their own change back stale."""
        ts, snap = self._snap
        if snap is not None and self.now() - ts < DOMAIN_SNAPSHOT_SEC:
            return snap
        fut = self._snap_inflight
        if fut is not None:
            return await asyncio.shield(fut)
        gen = self._snap_gen
        fut = self._snap_inflight = asyncio.get_running_loop().create_future()
        try:
            domains = await self.backend.list_domains()
        except BaseException as e:
            if not fut.done():
                if isinstance(e, Exception):
                    fut.set_exception(e)
                    fut.exception()                        # a waiter is optional; mark it retrieved
                else:
                    fut.cancel()
            raise
        finally:
            self._snap_inflight = None
        for d in domains:
            self._set_index(d)
        if gen == self._snap_gen:                          # not invalidated while it was being read
            self._snap = (self.now(), domains)
        fut.set_result(domains)
        return domains

    def invalidate_domains(self) -> None:
        self._snap_gen += 1
        self._snap = (0.0, None)

    async def refresh_index(self) -> None:
        domains = await self.backend.list_domains()
        idx = {}
        for d in domains:
            if d.meta and d.meta.assigned:
                idx[d.uuid] = set(d.meta.assigned)
        self._assign = idx
        self._index_ok = True

    def _set_index(self, d: DomainInfo) -> None:
        if d.meta and d.meta.assigned:
            self._assign[d.uuid] = set(d.meta.assigned)
        else:
            self._assign.pop(d.uuid, None)

    async def role_of(self, pubkey: str) -> Optional[str]:
        pk = (pubkey or "").lower()
        if not _HEX64.match(pk):
            return None
        if pk in await self.admin_pubkeys():
            return "admin"
        if pk in self.cfg.allowed_pubkeys:
            return "user"
        if any(pk in s for s in self._assign.values()):
            return "user"
        if self.migrator is not None and self.migrator.is_peer(pk):
            return "peer"                                 # phase 3: a paired host, for peer.migrate.* only
        return None

    def _visible(self, d: DomainInfo, role: str, pk: str) -> bool:
        return role == "admin" or bool(d.meta and pk in d.meta.assigned)

    # ---------------------------------------------------------------- locks
    def _vm_lock(self, vm: str) -> asyncio.Lock:
        lk = self._vm_locks.get(vm)
        if lk is None:
            lk = self._vm_locks[vm] = asyncio.Lock()
        return lk

    async def _acquire(self, lock: asyncio.Lock, what: str):
        try:
            await asyncio.wait_for(lock.acquire(), LOCK_WAIT)
        except asyncio.TimeoutError:
            raise VmHostError("busy", f"{what} is busy with another operation — try again shortly")

    # ---------------------------------------------------------------- entry point
    async def handle(self, requester: str, op, args, req_id, progress=None,
                     session: Optional[str] = None) -> Optional[dict]:
        """`session`: the session public key the request was SIGNED with, when the transport resolved it
        to `requester` (its owner). Such a request may only run SESSION_OPS — see sessions.py."""
        role = await self.role_of(requester)
        if role is None:
            return None                                   # stranger: silence, see the module doc
        pk = requester.lower()
        rid = req_id if isinstance(req_id, str) and _REQ_ID.match(req_id) else None

        def err(code, msg):
            return {"v": PROTO_VERSION, "id": rid or "", "ok": False, "error": {"code": code, "message": msg}}

        if rid is None:
            return err("bad_request", "request id missing or malformed")
        if not isinstance(op, str) or op not in OPS:
            return err("unsupported", f"unknown operation {str(op)[:40]!r}")
        if not isinstance(args, dict):
            return err("bad_request", "args must be an object")
        need, mutating = OPS[op]
        if session and (op not in SESSION_OPS or op in REAL_KEY_ONLY):
            return err("step_up_required", "sign this with your own key — a session key can only view, "
                                           "power and open consoles")
        if need == "admin" and role != "admin":
            return err("forbidden", "only a host admin can do that")
        if need == "peer" and not (self.migrator is not None and self.migrator.is_peer(pk)):
            return err("forbidden", "only a paired VM host can do that")
        if role == "peer" and need != "peer":
            return err("forbidden", "a paired host can only take part in migrations")

        async def run():
            CURRENT_SESSION.set(session)
            try:
                result = await getattr(self, "_op_" + op.replace(".", "_"))(pk, role, args, progress)
                return {"v": PROTO_VERSION, "id": rid, "ok": True, "result": result}
            except VmHostError as e:
                return err(e.code, e.message)
            except BackendError as e:
                return err(e.code if e.code in ERROR_CODES else "backend_error", str(e))
            except PathEscape:
                return err("bad_request", "that path is not allowed")
            except Exception as e:  # pragma: no cover - last resort, logged
                logger.exception("[vmhost] %s failed", op)
                return err("internal", f"{type(e).__name__}")

        if not mutating:
            return await run()
        try:
            res = await self.journal.run_once(pk, rid, op, run, args_hash=_args_hash(args))
        finally:
            self.invalidate_domains()
        if res is None:
            return err("bad_request", "that request id was already used for a different operation or arguments")
        return res

    # ---------------------------------------------------------------- views
    def _vm_view(self, d: DomainInfo, role: str, pk: str) -> dict:
        m = d.meta
        v = {"uuid": d.uuid, "name": d.name, "state": d.state, "vcpus": d.vcpus, "ram_mib": d.ram_mib,
             "disk_gib": m.disk_gib if m else 0, "managed": bool(m and self.storage.is_managed_dir(d.uuid)),
             "guest": m.guest if m else "", "firmware": m.firmware if m else "",
             "autostart": d.autostart, "labels": list(m.labels) if m else [],
             "created": m.created if m else 0,
             "migration": dict(m.migration) if (m and m.migration) else {}}
        if role == "admin":
            v["assigned"] = sorted(m.assigned) if m else []
            v["owner"] = m.owner if m else ""
            v["iso"] = m.iso if m else ""
        else:
            v["assigned"] = [pk] if (m and pk in m.assigned) else []
        return v

    async def _domain(self, pk: str, role: str, args: dict) -> DomainInfo:
        u = valid_uuid(args.get("vm"))
        if not u:
            raise VmHostError("bad_request", "vm must be a VM id")
        d = await self.backend.get(u)
        if d is None or not self._visible(d, role, pk):
            raise VmHostError("not_found", "no such VM")
        return d

    # ---------------------------------------------------------------- read ops
    async def _op_host_whoami(self, pk, role, args, progress):
        return {"role": role, "pubkey": pk,
                "host": {"name": self.cfg.display_name or "PosterChan VM host", "pubkey": self.node_pubkey,
                         "version": PROTO_VERSION, "proto": [PROTO_VERSION], "features": list(FEATURES),
                         "https": self.cfg.public_url}}

    async def _op_host_info(self, pk, role, args, progress):
        avail = await self.backend.available()
        domains = await self.domains()
        mine = [d for d in domains if self._visible(d, role, pk)]
        out = {"name": self.cfg.display_name or "PosterChan VM host",
               "kvm": bool(avail.get("kvm")), "libvirt": bool(avail.get("ok")),
               "vms": {"running": sum(1 for d in mine if d.state == "running"), "total": len(mine)}}
        if role != "admin":
            return out
        st = await self.backend.host_stats(str(self.storage.root))
        out.update({
            "cpu": {"cores": st.get("cores", 0), "load1": st.get("load1", 0.0)},
            "ram": {"total_mib": st.get("ram_total_mib", 0), "free_mib": st.get("ram_free_mib", 0),
                    "committed_mib": sum(d.ram_mib for d in domains)},
            "disk": {"total_gib": st.get("disk_total_gib", 0), "free_gib": st.get("disk_free_gib", 0),
                     "committed_gib": sum((d.meta.disk_gib if d.meta else 0) for d in domains)},
            "limits": {"max_vcpus": self.cfg.max_vcpus, "max_ram_mib": self.cfg.max_ram_mib,
                       "max_disk_gib": self.cfg.max_disk_gib, "reserve_ram_mib": self.cfg.reserve_ram_mib,
                       "reserve_disk_gib": self.cfg.reserve_disk_gib, "overcommit": self.cfg.allow_overcommit},
            "libvirt_version": avail.get("libvirt", ""), "error": avail.get("error", ""),
        })
        return out

    async def _op_vm_list(self, pk, role, args, progress):
        limit = args.get("limit", 50)
        try:
            limit = max(1, min(50, int(limit)))
            start = max(0, int(args.get("cursor") or 0))
        except (TypeError, ValueError):
            raise VmHostError("bad_request", "cursor/limit must be numbers")
        domains = await self.domains()
        vis = sorted((d for d in domains if self._visible(d, role, pk)), key=lambda d: (d.name.lower(), d.uuid))
        page = vis[start:start + limit]
        nxt = str(start + limit) if start + limit < len(vis) else None
        return {"vms": [self._vm_view(d, role, pk) for d in page], "next": nxt}

    async def _op_vm_get(self, pk, role, args, progress):
        d = await self._domain(pk, role, args)
        view = self._vm_view(d, role, pk)
        if role == "admin":
            view["hardware"] = await self._hardware(d.uuid)
        return {"vm": view}

    async def _op_iso_list(self, pk, role, args, progress):
        self._prune_jobs()
        jobs = [self._job_view(j) for j in self._iso_jobs().values()]
        return {"isos": await asyncio.to_thread(self.storage.list_isos), "jobs": jobs,
                "fetch_enabled": self.cfg.iso_fetch_enabled}

    # ---------------------------------------------------------------- power
    async def _op_vm_power(self, pk, role, args, progress):
        action = args.get("action")
        if action not in ("start", "shutdown", "reboot", "destroy"):
            raise VmHostError("bad_request", "action must be start, shutdown, reboot or destroy")
        d = await self._domain(pk, role, args)
        lock = self._vm_lock(d.uuid)
        await self._acquire(lock, "this VM")
        try:
            d = await self.backend.get(d.uuid)            # re-read under the lock
            if d is None or not self._visible(d, role, pk):
                raise VmHostError("not_found", "no such VM")
            if action == "start":
                self._migration_guard(d)
                if d.state in ("running", "paused", "stopping"):
                    raise VmHostError("conflict", f"the VM is already {d.state}")
                await self.backend.start(d.uuid)
            else:
                if d.state not in ("running", "paused", "stopping"):
                    raise VmHostError("conflict", "the VM is not running")
                await getattr(self.backend, action)(d.uuid)
                if action in ("shutdown", "destroy"):
                    self.consoles.revoke(d.uuid)
            after = await self.backend.get(d.uuid) or d
            return {"vm": self._vm_view(after, role, pk), "action": action}
        finally:
            lock.release()

    # ---------------------------------------------------------------- create / delete
    async def _op_vm_create(self, pk, role, args, progress):
        name = clean_name(args.get("name"))
        if not name:
            raise VmHostError("bad_request", "give the VM a name (letters, digits, . _ -)")
        guest = args.get("guest", "linux")
        firmware = args.get("firmware", "efi")
        if guest not in ("linux", "windows"):
            raise VmHostError("bad_request", "guest must be linux or windows")
        if firmware not in ("efi", "bios"):
            raise VmHostError("bad_request", "firmware must be efi or bios")
        vcpus = _int_arg(args, "vcpus", 1, self.cfg.max_vcpus, 2)
        ram = _int_arg(args, "ram_mib", 256, self.cfg.max_ram_mib, 2048)
        disk = _int_arg(args, "disk_gib", 1, self.cfg.max_disk_gib, 20)
        iso_id = args.get("iso") or ""
        iso_path = ""
        if iso_id:
            if not isinstance(iso_id, str):
                raise VmHostError("bad_request", "iso must be an ISO id")
            try:
                iso_path = str(await asyncio.to_thread(self.storage.iso_path, iso_id))
            except PathEscape:
                raise VmHostError("bad_request", "that is not an ISO from this host's library")
            except FileNotFoundError:
                raise VmHostError("not_found", "no such ISO in this host's library")

        await self._acquire(self._host_lock, "this host")
        created_dir = None
        defined = None
        try:
            domains = await self.backend.list_domains()
            if any(d.name == name for d in domains):
                raise VmHostError("conflict", f"a VM named {name} already exists")
            st = await self.backend.host_stats(str(self.storage.root))
            cores = int(st.get("cores") or 0)
            if cores and vcpus > cores and not self.cfg.allow_overcommit:
                raise VmHostError("insufficient_capacity", f"this host has {cores} CPU cores")
            committed = sum(d.ram_mib for d in domains)
            ram_room = int(st.get("ram_total_mib") or 0) - self.cfg.reserve_ram_mib - committed
            if not self.cfg.allow_overcommit and ram > ram_room:
                raise VmHostError("insufficient_capacity",
                                  f"only {max(0, ram_room)} MiB of memory is uncommitted on this host")
            disk_room = int(st.get("disk_free_gib") or 0) - self.cfg.reserve_disk_gib
            if disk > disk_room and not (self.cfg.allow_overcommit and disk_room > 0):
                raise VmHostError("insufficient_capacity",
                                  f"only {max(0, disk_room)} GiB of disk is free on this host")

            vm_uuid = new_uuid()
            vm_dir = self.storage.vm_dir(vm_uuid)
            await asyncio.to_thread(vm_dir.mkdir, mode=0o750, parents=False, exist_ok=False)
            created_dir = vm_dir
            disk_path = self.storage.disk_path(vm_uuid)
            if progress:
                await progress({"phase": "disk", "msg": f"creating a {disk} GiB disk"})
            await self.backend.img_create(str(disk_path), disk)
            meta = domainxml.VmMeta(owner=pk, created=int(self.now()), guest=guest, firmware=firmware,
                                    disk_gib=disk, iso=iso_id, assigned=[], labels=[])
            spec = domainxml.DomainSpec(
                name=name, uuid=vm_uuid, guest=guest, firmware=firmware, vcpus=vcpus, ram_mib=ram,
                disk_path=str(disk_path), nvram_path=str(self.storage.nvram_path(vm_uuid)),
                iso_path=iso_path, network=self.cfg.default_network, bridge=self.cfg.bridge, meta=meta)
            if progress:
                await progress({"phase": "define", "msg": "defining the VM"})
            await self.backend.define(domainxml.build_domain_xml(spec), str(vm_dir))
            defined = vm_uuid
            if args.get("autostart"):
                await self.backend.set_autostart(vm_uuid, True)
            if args.get("start"):
                if progress:
                    await progress({"phase": "start", "msg": "starting the VM"})
                await self.backend.start(vm_uuid)
            d = await self.backend.get(vm_uuid)
            if d is None:
                raise VmHostError("backend_error", "the VM was defined but libvirt does not report it")
            self._set_index(d)
            logger.info("[vmhost] created VM %s (%s) for %s", name, vm_uuid, pk[:12])
            return {"vm": self._vm_view(d, role, pk)}
        except BaseException:
            # Leave nothing half-made: an undefined domain pointing at a deleted disk, or a disk no
            # domain owns, are both things an admin would have to find and clean by hand.
            if defined:
                try:
                    await self.backend.undefine(defined, keep_nvram=False)
                except Exception:
                    pass
            if created_dir is not None:
                await asyncio.to_thread(shutil.rmtree, created_dir, True)
            raise
        finally:
            self._host_lock.release()

    async def _op_vm_delete(self, pk, role, args, progress):
        d = await self._domain(pk, role, args)
        if not isinstance(args.get("confirm_name"), str) or args.get("confirm_name") != d.name:
            raise VmHostError("bad_request", "type the VM's name to confirm deleting it")
        if d.meta is None or not self.storage.is_managed_dir(d.uuid):
            raise VmHostError("unsupported", "this VM was not created by PosterChan — delete it with virsh")
        await self._acquire(self._host_lock, "this host")
        try:
            lock = self._vm_lock(d.uuid)
            await self._acquire(lock, "this VM")
            try:
                d = await self.backend.get(d.uuid)
                if d is None:
                    raise VmHostError("not_found", "no such VM")
                self._migration_guard(d)
                if d.state != "shutoff":
                    raise VmHostError("conflict", "shut the VM down before deleting it")
                delete_disks = bool(args.get("delete_disks"))
                vm_dir = self.storage.vm_dir(d.uuid)
                if delete_disks and (self.storage.root / d.uuid).is_symlink():
                    raise VmHostError("bad_request", "refusing to delete through a symlink")
                self.consoles.revoke(d.uuid)
                await self.backend.undefine(d.uuid, keep_nvram=not delete_disks)
                if delete_disks:
                    await asyncio.to_thread(shutil.rmtree, vm_dir, True)
                self._assign.pop(d.uuid, None)
                self._vm_locks.pop(d.uuid, None)
                self._pw_locks.pop(d.uuid, None)
                logger.info("[vmhost] deleted VM %s (%s, disks %s) by %s", d.name, d.uuid,
                            "deleted" if delete_disks else "kept", pk[:12])
                return {"deleted": d.uuid, "disks_deleted": delete_disks}
            finally:
                lock.release()
        finally:
            self._host_lock.release()

    # ---------------------------------------------------------------- assignment
    async def _assignment(self, pk, role, args, add: bool):
        target = _to_hex(args.get("pubkey"))
        if not target:
            raise VmHostError("bad_request", "pubkey must be an npub or a 64-hex key")
        d = await self._domain(pk, role, args)
        lock = self._vm_lock(d.uuid)
        await self._acquire(lock, "this VM")
        try:
            d = await self.backend.get(d.uuid)
            if d is None:
                raise VmHostError("not_found", "no such VM")
            self._migration_guard(d)
            meta = d.meta or domainxml.VmMeta(owner=pk, created=int(self.now()))
            assigned = list(meta.assigned)
            if add and target not in assigned:
                assigned.append(target)
            elif not add and target in assigned:
                assigned.remove(target)
            meta.assigned = assigned
            await self.backend.set_metadata(d.uuid, meta, live=d.state == "running")
            if not add:
                self.consoles.revoke(d.uuid, target)
            after = await self.backend.get(d.uuid) or d
            self._set_index(after)
            return {"vm": self._vm_view(after, role, pk)}
        finally:
            lock.release()

    async def _op_vm_assign(self, pk, role, args, progress):
        return await self._assignment(pk, role, args, True)

    async def _op_vm_unassign(self, pk, role, args, progress):
        return await self._assignment(pk, role, args, False)

    # ---------------------------------------------------------------- console
    async def _op_console_ticket(self, pk, role, args, progress):
        d = await self._domain(pk, role, args)
        if d.state != "running":
            raise VmHostError("conflict", "start the VM before opening its console")
        if not self.consoles.rate_ok(pk):
            raise VmHostError("rate_limited", "too many console requests — wait a minute")
        ep = await self.backend.vnc_endpoint(d.uuid)
        if not ep or not is_loopback(ep[0]):
            raise VmHostError("unsupported", "this VM has no loopback VNC display")
        ttl = self.cfg.ticket_ttl_sec
        lock = self._pw_locks.setdefault(d.uuid, asyncio.Lock())
        async with lock:          # two tickets racing for one VM must agree on ONE password
            # Reuse the password while a ticket issued with it is unexpired (a shared VM), but always SET
            # it again: that extends QEMU's expiry to cover this ticket too.
            pw = self.consoles.current_password(d.uuid) or domainxml.random_vnc_password()
            try:
                await self.backend.set_vnc_password(d.uuid, pw, ttl)
            except BackendError as e:
                # No password, no console: a display QEMU would not protect (one defined without
                # `passwd`) is refused here rather than handed out with a password that guards nothing.
                logger.warning("[vmhost] could not secure the console of %s: %s", d.uuid, e)
                raise BackendError("could not set a console password on this VM's display — it may have "
                                   "been defined without VNC password auth (see docs/VM_HOSTING.md)")
            tok, exp = self.consoles.issue(d.uuid, pk, ttl, password=pw)
        base = self.cfg.public_url
        if base.startswith("https://"):
            ws = "wss://" + base[len("https://"):] + "/ws/vmconsole"
        elif base.startswith("http://"):
            ws = "ws://" + base[len("http://"):] + "/ws/vmconsole"
        else:
            ws = "/ws/vmconsole"
        return {"ws": ws, "ticket": tok, "vnc_password": pw, "exp": exp}

    async def console_target(self, pubkey: str, vm: str) -> tuple:
        """Re-check, at the moment a console socket is opened, that this pubkey may still reach this
        VM and that it is running; returns the loopback (host, port). Raises VmHostError."""
        role = await self.role_of(pubkey)
        if role is None:
            raise VmHostError("forbidden", "you no longer have access to this host")
        d = await self.backend.get(vm)
        if d is None or not self._visible(d, role, pubkey.lower()):
            raise VmHostError("forbidden", "you no longer have access to this VM")
        if d.state != "running":
            raise VmHostError("conflict", "the VM is not running")
        ep = await self.backend.vnc_endpoint(vm)
        if not ep or not is_loopback(ep[0]):
            raise VmHostError("unsupported", "this VM has no loopback VNC display")
        return ep


# The running service, for the console route and the admin status endpoint. Set by transport.start.
_current: Optional[VmHostService] = None


def current() -> Optional[VmHostService]:
    return _current


def set_current(svc: Optional[VmHostService]) -> None:
    global _current
    _current = svc


# ======================================================================================================
# PHASE 3 — cold migration. The state machine lives in migrate.py; this block only registers its ops,
# the peer role and the start/delete/assign guard, so phase-2 edits above never touch these lines.
# ======================================================================================================
ERROR_CODES = ERROR_CODES + tuple(c for c in ("migrating", "aborted") if c not in ERROR_CODES)

MIGRATION_OPS = {
    # client ops (an admin of THIS host; the target independently checks the same person is its admin)
    "vm.migrate.precheck":      ("admin", False),
    "vm.migrate":               ("admin", True),
    "vm.migrate.status":        ("admin", False),
    "vm.migrate.cancel":        ("admin", True),
    "vm.migrate.force_reclaim": ("admin", True),
    # host ↔ host (only the keys in vmhost_peer_hosts). Not journaled: each is idempotent by state.
    "peer.migrate.precheck":    ("peer", False),
    "peer.migrate.begin":       ("peer", False),
    "peer.migrate.status":      ("peer", False),
    "peer.migrate.challenge":   ("peer", False),
    "peer.migrate.commit":      ("peer", False),
    "peer.migrate.ack":         ("peer", False),
    "peer.migrate.abort":       ("peer", False),
}
OPS.update(MIGRATION_OPS)
FEATURES.append("cold-migrate")


def _migration_guard(self, d) -> None:
    """Refuse start/delete/assign while a migration holds the VM — from the journal (authoritative,
    survives a restart) and from the `pc:migration` metadata (survives a lost journal)."""
    why = self.migrator.blocks_start(d.uuid) if self.migrator is not None else None
    if not why and d.meta is not None and d.meta.migration.get("id"):
        mig = self.migrator.store.get(d.meta.migration["id"]) if self.migrator is not None else None
        if mig is None or mig.get("state") not in ("done", "aborted", "reclaimed", "released"):
            why = "this VM is marked as migrating (" + str(d.meta.migration.get("state") or "?") + ")"
    if why:
        raise VmHostError("migrating", why)


def _migration_delegate(op: str):
    async def run(self, pk, role, args, progress):
        if self.migrator is None:
            raise VmHostError("unsupported", "migration is not available on this host")
        from .migrate import MigrationError
        try:
            return await self.migrator.handle_op(op, pk, role, args, progress)
        except MigrationError as e:
            raise VmHostError(e.code, e.message)
    return run


VmHostService.migrator = None
VmHostService._migration_guard = _migration_guard
for _op in MIGRATION_OPS:
    setattr(VmHostService, "_op_" + _op.replace(".", "_"), _migration_delegate(_op))
