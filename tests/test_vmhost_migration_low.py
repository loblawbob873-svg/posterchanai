"""The smaller migration trust and housekeeping gaps, each through the shipped code.

  * ASSIGNMENTS: the source's `pc:vm` could grant any pubkey access on the TARGET. The target now carries only the
    pubkeys the target admin's own signed authorization lists (the client signs the current assigned list).
  * NOT_FOUND WHILE LOCKED: a source that answers "no such migration" (a lost or replaced journal) is not a source
    that took the VM back — a LOCKED target keeps its copy until an admin force-reclaims.
  * THROTTLE: `vmhost_transfer_max_mbps` held per REQUEST, so parallel range requests multiplied it.
  * NIP-98 REPLAY: a captured transfer header could be replayed for its whole 60 s window.
  * RETAINED COPIES: a `released` source copy was never reaped; admins can list and delete retained copies; a
    cancel after the target was force-kept is a split-brain decision and says so.
"""
import asyncio
import base64
import json
import time

import httpx

from app.services.nostr.event import build_event
from app.services.vmhost import domainxml, migrate
from tests.vmhost_migration_fake import (S, S_HTTPS, T, T_SK, USER, USER_SK, VM, World, seed_vm, until)

EVIL = "e7" * 32


def run(coro):
    return asyncio.run(coro)


def state(host, mig):
    r = host.rec(mig)
    return r and r["state"]


# ------------------------------------------------------------------------------------------ assignments
def test_the_target_grants_only_the_assignments_its_admin_signed(tmp_path):
    async def go():
        w = World(tmp_path)
        try:
            seed_vm(w.S, state="shutoff", snapshots=False, assigned=(USER, EVIL))
            await w.S.svc.refresh_index()
            authz = w.authz(assigned=[USER])            # the admin saw and signed: USER only
            res = await w.call(w.S, "vm.migrate", {"vm": VM, "target": T, "authz": authz})
            assert res["ok"], res
            mig = res["result"]["migration"]["id"]
            await until(lambda: state(w.T, mig) == "done", what="done")
            meta = domainxml.parse_meta(w.T.backend.domains[VM]["meta_xml"])
            assert meta.assigned == [USER], meta.assigned
        finally:
            await w.close()
    run(go())


def test_an_authorization_without_an_assignment_list_carries_none(tmp_path):
    async def go():
        w = World(tmp_path)
        try:
            seed_vm(w.S, state="shutoff", snapshots=False, assigned=(USER,))
            await w.S.svc.refresh_index()
            res = await w.call(w.S, "vm.migrate", {"vm": VM, "target": T, "authz": w.authz(assigned=None)})
            mig = res["result"]["migration"]["id"]
            await until(lambda: state(w.T, mig) == "done", what="done")
            assert domainxml.parse_meta(w.T.backend.domains[VM]["meta_xml"]).assigned == []
        finally:
            await w.close()
    run(go())


# ------------------------------------------------------------------------------------------ not_found while locked
def test_a_locked_target_keeps_its_copy_when_the_source_forgot_the_migration(tmp_path):
    async def go():
        w = World(tmp_path, timing={"contact_deadline": 0.5})
        try:
            seed_vm(w.S, state="shutoff", snapshots=False)
            await w.S.svc.refresh_index()

            async def cut(u):
                w.relay.partition(S, T)
            w.S.backend.hooks["undefine_for_migration"] = cut
            res = await w.call(w.S, "vm.migrate", {"vm": VM, "target": T, "authz": w.authz(), "start_after": False})
            mig = res["result"]["migration"]["id"]
            await until(lambda: state(w.T, mig) == "locked", what="target locked")
            # The source's journal is gone (a restore, a wiped disk): it now answers not_found.
            await w.S.crash()
            (w.S.root / ".state" / "migrations" / f"{mig}.json").unlink()
            await w.S.restart()
            w.relay.heal()
            await asyncio.sleep(1.0)
            assert state(w.T, mig) == "locked", "not_found is not the source taking the VM back"
            assert VM in w.T.backend.domains and (w.T.root / VM / "disk-vda.qcow2").exists()
        finally:
            await w.close()
    run(go())


# ------------------------------------------------------------------------------------------ transfer route
def _nip98(sk, url, created_at=None, nonce=True):
    tags = [["u", url], ["method", "GET"]] + ([["nonce", base64.b16encode(time.time_ns().to_bytes(8, "big")).decode()]]
                                              if nonce else [])
    ev = build_event(sk, 27235, "", tags, created_at=created_at)
    return "Nostr " + base64.b64encode(json.dumps(ev).encode()).decode()


def test_a_transfer_header_is_single_use(tmp_path):
    async def go():
        w = World(tmp_path)
        try:
            seed_vm(w.S, state="shutoff", snapshots=False)
            await w.S.svc.refresh_index()
            w.T.http.gate = asyncio.Event()
            res = await w.call(w.S, "vm.migrate", {"vm": VM, "target": T, "authz": w.authz()})
            mig = res["result"]["migration"]["id"]
            await until(lambda: state(w.S, mig) == "transferring", what="transferring")
            path = f"/api/vmhost/transfer/{mig}/0"
            hdr = _nip98(T_SK, S_HTTPS + path)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app)) as cl:
                first = await cl.get(S_HTTPS + path, headers={"Authorization": hdr, "Range": "bytes=0-9"})
                again = await cl.get(S_HTTPS + path, headers={"Authorization": hdr, "Range": "bytes=0-9"})
            assert first.status_code == 206 and again.status_code == 401, (first.status_code, again.status_code)
            a, b = migrate.nip98_header(T_SK, S_HTTPS + path), migrate.nip98_header(T_SK, S_HTTPS + path)
            assert a != b and json.loads(base64.b64decode(a.split()[1]))["id"] != json.loads(base64.b64decode(b.split()[1]))["id"], \
                "two honest requests in the same second must not share an event id"
            w.T.http.gate.set()
        finally:
            await w.close()
    run(go())


def test_the_throttle_holds_across_parallel_requests(tmp_path):
    async def go():
        w = World(tmp_path)
        try:
            seed_vm(w.S, state="shutoff", snapshots=False, size=4 << 20)
            await w.S.svc.refresh_index()
            w.T.http.gate = asyncio.Event()
            res = await w.call(w.S, "vm.migrate", {"vm": VM, "target": T, "authz": w.authz()})
            mig = res["result"]["migration"]["id"]
            await until(lambda: state(w.S, mig) == "transferring", what="transferring")
            w.S.migrator.mcfg.transfer_max_mbps = 16          # 2 MB/s for the whole migration
            path = f"/api/vmhost/transfer/{mig}/0"

            async def pull(rng):
                async with httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app)) as cl:
                    r = await cl.get(S_HTTPS + path, headers={"Authorization": _nip98(T_SK, S_HTTPS + path),
                                                              "Range": rng})
                    return len(r.content)
            t0 = time.monotonic()
            got = await asyncio.gather(pull("bytes=0-1048575"), pull("bytes=1048576-2097151"),
                                       pull("bytes=2097152-3145727"))
            elapsed = time.monotonic() - t0
            assert got == [1 << 20] * 3
            # 3 MiB at 2 MB/s is ~1.5 s for the migration; per-request pacing let it through in ~0.5 s.
            assert elapsed >= 1.2, f"parallel requests multiplied the limit: {elapsed:.2f}s"
            w.T.http.gate.set()
        finally:
            await w.close()
    run(go())


# ------------------------------------------------------------------------------------------ retained copies
async def locked_world(tmp_path, keep_hours=1):
    w = World(tmp_path, keep_hours=keep_hours)
    seed_vm(w.S, state="shutoff", snapshots=False)
    await w.S.svc.refresh_index()

    async def cut(u):
        w.relay.partition(S, T)
    w.S.backend.hooks["undefine_for_migration"] = cut
    res = await w.call(w.S, "vm.migrate", {"vm": VM, "target": T, "authz": w.authz(), "start_after": False})
    mig = res["result"]["migration"]["id"]
    await until(lambda: state(w.S, mig) == "locked" and state(w.T, mig) == "locked", what="both locked")
    return w, mig


def test_released_copies_are_reaped_by_the_keep_rule_and_admins_can_list_and_delete_them(tmp_path):
    async def go():
        w, mig = await locked_world(tmp_path)
        try:
            r = await w.call(w.S, "vm.migrate.force_reclaim", {"migration": mig, "side": "target", "confirm": "split-brain"})
            assert r["ok"] and state(w.S, mig) == "released", r
            retained = w.S.root / ".retained" / w.S.rec(mig)["retained"]
            lst = await w.call(w.S, "vm.retained.list", {})
            assert lst["ok"] and [x["name"] for x in lst["result"]["retained"]] == [retained.name], lst
            from app.services.vmhost.service import OPS
            assert OPS["vm.retained.list"][0] == OPS["vm.retained.delete"][0] == "admin"
            r = await w.call(w.S, "vm.retained.list", {}, sk=USER_SK, timeout=1.0)
            assert r is None or r["error"]["code"] == "forbidden", r       # a non-admin: refused (or not answered)
            no = await w.call(w.S, "vm.retained.delete", {"name": retained.name})
            assert not no["ok"] and no["error"]["code"] == "bad_request" and retained.exists(), no
            await w.S.migrator.housekeeping()
            assert retained.exists(), "inside the keep window"
            w.S.migrator.store.get(mig)["forced"]["at"] -= 2 * 3600
            await w.S.migrator.housekeeping()
            assert not retained.exists(), "a released copy is reaped by the same rule as an acked one"
        finally:
            await w.close()
    run(go())


def test_an_admin_can_delete_a_stranded_retained_copy_but_never_a_locked_one(tmp_path):
    async def go():
        w, mig = await locked_world(tmp_path, keep_hours=0)
        try:
            name = w.S.rec(mig)["retained"]
            r = await w.call(w.S, "vm.retained.delete", {"name": name, "confirm": "delete"})
            assert not r["ok"] and r["error"]["code"] == "conflict", "a LOCKED migration's copy may be the only one"
            assert (w.S.root / ".retained" / name).exists()
            r = await w.call(w.S, "vm.migrate.force_reclaim", {"migration": mig, "side": "target", "confirm": "split-brain"})
            assert r["ok"], r
            r = await w.call(w.S, "vm.retained.delete", {"name": name, "confirm": "delete"})
            assert r["ok"], r
            assert not (w.S.root / ".retained" / name).exists()
            assert (await w.call(w.S, "vm.retained.delete", {"name": "../../etc", "confirm": "delete"}))["error"]["code"] \
                in ("bad_request", "not_found")
        finally:
            await w.close()
    run(go())


def test_cancel_after_a_force_kept_target_warns_of_split_brain_and_needs_confirm(tmp_path):
    async def go():
        w, mig = await locked_world(tmp_path)
        try:
            r = await w.call(w.S, "vm.migrate.force_reclaim", {"migration": mig, "side": "target", "confirm": "split-brain"})
            assert r["ok"], r
            r = await w.call(w.S, "vm.migrate.cancel", {"migration": mig})
            assert not r["ok"] and "SPLIT-BRAIN" in r["error"]["message"], r
            assert VM not in w.S.backend.domains
            r = await w.call(w.S, "vm.migrate.cancel", {"migration": mig, "confirm": "split-brain"})
            assert r["ok"] and "SPLIT-BRAIN" in r["result"]["warning"], r
            assert state(w.S, mig) == "reclaimed" and VM in w.S.backend.domains
        finally:
            await w.close()
    run(go())
