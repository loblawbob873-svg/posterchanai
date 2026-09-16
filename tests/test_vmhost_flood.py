"""One low-privilege user must not be able to exhaust the host — for everybody else, and above all for
the admins who would have to fix it.

What a single allowed npub could do to the first version, each measured here against the shipped code:
  * send requests as fast as it can sign them: every one was verified, decrypted and executed;
  * make every one of them a `vm.list`: each ran `virsh list` plus three virsh calls PER DOMAIN, so N
    concurrent lists on a host with M VMs were N x (1 + 3M) subprocesses;
  * fill the one shared pending budget (64) with slow operations, after which an ADMIN's request was
    dropped at the door — the person who could revoke the flooder could not even be heard;
  * while the account table was unreadable, make every request (a stranger's too) re-run the admin
    lookup, concurrently and uncached.
"""
import asyncio
import json
import time

from app.services.nostr import bip340, nip44
from app.services.vmhost import domainxml, transport
from app.services.vmhost.config import VmHostConfig
from app.services.vmhost.service import VmHostService
from app.services.vmhost.storage import Storage
from tests.vmhost_fake import FakeBackend

NODE_SK = bytes.fromhex("01" * 32)
ADMIN_SK = bytes.fromhex("02" * 32)
USER_SKS = [bytes.fromhex(f"{0x30 + i:02x}" * 32) for i in range(6)]
pub = lambda sk: bip340.pubkey_from_seckey(sk).hex()  # noqa: E731
NODE, ADMIN = pub(NODE_SK), pub(ADMIN_SK)
USERS = [pub(sk) for sk in USER_SKS]
U1 = "11111111-1111-4111-8111-111111111111"


def make(tmp_path, admin_provider=None, now=time.time):
    root = tmp_path / "vms"
    st = Storage(root)
    st.ensure()
    (root / U1).mkdir()
    cfg = VmHostConfig(enabled=True, storage_dir=str(root), admin_pubkeys=[ADMIN], allowed_pubkeys=USERS)
    be = FakeBackend()
    be.add_domain(U1, "alpha", state="shutoff", meta=domainxml.VmMeta(owner=ADMIN, assigned=USERS))

    async def none():
        return set()
    svc = VmHostService(cfg, be, node_pubkey=NODE, admin_provider=admin_provider or none, storage=st, now=now)
    published = []

    async def publish(ev):
        published.append(ev)
        return True
    return svc, be, transport.Transport(svc, NODE_SK, publish), published


def read(ev, sk):
    return json.loads(nip44.decrypt_from(sk, bytes.fromhex(NODE), ev["content"]))


# ------------------------------------------------------------------------------------ token bucket
def test_one_user_is_rate_limited_before_anything_is_decrypted(tmp_path, monkeypatch):
    svc, be, tr, published = make(tmp_path)
    decrypts = []
    real = transport.nip44.decrypt_from
    monkeypatch.setattr(transport.nip44, "decrypt_from", lambda *a: decrypts.append(1) or real(*a))
    sk = USER_SKS[0]
    reqs = [transport.build_request(sk, NODE, "host.whoami", {}, f"w{i}") for i in range(40)]

    async def go():
        await svc.refresh_index()
        return [await tr.on_event(r) for r in reqs]
    out = asyncio.run(go())
    answered = [r for r in out if r is not None]
    assert 0 < len(answered) < 40, "40 requests in one burst were all executed"
    assert len(decrypts) == len(answered), "a rate-limited request must not be decrypted"


def test_the_bucket_is_per_pubkey_and_a_forgery_cannot_drain_someone_elses(tmp_path):
    svc, be, tr, published = make(tmp_path)
    victim_sk, flooder_sk = USER_SKS[0], USER_SKS[1]
    forged = []
    for i in range(40):
        ev = transport.build_request(flooder_sk, NODE, "host.whoami", {}, f"f{i}")
        ev["pubkey"] = pub(victim_sk)          # claims to be the victim; signature is the flooder's
        forged.append(ev)
    flood = [transport.build_request(flooder_sk, NODE, "host.whoami", {}, f"x{i}") for i in range(40)]
    mine = transport.build_request(victim_sk, NODE, "host.whoami", {}, "mine")

    async def go():
        await svc.refresh_index()
        for e in forged + flood:
            await tr.on_event(e)
        return await tr.on_event(mine)
    assert asyncio.run(go()) is not None, "someone else's flood (or forgeries) spent the victim's budget"


# ------------------------------------------------------------------------------------ domain snapshot
def test_concurrent_lists_share_one_libvirt_read(tmp_path):
    svc, be, tr, published = make(tmp_path)
    gate = asyncio.Event

    async def go():
        be.gate["list_domains"] = g = gate()
        tasks = [asyncio.create_task(svc.handle(USERS[i % 6], "vm.list", {}, f"l{i}")) for i in range(12)]
        await asyncio.sleep(0.05)
        g.set()
        res = await asyncio.gather(*tasks)
        del be.gate["list_domains"]
        again = await svc.handle(USERS[0], "vm.list", {}, "again")
        return res, again
    res, again = asyncio.run(go())
    assert all(r["ok"] for r in res) and again["ok"]
    assert [c for c in be.calls if c[0] == "list_domains"] == [("list_domains",)], \
        "12 concurrent lists and one a moment later must cost ONE libvirt listing"


def test_the_snapshot_expires_and_a_change_invalidates_it(tmp_path):
    clock = [1000.0]
    svc, be, tr, published = make(tmp_path, now=lambda: clock[0])

    async def go():
        await svc.handle(USERS[0], "vm.list", {}, "a")
        await svc.handle(ADMIN, "vm.power", {"vm": U1, "action": "start"}, "p")
        after_change = await svc.handle(USERS[0], "vm.list", {}, "b")
        clock[0] += 10
        await svc.handle(USERS[0], "vm.list", {}, "c")
        return after_change
    after = asyncio.run(go())
    assert after["result"]["vms"][0]["state"] == "running", "a list right after a power-on showed stale state"
    assert len([c for c in be.calls if c[0] == "list_domains"]) == 3


# ------------------------------------------------------------------------------------ separate budgets
def test_a_user_flood_of_slow_operations_cannot_starve_an_admin(tmp_path, monkeypatch):
    monkeypatch.setattr(transport, "MAX_PENDING", 3)
    monkeypatch.setattr(transport, "MAX_BUSY", {"user": 3, "admin": 2}, raising=False)
    svc, be, tr, published = make(tmp_path)

    async def go():
        await svc.refresh_index()
        be.gate["start"] = g = asyncio.Event()
        for i in range(3):
            tr.spawn(transport.build_request(USER_SKS[i], NODE, "vm.power", {"vm": U1, "action": "start"}, f"p{i}"))
        await asyncio.sleep(0.3)                 # the three are now inside libvirt, holding their slots
        tr.spawn(transport.build_request(USER_SKS[3], NODE, "host.whoami", {}, "late-user"))
        tr.spawn(transport.build_request(ADMIN_SK, NODE, "host.whoami", {}, "admin"))
        await asyncio.sleep(0.3)
        admin_answered = any(e["tags"][1] == ["p", ADMIN] for e in published)
        g.set()
        loop = asyncio.get_running_loop()
        await asyncio.gather(*[t for t in list(transport._tasks) if t.get_loop() is loop], return_exceptions=True)
        return admin_answered
    assert asyncio.run(go()), "the admin's request was dropped while users held every slot"
    assert not any(["p", USERS[3]] in e["tags"] for e in published), "the user budget must be bounded"


# ------------------------------------------------------------------------------------ admin lookup
def test_a_failing_admin_lookup_is_single_flight_and_negatively_cached(tmp_path):
    clock = [1000.0]
    calls = []

    async def broken():
        calls.append(1)
        await asyncio.sleep(0.05)
        raise RuntimeError("database is down")
    svc, be, tr, published = make(tmp_path, admin_provider=broken, now=lambda: clock[0])

    async def go():
        roles = await asyncio.gather(*[svc.role_of(p) for p in [ADMIN, NODE] + USERS + ["ab" * 32] * 12])
        after = await svc.role_of("cd" * 32)
        return roles, after
    roles, _ = asyncio.run(go())
    assert roles[0] == "admin" and roles[1] == "admin", "the settings list and node key still count"
    assert len(calls) == 1, f"{len(calls)} concurrent lookups of a failing account table"
    clock[0] += 60
    asyncio.run(svc.role_of(USERS[0]))
    assert len(calls) == 2, "the negative cache must be SHORT: a recovered table is read again"


def test_a_successful_admin_lookup_is_single_flight(tmp_path):
    calls = []

    async def slow():
        calls.append(1)
        await asyncio.sleep(0.05)
        return {pub(bytes.fromhex("77" * 32))}
    svc, be, tr, published = make(tmp_path, admin_provider=slow)

    async def go():
        return await asyncio.gather(*[svc.role_of(pub(bytes.fromhex("77" * 32))) for _ in range(20)])
    assert set(asyncio.run(go())) == {"admin"} and len(calls) == 1
