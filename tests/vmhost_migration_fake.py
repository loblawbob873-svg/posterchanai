"""Two VM hosts, one fake relay, one real transfer route — the harness for the cold-migration tests.

Everything that makes a migration correct is the SHIPPED code: `Transport.on_event` (real BIP-340 +
NIP-44), `VmHostService.handle`, `Migrator` (journal, state machine, recovery) and the real
`/api/vmhost/transfer/{mig}/{index}` route mounted in a FastAPI app and reached through
`httpx.ASGITransport`. Only three things are fixtures, each the far end of a real protocol:

  * `MigratingFakeBackend` — FakeBackend (which carries the migration primitives: inactive XML, snapshots,
    undefine-for-migration) plus failure hooks. `define` PARSES the XML it is given, so the path rewriting
    is observable;
  * `FakeRelay` — delivers kind-5310/6310/7310 between subscribers, and can PARTITION two keys
    (drop every event authored by one on its way to the other) to model "unreachable";
  * the target's HTTP client, which can be made to cut a stream mid-file or to wait.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import xml.etree.ElementTree as ET

import httpx
from fastapi import FastAPI

from app.services.nostr import bip340, nip44
from app.services.vmhost import domainxml, kinds, migrate, transport
from app.services.vmhost import service as service_mod
from app.services.vmhost.config import VmHostConfig
from app.services.vmhost.service import VmHostService
from app.services.vmhost.storage import Storage
from tests.vmhost_fake import FakeBackend

pub = lambda sk: bip340.pubkey_from_seckey(sk).hex()  # noqa: E731

S_SK = bytes.fromhex("11" * 32)
T_SK = bytes.fromhex("22" * 32)
ADMIN_SK = bytes.fromhex("33" * 32)
USER_SK = bytes.fromhex("44" * 32)
S, T, ADMIN, USER = map(pub, (S_SK, T_SK, ADMIN_SK, USER_SK))
S_RELAY, T_RELAY = "wss://source.test/relay", "wss://target.test/relay"
S_HTTPS, T_HTTPS = "https://source.test", "https://target.test"
VM = "aaaaaaaa-1111-4111-8111-aaaaaaaaaaaa"

FAST = dict(rpc_timeout=1.5, rpc_retries=1, shutdown_poll=0.01, watch_poll=0.05, commit_retry=0.05,
            commit_retry_max=0.1, contact_deadline=0.8, transfer_attempts=4, transfer_backoff=0.01,
            progress_every=0.0, chunk=256 * 1024)


class SimulatedCrash(BaseException):
    """The process dying at this exact line: nothing after it runs, nothing catches it."""


class MigratingFakeBackend(FakeBackend):
    def __init__(self, **kw):
        super().__init__(**kw)
        self.hooks: dict = {}            # method -> async callable run before the method
        self.stubborn = False            # ACPI shutdown ignored

    async def _hook(self, name, *a):
        h = self.hooks.get(name)
        if h is not None:
            await h(*a)

    async def define(self, xml, workdir):
        await self._enter("define", workdir)
        root = ET.fromstring(xml)
        u = root.findtext("uuid").strip()
        mem = int(root.findtext("memory").strip())
        meta = domainxml.parse_meta(xml)
        with open(os.path.join(workdir, "domain.xml"), "w") as f:
            f.write(xml)
        prev = self.domains.get(u, {})
        self.domains[u] = {"name": root.findtext("name"), "state": prev.get("state", "shutoff"),
                           "vcpus": int(root.findtext("vcpu")), "ram_mib": mem,
                           "autostart": prev.get("autostart", False),
                           "meta_xml": meta.to_xml(prefixed=False) if meta else None, "xml": xml}
        await self._hook("define", u)

    def disk_sources(self, u):
        root = ET.fromstring(self.domains[u]["xml"])
        return [d.find("source").get("file") for d in root.iter("disk")
                if d.get("device") == "disk" and d.find("source") is not None]

    def nvram_of(self, u):
        return ET.fromstring(self.domains[u]["xml"]).find("os").findtext("nvram")

    async def shutdown(self, vm_uuid):
        await self._enter("shutdown", vm_uuid)
        if not self.stubborn:
            self.domains[vm_uuid]["state"] = "shutoff"

    # dumpxml_inactive / snapshot_names / snapshot_dumpxml / snapshot_redefine live in FakeBackend now (one
    # snapshot shape for phases 2 and 3). Only the crash hook is added here.
    async def undefine_for_migration(self, vm_uuid, keep_nvram):
        await self._enter("undefine_for_migration", vm_uuid, keep_nvram)
        await self._hook("undefine_for_migration", vm_uuid)
        self.domains.pop(vm_uuid, None)
        self.snapshots.pop(vm_uuid, None)


def _matches(f: dict, ev: dict) -> bool:
    if "kinds" in f and ev.get("kind") not in f["kinds"]:
        return False
    if "authors" in f and ev.get("pubkey") not in f["authors"]:
        return False
    for k, want in f.items():
        if k.startswith("#") and not set(kinds.tag_values(ev, k[1:])) & set(want):
            return False
    return True


class FakeRelay:
    def __init__(self):
        self.subs: list = []             # (url, owner, filter, callback)
        self.published: list = []        # (url, event)
        self.partitioned: set = set()    # frozenset({a, b})

    def partition(self, a, b):
        self.partitioned.add(frozenset((a, b)))

    def heal(self):
        self.partitioned.clear()

    def subscribe(self, url, owner, filt, cb):
        entry = (url, owner, filt, cb)
        self.subs.append(entry)
        return lambda: self.subs.remove(entry) if entry in self.subs else None

    async def publish(self, url, ev) -> bool:
        self.published.append((url, ev))
        for (u, owner, f, cb) in list(self.subs):
            if u != url or not _matches(f, ev):
                continue
            if frozenset((ev.get("pubkey"), owner)) in self.partitioned:
                continue
            r = cb(ev)
            if asyncio.iscoroutine(r):
                await r
        return True


class FakeIO:
    """migrate.RelayRoundtrip's contract over the fake relay: subscribe first, publish, wait."""

    def __init__(self, relay: FakeRelay, owner: str):
        self.relay, self.owner = relay, owner

    async def roundtrip(self, url, event, filt, accept, timeout):
        fut = asyncio.get_running_loop().create_future()

        def cb(ev):
            got = accept(ev)
            if got is not None and not fut.done():
                fut.set_result(got)
        unsub = self.relay.subscribe(url, self.owner, filt, cb)
        try:
            await self.relay.publish(url, event)
            return await asyncio.wait_for(fut, timeout)
        except asyncio.TimeoutError:
            return None
        finally:
            unsub()


# --------------------------------------------------------------------------------------- HTTP
class CutStream(httpx.AsyncByteStream):
    def __init__(self, data: bytes):
        self.data = data

    async def __aiter__(self):
        yield self.data
        raise httpx.ReadError("connection cut mid-file")


class TargetHttp(httpx.AsyncBaseTransport):
    """The target's view of the network: the real route via ASGI, plus a cut and a gate."""

    def __init__(self, app):
        self.inner = httpx.ASGITransport(app=app)
        self.cut_after = None            # bytes: cut the FIRST response for file 0 there
        self.cuts = 0
        self.gate = None                 # asyncio.Event: hold every request until set
        self.requests: list = []

    async def handle_async_request(self, request):
        self.requests.append((request.url.path, request.headers.get("range")))
        if self.gate is not None:
            await self.gate.wait()
        resp = await self.inner.handle_async_request(request)
        if self.cut_after is not None and request.url.path.endswith("/0") and self.cuts == 0 \
                and resp.status_code in (200, 206):
            self.cuts += 1
            body = b"".join([c async for c in resp.stream])
            headers = [(k, v) for k, v in resp.headers.items() if k.lower() != "content-length"]
            return httpx.Response(resp.status_code, headers=headers, stream=CutStream(body[:self.cut_after]))
        return resp


def make_app():
    from app.routers import vmhost as vmhost_router
    app = FastAPI()
    app.include_router(vmhost_router.router)
    return app


# --------------------------------------------------------------------------------------- hosts
class Host:
    def __init__(self, world, name, sk, root, backend, https, relay_url, peer_line, admins, keep_hours=72):
        self.world, self.name, self.sk, self.pk = world, name, sk, pub(sk)
        self.root, self.backend, self.https, self.relay_url = root, backend, https, relay_url
        self.peer_line, self.admins, self.keep_hours = peer_line, admins, keep_hours
        self.unsub = None
        self.boot()

    def boot(self):
        storage = Storage(self.root)
        storage.ensure()
        cfg = VmHostConfig(enabled=True, storage_dir=str(self.root), admin_pubkeys=list(self.admins),
                           public_url=self.https, reserve_disk_gib=5)

        async def no_db_admins():
            return set()
        self.svc = VmHostService(cfg, self.backend, node_pubkey=self.pk, admin_provider=no_db_admins,
                                 storage=storage)
        relay = self.world.relay

        async def publish(ev):
            return await relay.publish(self.relay_url, ev)
        self.http = TargetHttp(self.world.app)
        settings = {"vmhost_peer_hosts": self.peer_line, "vmhost_shutdown_timeout_sec": "5",
                    "vmhost_migration_keep_source_hours": str(self.keep_hours)}
        self.migrator = migrate.attach(self.svc, self.sk, publish, settings,
                                       rpc=migrate.PeerRpc(self.sk, FakeIO(relay, self.pk)),
                                       http_client=lambda: httpx.AsyncClient(transport=self.http),
                                       timing=migrate.Timing(**self.world.timing))
        self.tr = transport.Transport(self.svc, self.sk, publish)
        self.unsub = relay.subscribe(self.relay_url, self.pk, {"kinds": [kinds.REQ_KIND], "#p": [self.pk]},
                                     self.tr.spawn)

    async def crash(self):
        """The process goes away: its subscription, its tasks and its memory. Disk and libvirt stay."""
        self.unsub()
        await self.migrator.close()

    async def restart(self):
        self.boot()
        await self.svc.refresh_index()
        await self.migrator.resume()

    def rec(self, mig):
        return self.migrator.store.read(mig)          # never reload the LIVE store under running code


class World:
    def __init__(self, tmp_path, *, target_admins=None, timing=None, keep_hours=72):
        WORLDS.append(self)
        self.relay = FakeRelay()
        self.app = make_app()
        self.timing = dict(FAST, **(timing or {}))
        self.progress: list = []
        self.S = Host(self, "source", S_SK, tmp_path / "s", MigratingFakeBackend(), S_HTTPS, S_RELAY,
                      f"{T} {T_RELAY} {T_HTTPS}", [ADMIN], keep_hours)
        self.T = Host(self, "target", T_SK, tmp_path / "t", MigratingFakeBackend(), T_HTTPS, T_RELAY,
                      f"{S} {S_RELAY} {S_HTTPS}", [ADMIN] if target_admins is None else target_admins, keep_hours)
        service_mod.set_current(self.S.svc)          # the transfer route answers as the source
        for url in (S_RELAY, T_RELAY):
            self.relay.subscribe(url, ADMIN, {"kinds": [kinds.PROGRESS_KIND], "#p": [ADMIN]}, self._progress)

    def _progress(self, ev):
        body = json.loads(nip44.decrypt_from(ADMIN_SK, bytes.fromhex(ev["pubkey"]), ev["content"]))
        self.progress.append((ev["pubkey"], kinds.tag_values(ev, "e"), body))

    async def call(self, host: Host, op: str, args: dict, sk: bytes = ADMIN_SK, timeout: float = 5.0):
        """A client request through the SHIPPED transport (real signature + NIP-44 both ways)."""
        rid = os.urandom(8).hex()
        ev = transport.build_request(sk, host.pk, op, args, rid)
        io = FakeIO(self.relay, pub(sk))

        def accept(rev):
            if rev.get("kind") != kinds.RES_KIND or ev["id"] not in kinds.tag_values(rev, "e"):
                return None
            return json.loads(nip44.decrypt_from(sk, bytes.fromhex(rev["pubkey"]), rev["content"]))
        return await io.roundtrip(host.relay_url, ev, {"kinds": [kinds.RES_KIND], "authors": [host.pk]},
                                  accept, timeout)

    def authz(self, vm=VM, sk=ADMIN_SK, target=T, source=S, created_at=None):
        return transport.build_request(sk, target, migrate.AUTHZ_OP, {"source": source, "target": target, "vm": vm},
                                       os.urandom(8).hex(), created_at=created_at)

    async def close(self):
        for h in (self.S, self.T):
            await h.migrator.close()
        service_mod.set_current(None)


WORLDS: list = []


async def until(pred, timeout=30.0, what="condition"):
    loop = asyncio.get_running_loop()
    end = loop.time() + timeout
    while loop.time() < end:
        r = pred()
        if asyncio.iscoroutine(r):
            r = await r
        if r:
            return r
        await asyncio.sleep(0.02)
    diag = []
    for w in WORLDS[-1:]:
        for h in (w.S, w.T):
            diag.append((h.name, [(r["state"], r.get("error"), r.get("history")) for r in h.migrator.store.all()]))
    import io
    stacks = io.StringIO()
    for t in asyncio.all_tasks():
        t.print_stack(limit=6, file=stacks)
    raise AssertionError(f"timed out waiting for {what}: {diag}\n{stacks.getvalue()}")


def seed_vm(host: Host, *, guest="linux", state="running", autostart=True, size=3 * 1024 * 1024 + 17,
            snapshots=True, assigned=(USER,), name="alpha"):
    """A PosterChan-made VM on `host`: a real disk file with random bytes, an NVRAM file, the domain
    XML this app generates, and (optionally) two snapshots whose XML embeds the domain."""
    vm_dir = host.root / VM
    vm_dir.mkdir(parents=True)
    disk = vm_dir / "disk-vda.qcow2"
    disk.write_bytes(b"QFI\xfb" + b"\x00\x00\x00\x03" + b"\x00" * 8 + os.urandom(size - 16))
    (vm_dir / "nvram.fd").write_bytes(os.urandom(4096))
    meta = domainxml.VmMeta(owner=ADMIN, created=1700000000, guest=guest, firmware="efi", disk_gib=20,
                            assigned=list(assigned), labels=["web"])
    spec = domainxml.DomainSpec(name=name, uuid=VM, guest=guest, firmware="efi", vcpus=2, ram_mib=2048,
                                disk_path=str(disk), nvram_path=str(vm_dir / "nvram.fd"),
                                network="default", meta=meta)
    xml = domainxml.build_domain_xml(spec)
    be = host.backend
    root = ET.fromstring(xml)
    be.domains[VM] = {"name": name, "state": state, "vcpus": 2, "ram_mib": 2048, "autostart": autostart,
                      "meta_xml": meta.to_xml(prefixed=False), "xml": xml}
    if snapshots:
        dom = ET.tostring(root, encoding="unicode")
        be.snapshots[VM] = [
            {"name": "clean-install", "current": False,
             "xml": f"<domainsnapshot><name>clean-install</name><state>shutoff</state>"
                    f"<disks><disk name='vda' snapshot='internal'/></disks>{dom}</domainsnapshot>"},
            {"name": "before-upgrade", "current": True,
             "xml": f"<domainsnapshot><name>before-upgrade</name><parent><name>clean-install</name></parent>"
                    f"<state>shutoff</state><disks><disk name='vda' snapshot='internal'/></disks>{dom}"
                    f"</domainsnapshot>"},
        ]
    return disk


def sha(path) -> str:
    return hashlib.sha256(open(path, "rb").read()).hexdigest()
