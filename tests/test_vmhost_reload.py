"""A settings reload must not leave the OLD configuration's request handlers running, or two migrators on one journal.

`transport.reload()` stops the host and starts a new one — typically because an admin just NARROWED the access
lists. A request already admitted under the old configuration used to keep running in a module-level task set that
`stop()` never touched: it finished its libvirt call and published its reply after the new service had started,
acting on the permissions the Save had just taken away. And nothing stopped a new Migrator from opening the same
journal while the old one's tasks were still saving into it (a stale `defining` written over `defined`).
"""
import asyncio

import pytest

from app.services.vmhost import migrate, transport
from app.services.vmhost import service as service_mod
from tests.test_vmhost_integration import ADMIN_SK, NODE_SK, make, req


def run(coro):
    return asyncio.run(coro)


def test_stop_cancels_an_admitted_request_before_it_returns(tmp_path):
    svc, be, root, tr = make(tmp_path)

    async def go():
        published = []

        async def publish(ev):
            published.append(ev)
            return True
        tr.publish = publish
        gate = asyncio.Event()
        be.gate["list_domains"] = gate
        svc.invalidate_domains()
        tr.spawn(req(ADMIN_SK, "vm.list", {}, "slow"))
        for _ in range(100):
            if any(c[0] == "list_domains" for c in be.calls):
                break
            await asyncio.sleep(0.01)
        assert any(c[0] == "list_domains" for c in be.calls), "the request reached libvirt"
        transport._state.update({"transport": tr, "stop": asyncio.Event(), "tasks": [], "lock_fd": None})
        service_mod.set_current(svc)
        await asyncio.wait_for(transport.stop(), 10)
        gate.set()                                   # libvirt answers — after the reload
        await asyncio.sleep(0.1)
        assert published == [], "the old configuration's handler replied after the host was stopped"
        tr.spawn(req(ADMIN_SK, "vm.list", {}, "late"))
        await asyncio.sleep(0.1)
        assert published == [], "a stopped transport must not admit new requests"
    run(go())


def test_two_migrators_never_hold_one_journal(tmp_path):
    svc, be, root, tr = make(tmp_path)               # make() attached a migrator to this storage
    first = svc.migrator

    async def publish(ev):
        return True
    with pytest.raises(migrate.JournalBusy):
        migrate.attach(svc, NODE_SK, publish, {})
    assert svc.migrator is first, "a refused attach must not replace the running migrator"
    run(first.close())
    second = migrate.attach(svc, NODE_SK, publish, {})
    assert second is not first and svc.migrator is second
    run(second.close())


def test_a_closed_migrator_does_not_write_the_journal(tmp_path):
    svc, be, root, tr = make(tmp_path)
    m = svc.migrator
    rec = {"id": "ab" * 16, "role": "source", "state": "planned", "vm": "x", "history": []}
    run(m._save(rec))
    run(m.close())
    rec["state"] = "aborted"
    with pytest.raises(migrate.Superseded):
        run(m._save(rec))
    assert m.store.read("ab" * 16)["state"] == "planned"
