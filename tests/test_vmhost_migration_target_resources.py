"""A hostile SOURCE must not be able to exhaust the TARGET's disk or memory, or keep it busy for ever.

  * the manifest is read with a hard cap, never buffered whole from whatever the source sends;
  * the manifest's total may not exceed what the precheck reserved, and the precheck counts the space every other
    incoming migration already reserved (two migrations that each fit alone must not both be accepted);
  * a file body is cut at the size the manifest declared — extra bytes are never written;
  * interrupted transfers are retried a BOUNDED number of times in total: a source that sends one chunk and cuts
    the connection every time used to reset the failure count on each chunk and could hold the target for ever.
"""
import asyncio

import httpx

from app.services.vmhost import migrate
from tests.vmhost_migration_fake import S, T, VM, World, seed_vm, until

GIB = 1 << 30


def run(coro):
    return asyncio.run(coro)


def state(host, mig):
    r = host.rec(mig)
    return r and r["state"]


async def begin(w):
    res = await w.call(w.S, "vm.migrate", {"vm": VM, "target": T, "authz": w.authz(), "start_after": False})
    assert res["ok"], res
    return res["result"]["migration"]["id"]


class Endless(httpx.AsyncByteStream):
    def __init__(self, head: bytes, junk: int, on_chunk=None):
        self.head, self.junk, self.on_chunk, self.sent = head, junk, on_chunk, 0

    async def __aiter__(self):
        yield self.head
        self.sent += len(self.head)
        for _ in range(self.junk):
            if self.on_chunk:
                self.on_chunk()
            self.sent += 1 << 20
            yield b"\xee" * (1 << 20)


def test_an_oversized_manifest_is_refused_without_being_buffered(tmp_path):
    async def go():
        w = World(tmp_path)
        try:
            seed_vm(w.S, state="shutoff", snapshots=False)
            await w.S.svc.refresh_index()
            streams = []

            async def huge_manifest(request, inner):
                if request.url.path.endswith("/manifest"):
                    st = Endless(b"{", 64)
                    streams.append(st)
                    return httpx.Response(200, stream=st)
            w.T.http.override = huge_manifest
            mig = await begin(w)
            await until(lambda: state(w.T, mig) == "aborted", what="refused")
            assert streams and max(st.sent for st in streams) <= migrate.MANIFEST_MAX + (2 << 20), \
                "the target kept reading a manifest far past its cap"
            assert VM in w.S.backend.domains
        finally:
            await w.close()
    run(go())


def test_a_manifest_larger_than_the_precheck_is_refused(tmp_path):
    async def go():
        w = World(tmp_path)
        try:
            disk = seed_vm(w.S, state="shutoff", snapshots=False)
            await w.S.svc.refresh_index()
            real = w.S.backend.dumpxml_inactive
            calls = {"n": 0}

            async def grow_after_precheck(u):
                calls["n"] += 1
                if calls["n"] == 2:                       # export: the disk is now 16 MiB bigger than precheck said
                    with open(disk, "ab") as f:
                        f.write(b"\x00" * (16 << 20))
                return await real(u)
            w.S.backend.dumpxml_inactive = grow_after_precheck
            mig = await begin(w)
            await until(lambda: state(w.T, mig) == "aborted", what="refused")
            assert "precheck" in w.T.rec(mig)["error"], w.T.rec(mig)["error"]
            assert not any(p.endswith(f"/{mig}/0") for p, _ in w.T.http.requests), "no file was pulled"
        finally:
            await w.close()
    run(go())


def test_the_precheck_counts_space_other_incoming_migrations_reserved(tmp_path):
    async def go():
        w = World(tmp_path)
        try:
            w.T.backend.stats["disk_free_gib"] = 5 + 3          # reserve 5 GiB → 3 GiB of room
            peer = w.S.migrator.peer(T)
            others = ["cccccccc-3333-4333-8333-cccccccccc0%d" % i for i in range(2)]
            answers = []
            for i, u in enumerate(others):
                args = {"migration": ("%x" % (i + 1)) * 32, "dry_run": False, "authz": w.authz(vm=u),
                        "start_after": False,
                        "vm": {"uuid": u, "name": f"big{i}", "vcpus": 1, "ram_mib": 512,
                               "total_bytes": int(1.5 * GIB), "loader": "", "firmware_auto": True, "iso": ""}}
                answers.append(await w.S.migrator.rpc.call(peer, "peer.migrate.precheck", args, timeout=2, retries=0))
            assert answers[0]["ok"], answers[0]
            assert not answers[1]["ok"] and answers[1]["error"]["code"] == "insufficient_capacity", answers[1]
        finally:
            await w.close()
    run(go())


def test_a_body_longer_than_the_file_is_cut_at_the_declared_size(tmp_path):
    async def go():
        w = World(tmp_path)
        try:
            disk = seed_vm(w.S, state="shutoff", snapshots=False)
            size = disk.stat().st_size
            await w.S.svc.refresh_index()
            over = []

            async def padded(request, inner):
                if "/transfer/" in request.url.path and request.url.path.endswith("/0"):
                    resp = await inner.handle_async_request(request)
                    body = b"".join([c async for c in resp.stream])
                    parts = list((w.T.root / ".incoming").rglob("disk-vda.qcow2.part"))

                    def check():
                        for p in parts or list((w.T.root / ".incoming").rglob("disk-vda.qcow2.part")):
                            if p.exists() and p.stat().st_size > size:
                                over.append(p.stat().st_size)
                    headers = [(k, v) for k, v in resp.headers.items() if k.lower() != "content-length"]
                    return httpx.Response(resp.status_code, headers=headers, stream=Endless(body, 8, check))
            w.T.http.override = padded
            mig = await begin(w)
            await until(lambda: state(w.T, mig) in ("done", "aborted"), what="finished", timeout=20)
            assert over == [], f"bytes past the declared size reached the disk: {over}"
            assert state(w.T, mig) == "done", w.T.rec(mig)
        finally:
            await w.close()
    run(go())


def test_a_source_that_cuts_every_response_after_one_chunk_is_given_up_on(tmp_path):
    async def go():
        w = World(tmp_path)
        try:
            seed_vm(w.S, state="shutoff", snapshots=False, size=8 << 20)
            await w.S.svc.refresh_index()
            tries = []

            class Cut(httpx.AsyncByteStream):
                def __init__(self, data):
                    self.data = data

                async def __aiter__(self):
                    yield self.data
                    raise httpx.ReadError("cut")

            async def trickle(request, inner):
                if "/transfer/" in request.url.path and request.url.path.endswith("/0"):
                    tries.append(1)
                    resp = await inner.handle_async_request(request)
                    first = b""
                    async for c in resp.stream:            # one whole chunk: real progress, then the cut
                        first += c
                        if len(first) >= w.T.migrator.t.chunk:
                            break
                    first = first[:w.T.migrator.t.chunk]
                    headers = [(k, v) for k, v in resp.headers.items() if k.lower() != "content-length"]
                    return httpx.Response(resp.status_code, headers=headers, stream=Cut(first))
            w.T.http.override = trickle
            mig = await begin(w)
            await until(lambda: state(w.T, mig) == "aborted", what="given up", timeout=20)
            assert len(tries) <= 4 * w.T.migrator.t.transfer_attempts + 1, len(tries)
            assert VM in w.S.backend.domains and S
        finally:
            await w.close()
    run(go())
