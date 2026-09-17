"""ISO transfers are bounded: how many run at once, how much disk they may promise, and how long they hold a slot.

  * a GLOBAL concurrency cap covers fetches and uploads together;
  * every in-flight transfer RESERVES its size, and the free-space check counts those reservations, incoming
    migrations and the unallocated part of every thin VM disk — two 2 GiB uploads into 3 GiB of room must not
    both be accepted, and a host whose VMs have promised their disks all the room has none for ISOs;
  * `iso.fetch` is a tracked BACKGROUND job (`iso.fetch.status`, `iso.fetch.cancel`, admin only) — it used to
    hold an admin busy slot, and the requester's Nostr round trip, for the whole multi-GB download;
  * stale `.part` files and stale migration `.incoming/<id>` directories are removed at startup and periodically;
  * nginx streams the upload route (parsed directives, both shipped configs).
"""
import asyncio
import os
import re
import time
from pathlib import Path

import httpx
import pytest

from app.services.vmhost import isolib
from tests.test_vmhost_phase2 import ADMIN, USER, c, make

GIB = 1 << 30
ROOT = Path(__file__).resolve().parents[1]


def run(coro):
    return asyncio.run(coro)


def gated_fetch(svc, gate, body=b"\x01" * (1 << 20)):
    """The shipped fetch client over a mock wire whose body waits on `gate`."""
    async def stream():
        yield body[:1024]
        await gate.wait()
        yield body[1024:]

    def handler(request):
        return httpx.Response(200, headers={"content-length": str(len(body))}, content=stream())
    svc.fetch_transport = httpx.MockTransport(handler)
    svc.fetch_resolver = lambda host, port: ["93.184.216.34"]


# ------------------------------------------------------------------------------------------ background job
def test_fetch_returns_a_job_at_once_and_status_and_cancel_are_admin_ops(tmp_path):
    svc, be, root = make(tmp_path)

    async def go():
        gate = asyncio.Event()
        gated_fetch(svc, gate)
        events = []

        async def progress(p):
            events.append(p)
        res = await asyncio.wait_for(svc.handle(ADMIN, "iso.fetch", {"url": "https://m.example/a.iso"}, "f1", progress),
                                     1.0)
        assert res["ok"], res
        job = res["result"]["job"]
        assert job["state"] == "running" and job["id"]
        await asyncio.sleep(0.05)
        st = await svc.handle(ADMIN, "iso.fetch.status", {"job": job["id"]}, "s1")
        assert st["ok"] and st["result"]["job"]["state"] == "running", st
        assert (await svc.handle(USER, "iso.fetch.status", {}, "s2"))["error"]["code"] == "forbidden"
        assert (await svc.handle(USER, "iso.fetch.cancel", {"job": job["id"]}, "c0"))["error"]["code"] == "forbidden"
        cancel = await svc.handle(ADMIN, "iso.fetch.cancel", {"job": job["id"]}, "c1")
        assert cancel["ok"] and cancel["result"]["job"]["state"] == "cancelled", cancel
        inc = root / "isos" / ".incoming"
        assert not inc.exists() or not [n for n in os.listdir(inc) if n.endswith(".part")]
        gate.set()
        # a second fetch runs to the end and reports it, with progress on the ORIGINAL request
        gate2 = asyncio.Event()
        gate2.set()
        gated_fetch(svc, gate2)
        res2 = await svc.handle(ADMIN, "iso.fetch", {"url": "https://m.example/b.iso"}, "f2", progress)
        jid = res2["result"]["job"]["id"]
        for _ in range(200):
            st = (await svc.handle(ADMIN, "iso.fetch.status", {"job": jid}, "s3"))["result"]["job"]
            if st["state"] != "running":
                break
            await asyncio.sleep(0.01)
        assert st["state"] == "done" and st["iso"]["id"] == "b.iso", st
        assert (root / "isos" / "b.iso").exists()
        assert any(e.get("phase") == "done" for e in events), events
    run(go())


# ------------------------------------------------------------------------------------------ concurrency
def test_a_global_cap_covers_fetches_and_uploads_together(tmp_path):
    svc, be, root = make(tmp_path)

    async def go():
        gate = asyncio.Event()
        gated_fetch(svc, gate)
        started = []
        for i in range(isolib.ISO_MAX_CONCURRENT):
            started.append(asyncio.create_task(svc.handle(ADMIN, "iso.fetch", {"url": f"https://m.example/{i}.iso"},
                                                          f"f{i}")))
        await asyncio.sleep(0.1)
        extra = await asyncio.wait_for(svc.handle(ADMIN, "iso.fetch", {"url": "https://m.example/x.iso"}, "fx"), 1.0)
        assert not extra["ok"] and extra["error"]["code"] == "busy", extra
        t = await svc.handle(ADMIN, "iso.upload_ticket", {"name": "u.iso", "size": 10}, "t")
        assert t["ok"], t

        async def body():
            yield b"x" * 10
        with pytest.raises(Exception) as e:
            await svc.receive_upload(t["result"]["ticket"], body())
        assert getattr(e.value, "code", "") == "busy", e.value
        gate.set()
        await asyncio.gather(*started)
    run(go())


# ------------------------------------------------------------------------------------------ reservations
def test_in_flight_transfers_reserve_their_size(tmp_path):
    svc, be, root = make(tmp_path)
    thin = 40                                               # the two fake VMs promise 20 GiB each, allocate nothing
    be.stats["disk_free_gib"] = svc.cfg.reserve_disk_gib + thin + 3

    async def go():
        t1 = (await svc.handle(ADMIN, "iso.upload_ticket", {"name": "one.iso", "size": 2 * GIB}, "t1"))
        assert t1["ok"], t1
        gate = asyncio.Event()

        async def slow_body():
            yield b"x" * 1024
            await gate.wait()
        up = asyncio.create_task(svc.receive_upload(t1["result"]["ticket"], slow_body()))
        await asyncio.sleep(0.05)
        t2 = await svc.handle(ADMIN, "iso.upload_ticket", {"name": "two.iso", "size": 2 * GIB}, "t2")
        assert not t2["ok"] and t2["error"]["code"] == "insufficient_capacity", t2
        up.cancel()
        await asyncio.gather(up, return_exceptions=True)
    run(go())


def test_thin_vm_disks_count_against_iso_room(tmp_path):
    svc, be, root = make(tmp_path)
    be.stats["disk_free_gib"] = svc.cfg.reserve_disk_gib + 10     # 10 GiB free, but the VMs promised 40 GiB
    res = c(svc, ADMIN, "iso.upload_ticket", {"name": "a.iso", "size": 1 * GIB}, "t1")
    assert not res["ok"] and res["error"]["code"] == "insufficient_capacity", res
    be.stats["disk_free_gib"] = svc.cfg.reserve_disk_gib + 40 + 10   # room once the promises are covered
    res = c(svc, ADMIN, "iso.upload_ticket", {"name": "a.iso", "size": 1 * GIB}, "t2")
    assert res["ok"], res


# ------------------------------------------------------------------------------------------ stale parts
def test_stale_parts_and_incoming_dirs_are_cleaned(tmp_path):
    svc, be, root = make(tmp_path)
    inc = root / "isos" / ".incoming"
    inc.mkdir(parents=True, exist_ok=True)
    old, fresh = inc / "dead.part", inc / "live.part"
    old.write_bytes(b"x")
    fresh.write_bytes(b"x")
    past = time.time() - 7200
    os.utime(old, (past, past))
    mig_inc = root / ".incoming" / ("ab" * 16)
    mig_inc.mkdir(parents=True)
    (mig_inc / "disk-vda.qcow2.part").write_bytes(b"x")
    os.utime(mig_inc, (past, past))
    run(svc.cleanup_incoming(older_than=3600))
    assert not old.exists() and fresh.exists()
    assert not mig_inc.exists(), "an .incoming directory no migration owns"
    run(svc.cleanup_incoming(older_than=0))
    assert not fresh.exists(), "at startup nothing can own a part"


# ------------------------------------------------------------------------------------------ nginx
def nginx_blocks(text: str) -> list:
    """A small nginx parser: [(name, args, directives, children)] with comments and quoting handled."""
    text = re.sub(r"#[^\n]*", "", text)
    tokens = re.findall(r'"(?:[^"\\]|\\.)*"|\'(?:[^\'\\]|\\.)*\'|[{};]|[^\s{};]+', text)
    pos = 0

    def block():
        nonlocal pos
        out, cur = [], []
        while pos < len(tokens):
            tok = tokens[pos]
            pos += 1
            if tok == ";":
                if cur:
                    out.append((cur[0], cur[1:], None))
                cur = []
            elif tok == "{":
                out.append((cur[0], cur[1:], block()))
                cur = []
            elif tok == "}":
                return out
            else:
                cur.append(tok)
        return out
    return block()


def find_locations(tree):
    for name, args, kids in tree:
        if kids is None:
            continue
        if name == "location":
            yield args, kids
        yield from find_locations(kids)


@pytest.mark.parametrize("rel", ["nginx/posterchanai.conf.example", "docker/proxy/posterchanai.conf"])
@pytest.mark.parametrize("prefix,need", [
    ("/api/vmhost/iso/", {"proxy_request_buffering": ["off"], "client_max_body_size": ["0"],
                          "proxy_read_timeout": ["3600s"]}),
    ("/api/vmhost/transfer/", {"proxy_buffering": ["off"], "proxy_request_buffering": ["off"],
                               "proxy_read_timeout": ["3600s"]}),
])
def test_nginx_streams_the_vmhost_routes(rel, prefix, need):
    tree = nginx_blocks((ROOT / rel).read_text())
    locs = [(args, kids) for args, kids in find_locations(tree)]
    order = [" ".join(a) for a, _ in locs]
    match = [(a, k) for a, k in locs if a == ["^~", prefix]]
    assert len(match) == 1, f"{rel}: location ^~ {prefix} missing or duplicated"
    kids = {name: args for name, args, sub in match[0][1] if sub is None}
    api = next(k for a, k in locs if a == ["^~", "/api/"])
    assert kids.get("proxy_pass") == next(args for name, args, sub in api if name == "proxy_pass"), rel
    for k, v in need.items():
        assert kids.get(k) == v, (rel, prefix, k, kids.get(k))
    assert not any(name == "proxy_set_header" for name, _, _ in match[0][1]), \
        "an array directive here drops every server-level header"
    assert order.index(f"^~ {prefix}") < order.index("^~ /api/"), rel


# ------------------------------------------------------------------------------------------ iso.delete race
def test_iso_delete_cannot_race_an_attach(tmp_path):
    """vm.update attaches under the HOST lock; iso.delete used to check attachments without it, so a delete that
    ran while an attach was mid-define found nothing attached and removed the ISO the VM was being given."""
    svc, be, root = make(tmp_path)
    (root / "isos" / "debian.iso").write_bytes(b"ISO")

    async def go():
        gate = asyncio.Event()
        be.gate["define"] = gate
        upd = asyncio.create_task(svc.handle(ADMIN, "vm.update",
                                             {"vm": "11111111-1111-4111-8111-111111111111", "media": {"iso": "debian.iso"}},
                                             "u1"))
        for _ in range(200):
            if any(call[0] == "define" for call in be.calls):
                break
            await asyncio.sleep(0.005)
        dele = asyncio.create_task(svc.handle(ADMIN, "iso.delete", {"iso": "debian.iso"}, "d1"))
        await asyncio.sleep(0.05)
        assert (root / "isos" / "debian.iso").exists(), "deleted while an attach of it was in progress"
        gate.set()
        be.gate.pop("define", None)
        assert (await upd)["ok"]
        res = await dele
        assert not res["ok"] and res["error"]["code"] in ("conflict", "busy"), res
        assert (root / "isos" / "debian.iso").exists()
    run(go())
