"""The ISO library, phase 2: fetch an installer by URL, upload one, delete one. All ADMIN-only.

FETCH IS SERVER-SIDE REQUEST FORGERY BY DESIGN. The URL passes the repo's cheap syntactic gate
(`rss_service.looks_fetchable`); then the name is resolved ONCE, every address it resolves to must be
public (`ip_blocked`: private, loopback, link-local, multicast, reserved, CGNAT 100.64/10, 192.0.0.0/24,
198.18/15, the NAT64 prefixes, and IPv4-mapped/compatible/6to4 forms of any of those), and the connection
is made to THAT address (`PinnedTransport`) with the Host header and the TLS SNI/certificate check still
bound to the name. Resolving to check and letting the client resolve again to connect is DNS rebinding: a
server that answers public first and 127.0.0.1 second walks past the check. Redirects are followed BY HAND
and every hop is resolved, checked and pinned again (search_service.fetch_url_content once followed a 302
to 169.254.169.254 with only the FIRST url checked). The client ignores HTTP(S)_PROXY (`trust_env=False`)
and never uses the Tor fallback transport: a multi-GB installer over Tor is not a plan, and the pinning is
what makes direct safe.

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
import ipaddress
import logging
import os
import secrets
import socket
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .storage import PathEscape, clean_iso_name

logger = logging.getLogger(__name__)

ISO_MAX_GIB = 32
ISO_MAX_CONCURRENT = 2          # fetches and uploads together, on one host
JOB_KEEP_SEC = 3600             # a finished fetch job stays readable (iso.fetch.status) this long
MAX_JOBS_KEPT = 32
PART_STALE_SEC = 3600
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


_EXTRA_BLOCKED = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "100.64.0.0/10", "192.0.0.0/24", "198.18.0.0/15", "240.0.0.0/4",
    "64:ff9b::/96", "64:ff9b:1::/48", "::/96", "2001::/32", "2002::/16"))


def ip_blocked(addr) -> bool:
    """True for every address an installer download has no business reaching. `is_global` alone is not the rule
    (its answers differ across Python versions), so the classic flags, an explicit list and the embedded-IPv4
    forms are all checked."""
    try:
        ip = ipaddress.ip_address(str(addr).split("%", 1)[0].strip("[]"))
    except ValueError:
        return True
    if isinstance(ip, ipaddress.IPv6Address):
        inner = ip.ipv4_mapped or ip.sixtofour
        if inner is None and int(ip) >> 32 == 0 and int(ip) > 1:
            inner = ipaddress.IPv4Address(int(ip) & 0xFFFFFFFF)          # IPv4-compatible ::a.b.c.d
        if inner is None and ip.teredo:
            inner = ip.teredo[1]
        if inner is not None and ip_blocked(inner):
            return True
    if (ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved
            or ip.is_unspecified or not ip.is_global):
        return True
    return any(ip.version == n.version and ip in n for n in _EXTRA_BLOCKED)


def _resolve(host: str, port: int) -> list:
    return [info[4][0] for info in socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)]


def _host_of(url: str) -> str:
    """Lowercased host of a URL, or '' — used to match a fetch target against this host's OWN Blossom."""
    try:
        import httpx
        return (httpx.URL(str(url or '')).host or '').lower()
    except Exception:
        return ''


class PinnedTransport:
    """An httpx transport that resolves each request's host ONCE, refuses it unless every answer is public, and
    connects to the checked address — keeping the Host header and TLS SNI (and so certificate verification) on
    the name. Wraps a real transport; tests may wrap a mock."""

    def __init__(self, inner=None, resolver=_resolve, allow_host=None):
        import httpx
        self.inner = inner if inner is not None else httpx.AsyncHTTPTransport(retries=0)
        self.resolver = resolver
        # The ONE host whose private/LAN address is permitted (this node's own Blossom). Everything
        # else stays under the full SSRF block. Set only for a self-constructed blob pull.
        self.allow_host = (allow_host or "").lower()

    async def handle_async_request(self, request):
        import httpx
        host = request.url.host
        port = request.url.port or (443 if request.url.scheme == "https" else 80)
        try:
            ipaddress.ip_address(host)
            addrs = [host]
        except ValueError:
            try:
                addrs = await asyncio.to_thread(self.resolver, host, port)
            except (OSError, UnicodeError):
                raise FetchRefused("forbidden", "that address does not resolve")
        if not addrs:
            raise FetchRefused("forbidden", "that address does not resolve")
        trusted = bool(self.allow_host) and (host or "").lower() == self.allow_host
        if not trusted and any(ip_blocked(a) for a in addrs):
            raise FetchRefused("forbidden", "that address is not allowed (private, local or not http/https)")
        ip = str(addrs[0]).split("%", 1)[0]
        ext = dict(request.extensions)
        if request.url.scheme == "https":
            ext["sni_hostname"] = host
        pinned = httpx.Request(request.method, request.url.copy_with(host=ip), headers=request.headers,
                               stream=request.stream, extensions=ext)
        return await self.inner.handle_async_request(pinned)

    async def aclose(self):
        await self.inner.aclose()

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        await self.aclose()


def make_fetch_client(transport=None, resolver=None, allow_host=None):
    """The one way an ISO download talks to the network: pinned, direct, no environment proxy, no redirects.
    `allow_host` (this node's OWN Blossom) may resolve to a private/LAN address; every other host is blocked."""
    import httpx
    return httpx.AsyncClient(transport=PinnedTransport(transport, resolver or _resolve, allow_host=allow_host),
                             trust_env=False, timeout=httpx.Timeout(30.0, read=120.0), follow_redirects=False)


async def fetch_to_part(url: str, part: Path, max_bytes: int, *, transport=None, resolver=None, progress=None,
                        now=time.monotonic, allow_host=None) -> dict:
    """GET `url` into `part`, resolving, checking and PINNING every hop. Returns {size, sha256, final_url,
    filename}. Raises FetchRefused (and leaves no part file) on any refusal."""
    import httpx
    from app.services import rss_service
    client = make_fetch_client(transport, resolver, allow_host=allow_host)
    _allow = (allow_host or "").lower()
    h = hashlib.sha256()
    size = 0
    try:
        cur = url
        for hop in range(MAX_REDIRECTS + 1):
            # hop 0 to this node's OWN Blossom is trusted (it resolves to a LAN address on purpose);
            # a REDIRECT (hop > 0) is never trusted, so it can't be bounced to a private/metadata host.
            _trusted = bool(_allow) and hop == 0 and _host_of(cur) == _allow and str(httpx.URL(cur).scheme) in ("http", "https")
            if not _trusted and (not isinstance(cur, str) or not rss_service.looks_fetchable(cur)):
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
    except FetchRefused as e:
        _unlink(part)
        if hop and e.code == "forbidden":
            raise FetchRefused("forbidden", "the download redirected to an address that is not allowed")
        raise
    except Exception as e:
        _unlink(part)
        raise FetchRefused("backend_error", f"download failed: {type(e).__name__}")
    finally:
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
    # ---- disk this host has promised and not yet written
    def _iso_state(self) -> dict:
        if not hasattr(self, "_iso_state_d"):
            self._iso_state_d = {"uploads": {}, "tasks": {}, "thin": 0}
        return self._iso_state_d

    def _iso_reserved_bytes(self) -> int:
        st = self._iso_state()
        jobs = sum(int(j.get("reserved") or 0) for j in self._iso_jobs().values() if j["state"] == "running")
        return jobs + sum(st["uploads"].values())

    def _iso_active(self) -> int:
        return sum(1 for j in self._iso_jobs().values() if j["state"] == "running") + len(self._iso_state()["uploads"])

    async def refresh_thin_reservation(self) -> int:
        """Bytes every managed VM's disk may still grow by: its provisioned size (pc:vm disk_gib) minus what its
        directory has actually allocated. A thin qcow2 promised 100 GiB occupies almost nothing today."""
        try:
            domains = await self.backend.list_domains()
        except Exception:
            return self._iso_state()["thin"]

        def allocated(vm):
            total = 0
            try:
                with os.scandir(self.storage.vm_dir(vm)) as it:
                    for e in it:
                        if e.is_file(follow_symlinks=False):
                            total += e.stat(follow_symlinks=False).st_blocks * 512
            except (OSError, PathEscape):
                pass
            return total
        promised = 0
        for d in domains:
            if d.meta is not None and d.meta.disk_gib:
                promised += max(0, int(d.meta.disk_gib) * (1 << 30) - await asyncio.to_thread(allocated, d.uuid))
        self._iso_state()["thin"] = promised
        return promised

    def other_reserved_bytes(self) -> int:
        """For the migrator's free-space checks: ISO transfers in flight plus thin-disk growth (last measured)."""
        return self._iso_reserved_bytes() + int(self._iso_state()["thin"])

    async def _iso_cap(self) -> int:
        st = await self.backend.host_stats(str(self.storage.root))
        thin = await self.refresh_thin_reservation()
        incoming = self.migrator.reserved_bytes(include_service=False) if getattr(self, "migrator", None) else 0
        room = (int(st.get("disk_free_gib") or 0) - self.cfg.reserve_disk_gib) * (1 << 30) \
            - self._iso_reserved_bytes() - thin - incoming
        return max(0, min(ISO_MAX_GIB * (1 << 30), room))

    def _iso_jobs(self) -> dict:
        if not hasattr(self, "_iso_jobs_d"):
            self._iso_jobs_d = {}
        return self._iso_jobs_d

    def _job_view(self, job: dict) -> dict:
        return {k: job.get(k) for k in ("id", "url", "bytes", "total", "state", "error", "iso", "started", "ended")}

    def _prune_jobs(self) -> None:
        jobs = self._iso_jobs()
        now = time.time()
        done = sorted((j for j in jobs.values() if j["state"] != "running"), key=lambda j: j.get("ended") or 0)
        for j in done:
            if now - (j.get("ended") or now) > JOB_KEEP_SEC or len(jobs) > MAX_JOBS_KEPT:
                jobs.pop(j["id"], None)

    async def cleanup_incoming(self, older_than: float = PART_STALE_SEC) -> int:
        """Remove `.part` files no running transfer owns, and migration `.incoming/<id>` directories no unfinished
        migration owns, once they are `older_than` seconds old (0 at startup, when nothing can own one)."""
        st = self._iso_state()
        live = {j.get("part") for j in self._iso_jobs().values() if j["state"] == "running"} | set(st["uploads"])
        m = getattr(self, "migrator", None)
        owned = set()
        if m is not None:
            from .migrate import FINAL
            owned = {r["id"] for r in m.store.all() if r["role"] == "target" and r["state"] not in FINAL}
        cutoff = time.time() - older_than

        def sweep():
            n = 0
            inc = self.storage.iso_dir / ".incoming"
            try:
                entries = list(os.scandir(inc))
            except OSError:
                entries = []
            for e in entries:
                if e.name.endswith(".part") and str(inc / e.name) not in live and e.is_file(follow_symlinks=False) \
                        and e.stat(follow_symlinks=False).st_mtime <= cutoff:
                    _unlink(inc / e.name)
                    n += 1
            import re
            import shutil
            minc = self.storage.root / ".incoming"
            try:
                entries = list(os.scandir(minc))
            except OSError:
                entries = []
            for e in entries:
                if re.fullmatch(r"[0-9a-f]{32}", e.name) and e.name not in owned and e.is_dir(follow_symlinks=False) \
                        and e.stat(follow_symlinks=False).st_mtime <= cutoff:
                    shutil.rmtree(minc / e.name, ignore_errors=True)
                    n += 1
            return n
        n = await asyncio.to_thread(sweep)
        if n:
            logger.info("[vmhost] removed %d stale incoming transfer file(s)", n)
        return n

    async def close_background(self) -> None:
        tasks = list(self._iso_state()["tasks"].values())
        for t in tasks:
            t.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

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
        """Start a download and answer AT ONCE with the job: a multi-GB fetch must not hold the admin's busy slot or
        their request open for its whole length. Progress goes out as 7310 events tagged to this request; the job is
        read with iso.fetch.status and stopped with iso.fetch.cancel."""
        if not self.cfg.iso_fetch_enabled:
            raise _err("forbidden", "downloading ISOs by URL is turned off on this host")
        url = args.get("url")
        blob = args.get("blob")
        allow_host = ""
        if blob is not None:
            # Pull an ISO the client already uploaded to Blossom (the 5 GB blob store), by sha256. The
            # host builds the URL from ITS OWN trusted blossom_public_url — the client never supplies a
            # URL — so there is no inbound route to this host to arrange and no SSRF surface to widen.
            sha = str(blob).strip().lower()
            if not re.fullmatch(r"[0-9a-f]{64}", sha):
                raise _err("bad_request", "blob must be a sha256 hex digest")
            base = (self.cfg.blossom_url or "").rstrip("/")
            if not base:
                raise _err("forbidden", "this host has no Blossom server configured to pull an ISO from")
            url = base + "/" + sha
            allow_host = _host_of(base)
        else:
            if not isinstance(url, str) or len(url) > 2048 or not url.strip():
                raise _err("bad_request", "url is required")
            url = url.strip()
            _bh = _host_of(self.cfg.blossom_url or "")
            if _bh and _host_of(url) == _bh:
                allow_host = _bh          # a pasted URL on our OWN Blossom is trusted like a blob pull
        want_name = args.get("name")
        if want_name is not None and not isinstance(want_name, str):
            raise _err("bad_request", "name must be text")
        pre = clean_iso_name(want_name) if want_name else ""
        if want_name and not pre:
            raise _err("bad_request", "that is not a usable ISO file name")
        from app.services import rss_service
        if not allow_host and not rss_service.looks_fetchable(url):
            raise _err("forbidden", "that address is not allowed (private, local or not http/https)")
        if self._iso_active() >= ISO_MAX_CONCURRENT:
            raise _err("busy", f"this host already runs {ISO_MAX_CONCURRENT} ISO transfers — try again when one ends")
        cap = await self._iso_cap()
        if cap <= 0:
            raise _err("insufficient_capacity", "this host has no free disk for ISOs")
        if self._iso_active() >= ISO_MAX_CONCURRENT:                 # re-checked after the await above
            raise _err("busy", f"this host already runs {ISO_MAX_CONCURRENT} ISO transfers — try again when one ends")
        inc = await asyncio.to_thread(self.storage.iso_incoming)
        part = inc / (secrets.token_hex(12) + ".part")
        job = {"id": part.stem, "url": url[:200], "bytes": 0, "total": 0, "state": "running", "by": pk,
               "reserved": cap, "part": str(part), "error": "", "iso": None, "started": int(time.time()), "ended": 0}
        self._prune_jobs()
        self._iso_jobs()[job["id"]] = job
        task = asyncio.create_task(self._run_fetch_job(job, url, pre, part, cap, progress, allow_host=allow_host))
        self._iso_state()["tasks"][job["id"]] = task
        task.add_done_callback(lambda t, jid=job["id"]: self._iso_state()["tasks"].pop(jid, None))
        return {"job": self._job_view(job)}

    async def _run_fetch_job(self, job: dict, url: str, pre: str, part: Path, cap: int, progress, allow_host: str = "") -> None:
        async def tell(p):
            if progress:
                try:
                    await progress(dict(p, job=job["id"]))
                except Exception:
                    pass

        async def prog(p):
            job["bytes"], job["total"] = p.get("bytes", 0), p.get("total", 0)
            if job["total"]:
                job["reserved"] = min(cap, int(job["total"]))           # the size is known: reserve only that
            await tell(p)
        try:
            try:
                got = await fetch_to_part(url, part, cap, progress=prog,
                                          transport=getattr(self, "fetch_transport", None),
                                          resolver=getattr(self, "fetch_resolver", None),
                                          allow_host=allow_host)
            except FetchRefused as e:
                raise _err(e.code, e.message)
            name = pre or clean_iso_name(got["filename"])
            if not name:
                raise _err("bad_request", "the URL has no usable file name — give one")
            job["bytes"] = got["size"]
            job["iso"] = await self._finish_iso(part, name, got["size"], got["sha256"])
            job["state"] = "done"
            await tell({"phase": "done", "msg": "added " + name, "iso": job["iso"]})
        except asyncio.CancelledError:
            job["state"], job["error"] = "cancelled", "cancelled by an admin"
            _unlink(part)
            raise
        except Exception as e:
            job["state"] = "failed"
            job["error"] = getattr(e, "message", None) or type(e).__name__
            job["code"] = getattr(e, "code", "backend_error")
            _unlink(part)
            logger.info("[vmhost] ISO fetch %s failed: %s", job["id"], job["error"])
            await tell({"phase": "failed", "msg": job["error"], "code": job["code"]})
        finally:
            job["ended"] = int(time.time())
            job["reserved"] = 0

    async def _op_iso_fetch_status(self, pk, role, args, progress):
        self._prune_jobs()
        jid = args.get("job")
        if jid is None:
            return {"jobs": [self._job_view(j) for j in self._iso_jobs().values()]}
        job = self._iso_jobs().get(jid) if isinstance(jid, str) else None
        if job is None:
            raise _err("not_found", "no such download")
        return {"job": self._job_view(job)}

    async def _op_iso_fetch_cancel(self, pk, role, args, progress):
        jid = args.get("job")
        job = self._iso_jobs().get(jid) if isinstance(jid, str) else None
        if job is None:
            raise _err("not_found", "no such download")
        task = self._iso_state()["tasks"].get(jid)
        if job["state"] == "running" and task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        return {"job": self._job_view(job)}

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
        if self._iso_active() >= ISO_MAX_CONCURRENT:
            raise _err("busy", f"this host already runs {ISO_MAX_CONCURRENT} ISO transfers — try again when one ends")
        cap = await self._iso_cap()
        limit = min(t.size, cap)
        if t.size > cap:
            raise _err("insufficient_capacity", "this host no longer has room for that ISO")
        if self._iso_active() >= ISO_MAX_CONCURRENT:
            raise _err("busy", f"this host already runs {ISO_MAX_CONCURRENT} ISO transfers — try again when one ends")
        inc = await asyncio.to_thread(self.storage.iso_incoming)
        part = inc / (secrets.token_hex(12) + ".part")
        uploads = self._iso_state()["uploads"]
        uploads[str(part)] = t.size                                  # reserved until this upload ends, either way
        try:
            return await self._receive_into(t, part, limit, chunks)
        finally:
            uploads.pop(str(part), None)

    async def _receive_into(self, t, part: Path, limit: int, chunks) -> dict:
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
        # Under the HOST lock, like vm.create and vm.update — the only ops that attach an ISO. Checked outside it, an
        # attach that was mid-define looked like "nothing uses this" and the ISO went out from under it.
        await self._acquire(self._host_lock, "this host")
        try:
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
        finally:
            self._host_lock.release()
        logger.info("[vmhost] ISO %s deleted by %s", iso_id, pk[:12])
        return {"deleted": iso_id}
