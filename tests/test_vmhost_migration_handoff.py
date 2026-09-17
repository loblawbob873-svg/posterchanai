"""The handoff is not finished until the VM is REALLY gone from the source — and until then nobody starts it there.

`handed_off` is journaled before the source undefines its copy (the commit point). If that undefine FAILS (libvirt
busy, a lock, a transient error), the source used to answer the target's retried commit with "handed_off" without
trying again, the target finalized and acked, and the source went to `done` with the VM still defined — two
runnable copies of one machine, and the source's assigned user one `vm.power start` away from a split brain.
"""
import asyncio

from app.services.vmhost import domainxml
from app.services.vmhost.backend import BackendError
from tests.vmhost_migration_fake import ADMIN_SK, T, USER, USER_SK, VM, World, seed_vm, until


def run(coro):
    return asyncio.run(coro)


def state(host, mig):
    r = host.rec(mig)
    return r and r["state"]


async def keep_trying_to_start(w, stop, successes):
    """An admin and the assigned user hammer `vm.power start` on the SOURCE for the whole migration."""
    while not stop.is_set():
        for sk in (USER_SK, ADMIN_SK):
            r = await w.call(w.S, "vm.power", {"vm": VM, "action": "start"}, sk=sk, timeout=2.0)
            if r and r.get("ok"):
                successes.append((sk == USER_SK and "user" or "admin", dict(w.S.backend.domains.get(VM, {}))))
                w.S.backend.domains[VM]["state"] = "shutoff"          # keep probing the same condition
        await asyncio.sleep(0.01)


def test_a_failed_undefine_is_retried_until_the_vm_is_gone_and_it_never_starts_on_the_source(tmp_path):
    async def go():
        w = World(tmp_path, timing={"contact_deadline": 30.0})
        try:
            seed_vm(w.S, state="shutoff", snapshots=False)
            await w.S.svc.refresh_index()
            w.S.backend.fail["undefine_for_migration"] = BackendError("Requested operation is not valid: domain is locked")
            res = await w.call(w.S, "vm.migrate", {"vm": VM, "target": T, "authz": w.authz(), "start_after": False})
            assert res["ok"], res
            mig = res["result"]["migration"]["id"]
            stop, successes = asyncio.Event(), []
            prober = asyncio.create_task(keep_trying_to_start(w, stop, successes))
            await until(lambda: state(w.S, mig) == "handed_off", what="handed off")
            await until(lambda: state(w.T, mig) == "done" and state(w.S, mig) == "done", what="both done")
            stop.set()
            await prober
            calls = [c for c in w.S.backend.calls if c[0] == "undefine_for_migration"]
            assert len(calls) >= 2, "the failed undefine was retried"
            assert VM not in w.S.backend.domains, "done means the VM is gone from the source"
            assert w.S.rec(mig).get("handoff_complete")
            assert successes == [], f"the VM started on the source during the handoff: {successes}"
        finally:
            await w.close()
    run(go())


def test_the_source_never_enters_done_while_the_vm_is_still_defined_there(tmp_path):
    async def go():
        w = World(tmp_path, timing={"contact_deadline": 30.0})
        try:
            seed_vm(w.S, state="shutoff", snapshots=False)
            await w.S.svc.refresh_index()

            async def always_fails(u, keep_nvram):
                w.S.backend.calls.append(("undefine_for_migration", u, keep_nvram))
                raise BackendError("domain is locked")
            w.S.backend.undefine_for_migration = always_fails
            res = await w.call(w.S, "vm.migrate", {"vm": VM, "target": T, "authz": w.authz(), "start_after": False})
            mig = res["result"]["migration"]["id"]
            await until(lambda: len([c for c in w.S.backend.calls if c[0] == "undefine_for_migration"]) >= 4,
                        what="several handoff attempts")
            assert state(w.S, mig) == "handed_off", "not done while the VM is still defined here"
            assert state(w.T, mig) in ("defined", "committed"), "the target does not own it yet either"
            for sk in (USER_SK, ADMIN_SK):
                r = await w.call(w.S, "vm.power", {"vm": VM, "action": "start"}, sk=sk)
                assert r is not None and not r.get("ok"), r
            t = await w.call(w.T, "vm.power", {"vm": VM, "action": "start"})
            assert not t["ok"] and t["error"]["code"] == "migrating", t
        finally:
            await w.close()
    run(go())


def test_start_stays_refused_on_the_source_after_done_if_the_vm_reappears(tmp_path):
    async def go():
        w = World(tmp_path)
        try:
            seed_vm(w.S, state="shutoff", snapshots=False)
            await w.S.svc.refresh_index()
            res = await w.call(w.S, "vm.migrate", {"vm": VM, "target": T, "authz": w.authz(), "start_after": False})
            mig = res["result"]["migration"]["id"]
            await until(lambda: state(w.T, mig) == "done" and state(w.S, mig) == "done", what="done")
            # Somebody puts the old definition back on the source by hand (virsh define of a backup).
            w.S.backend.add_domain(VM, "alpha", meta=domainxml.VmMeta(owner="ab" * 32, created=1, assigned=[USER]))
            await w.S.svc.refresh_index()
            for sk in (USER_SK, ADMIN_SK):
                r = await w.call(w.S, "vm.power", {"vm": VM, "action": "start"}, sk=sk)
                assert r is not None and not r["ok"] and r["error"]["code"] == "migrating", r
            assert w.S.backend.domains[VM]["state"] == "shutoff"
        finally:
            await w.close()
    run(go())
