"""The ISO library, phase 2: fetch an installer by URL, upload one, delete one. All ADMIN-only.

FETCH IS SERVER-SIDE REQUEST FORGERY BY DESIGN, so it goes through the repo's own guard, the same one
the RSS reader and the fediverse bridge use (`rss_service.looks_fetchable` + `is_safe_host`), and — the
part that has already been got wrong once in this repo (search_service.fetch_url_content followed a 302
to 169.254.169.254 with only the FIRST url checked) — redirects are followed BY HAND and every hop is
re-checked before it is requested. It also goes DIRECT, never through the Tor fallback transport: a
multi-GB installer over Tor is not a plan, and the guard is what makes direct safe.

Bytes are streamed to `isos/.incoming/<random>.part` with a running SHA-256 and a hard cap
(min(ISO_MAX_GIB, free disk − reserve)): a Content-Length over the cap is refused before a byte is read,
and a body that grows past it (a lying or absent Content-Length) is cut off and deleted. Only a whole
file is renamed into the library, and never over an existing name — an ISO a VM boots from is not
replaced behind its back.

UPLOAD uses a TICKET, issued over Nostr (`iso.upload_ticket`) to an admin and spent on
`PUT /api/vmhost/iso/{ticket}`: single use (consumed atomically at the start of the PUT, so a copy in a
proxy log is already spent), short-lived, bound to the admin, the declared size and the name. The PUT
re-checks that the ticket's npub is STILL an admin, streams the body to a .part with the same cap and
refuses a body longer than the size it declared.

DELETE refuses an ISO that any VM still references (its metadata `iso`, or a cdrom source on it).
"""
from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .storage import PathEscape, clean_iso_name

logger = logging.getLogger(__name__)

ISO_MAX_GIB = 32
MAX_REDIRECTS = 5
PROGRESS_EVERY = 2.0
UPLOAD_TICKET_TTL = 600
CHUNK = 1 << 20


def _err(code, msg):
    from .service import VmHostError
    return VmHostError(code, msg)


class FetchRefused(Exception):
    def __init__(self, code: str, message: str):
        super().__init__(message)
        self.code = code
        self.message = message


def _guard_default():
    from app.services import rss_service
    return rss_service.looks_fetchable, rss_service.is_safe_host


async def fetch_to_part(url: str, part: Path, max_bytes: int, *, client=None, guard=None, progress=None,
                        now=time.monotonic) -> dict:
    """GET `url` into `part`, re-checking the SSRF guard on EVERY hop. Returns {size, sha256, final_url,
    filename}. Raises FetchRefused (and leaves no part file) on any refusal."""
    import httpx
    looks, safe = guard or _guard_default()
    own = client is None
    if own:
        client = httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=120.0), follow_redirects=False)
    h = hashlib.sha256()
    size = 0
    try:
        cur = url
        for hop in range(MAX_REDIRECTS + 1):
            if not isinstance(cur, str) or not looks(cur) or not await asyncio.to_thread(safe, cur):
                raise FetchRefused("forbidden", "that address is not allowed (private, local or not http/https)"
                                   if hop == 0 else "the download redirected to an address that is not allowed")
            async with client.stream("GET", cur, headers={"User-Agent": "PosterChan-VMHost/1"}) as r:
                if r.status_code in (301, 302, 303, 307, 308):
                    loc = r.headers.get("location")
                    if not loc:
                        raise FetchRefused("backend_error", "a redirect with no location")
                    cur = str(httpx.URL(cur).join(loc))
                    continue
                if r.status_code != 200:
                    raise FetchRefused("backend_error", f"the server answered HTTP {r.status_code}")
                try:
                    declared = int(r.headers.get("content-length") or 0)
                except ValueError:
                    declared = 0
                if declared > max_bytes:
                    raise FetchRefused("insufficient_capacity",
                                       f"the file is {declared // (1 << 20)} MiB — over this host's limit")
                last = 0.0
                fd = await asyncio.to_thread(os.open, str(part), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
                f = os.fdopen(fd, "wb")
                try:
                    async for chunk in r.aiter_bytes(CHUNK):
                        size += len(chunk)
                        if size > max_bytes:
                            raise FetchRefused("insufficient_capacity", "the download grew past this host's limit")
                        h.update(chunk)
                        await asyncio.to_thread(f.write, chunk)
                        if progress and now() - last >= PROGRESS_EVERY:
                            last = now()
                            await progress({"phase": "download", "bytes": size, "total": declared,
                                            "pct": int(100 * size / declared) if declared else None,
                                            "msg": f"{size // (1 << 20)} MiB"})
                    await asyncio.to_thread(f.flush)
                    await asyncio.to_thread(os.fsync, f.fileno())
                finally:
                    f.close()
                path = httpx.URL(cur).path or ""
                return {"size": size, "sha256": h.hexdigest(), "final_url": cur,
                        "filename": path.rsplit("/", 1)[-1]}
        raise FetchRefused("backend_error", "too many redirects")
    except FetchRefused:
        _unlink(part)
        raise
    except Exception as e:
        _unlink(part)
        raise FetchRefused("backend_error", f"download failed: {type(e).__name__}")
    finally:
        if own:
            await client.aclose()


def _unlink(p: Path) -> None:
    try:
        os.unlink(p)
    except OSError:
        pass


# ---------------------------------------------------------------------------------------- upload tickets
@dataclass
class UploadTicket:
    pubkey: str
    name: str
    size: int
    exp: float


class UploadTickets:
    def __init__(self, now=time.time):
        self.now = now
        self._t: dict = {}
        self._lock = threading.Lock()

    def issue(self, pubkey: str, name: str, size: int, ttl: int = UPLOAD_TICKET_TTL) -> tuple:
        tok = secrets.token_urlsafe(32)
        exp = self.now() + ttl
        with self._lock:
            for k in [k for k, v in self._t.items() if v.exp < self.now()]:
                self._t.pop(k, None)
            self._t[tok] = UploadTicket(pubkey=pubkey, name=name, size=int(size), exp=exp)
        return tok, int(exp)

    def consume(self, token) -> UploadTicket | None:
        if not isinstance(token, str) or not token or len(token) > 128:
            return None
        with self._lock:
            t = self._t.pop(token, None)
        if t is None or t.exp < self.now():
            return None
        return t


# ---------------------------------------------------------------------------------------- ops mixin
class IsoOps:
    async def _iso_cap(self) -> int:
        st = await self.backend.host_stats(str(self.storage.root))
        room_gib = int(st.get("disk_free_gib") or 0) - self.cfg.reserve_disk_gib
        return max(0, min(ISO_MAX_GIB, room_gib)) * (1 << 30)

    def _iso_jobs(self) -> dict:
        if not hasattr(self, "_iso_jobs_d"):
            self._iso_jobs_d = {}
        return self._iso_jobs_d

    def _upload_tickets(self) -> UploadTickets:
        if not hasattr(self, "_upload_tickets_o"):
            self._upload_tickets_o = UploadTickets(now=self.now)
        return self._upload_tickets_o

    async def _finish_iso(self, part: Path, name: str, size: int, sha: str) -> dict:
        try:
            target = await asyncio.to_thread(self.storage.new_iso_target, name)
        except FileExistsError:
            _unlink(part)
            raise _err("conflict", f"the library already has {name}")
        except PathEscape:
            _unlink(part)
            raise _err("bad_request", "that is not a usable ISO file name")
        try:
            await asyncio.to_thread(os.link, part, target)      # fails if the name appeared meanwhile
        except FileExistsError:
            _unlink(part)
            raise _err("conflict", f"the library already has {name}")
        finally:
            _unlink(part)
        logger.info("[vmhost] ISO %s added (%d bytes, sha256 %s)", name, size, sha[:16])
        return {"id": name, "name": name, "size": size, "sha256": sha}

    async def _op_iso_fetch(self, pk, role, args, progress):
        if not self.cfg.iso_fetch_enabled:
            raise _err("forbidden", "downloading ISOs by URL is turned off on this host")
        url = args.get("url")
        if not isinstance(url, str) or len(url) > 2048 or not url.strip():
            raise _err("bad_request", "url is required")
        url = url.strip()
        want_name = args.get("name")
        if want_name is not None and not isinstance(want_name, str):
            raise _err("bad_request", "name must be text")
        pre = clean_iso_name(want_name) if want_name else ""
        if want_name and not pre:
            raise _err("bad_request", "that is not a usable ISO file name")
        cap = await self._iso_cap()
        if cap <= 0:
            raise _err("insufficient_capacity", "this host has no free disk for ISOs")
        inc = await asyncio.to_thread(self.storage.iso_incoming)
        part = inc / (secrets.token_hex(12) + ".part")
        job = {"id": part.stem, "url": url[:200], "bytes": 0, "total": 0, "state": "running", "by": pk}
        self._iso_jobs()[job["id"]] = job

        async def prog(p):
            job["bytes"], job["total"] = p.get("bytes", 0), p.get("total", 0)
            if progress:
                await progress(p)
        try:
            try:
                got = await fetch_to_part(url, part, cap, progress=prog,
                                          guard=getattr(self, "fetch_guard", None),
                                          client=getattr(self, "fetch_client", None))
            except FetchRefused as e:
                raise _err(e.code, e.message)
            name = pre or clean_iso_name(got["filename"])
            if not name:
                _unlink(part)
                raise _err("bad_request", "the URL has no usable file name — give one")
            return {"iso": await self._finish_iso(part, name, got["size"], got["sha256"])}
        finally:
            self._iso_jobs().pop(job["id"], None)

    async def _op_iso_upload_ticket(self, pk, role, args, progress):
        name = clean_iso_name(args.get("name")) if isinstance(args.get("name"), str) else ""
        if not name:
            raise _err("bad_request", "give the ISO a file name")
        try:
            size = int(args.get("size"))
        except (TypeError, ValueError):
            raise _err("bad_request", "size (bytes) is required")
        if isinstance(args.get("size"), bool) or size <= 0:
            raise _err("bad_request", "size (bytes) is required")
        cap = await self._iso_cap()
        if size > cap:
            raise _err("insufficient_capacity", f"this host takes ISOs up to {cap // (1 << 20)} MiB right now")
        if await asyncio.to_thread(lambda: os.path.lexists(self.storage.iso_dir / name)):
            raise _err("conflict", f"the library already has {name}")
        tok, exp = self._upload_tickets().issue(pk, name, size)
        base = self.cfg.public_url
        path = "/api/vmhost/iso/" + tok
        return {"ticket": tok, "url": (base + path) if base else path, "name": name, "size": size, "exp": exp}

    async def receive_upload(self, token: str, chunks) -> dict:
        """Called by the PUT route with an async iterator of body chunks. Raises VmHostError."""
        t = self._upload_tickets().consume(token)
        if t is None:
            raise _err("forbidden", "this upload ticket is invalid, expired or already used")
        if await self.role_of(t.pubkey) != "admin":
            raise _err("forbidden", "only a host admin can upload ISOs")
        cap = await self._iso_cap()
        limit = min(t.size, cap)
        if t.size > cap:
            raise _err("insufficient_capacity", "this host no longer has room for that ISO")
        inc = await asyncio.to_thread(self.storage.iso_incoming)
        part = inc / (secrets.token_hex(12) + ".part")
        h = hashlib.sha256()
        size = 0
        fd = await asyncio.to_thread(os.open, str(part), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o640)
        f = os.fdopen(fd, "wb")
        try:
            async for chunk in chunks:
                if not chunk:
                    continue
                size += len(chunk)
                if size > limit:
                    raise _err("bad_request", "the upload is larger than the size its ticket declared")
                h.update(chunk)
                await asyncio.to_thread(f.write, chunk)
            await asyncio.to_thread(f.flush)
            await asyncio.to_thread(os.fsync, f.fileno())
        except BaseException:
            f.close()
            _unlink(part)
            raise
        f.close()
        if size != t.size:
            _unlink(part)
            raise _err("bad_request", f"the upload ended at {size} bytes, not the {t.size} its ticket declared")
        return {"iso": await self._finish_iso(part, t.name, size, h.hexdigest())}

    async def _op_iso_delete(self, pk, role, args, progress):
        iso_id = args.get("iso")
        if not isinstance(iso_id, str):
            raise _err("bad_request", "iso must be an ISO id")
        try:
            p = await asyncio.to_thread(self.storage.iso_path, iso_id)
        except PathEscape:
            raise _err("bad_request", "that is not an ISO from this host's library")
        except FileNotFoundError:
            raise _err("not_found", "no such ISO in this host's library")
        for d in await self.backend.list_domains():
            srcs = {str(x.get("source", "")) for x in (d.disks or [])}
            if (d.meta and d.meta.iso == iso_id) or str(p) in srcs:
                raise _err("conflict", f"{d.name} still uses this ISO — eject it first")
            # The metadata can lag a hand edit; the definition is the authority on what is attached.
            if hasattr(self.backend, "dumpxml"):
                hw = await self._hardware(d.uuid)
                if hw.get("media") == iso_id:
                    raise _err("conflict", f"{d.name} still uses this ISO — eject it first")
        await asyncio.to_thread(os.unlink, p)
        logger.info("[vmhost] ISO %s deleted by %s", iso_id, pk[:12])
        return {"deleted": iso_id}
