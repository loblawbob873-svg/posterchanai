"""Cold migration between two VM hosts, driven end to end through the SHIPPED transport, service,
migrator and transfer route (see tests/vmhost_migration_fake.py for what is and is not a fixture).

What each test pins, and the failure it exists for:
  * the HAPPY PATH preserves what the VM is — assignments, owner, labels, snapshots (with their embedded
    paths rewritten), autostart — lands the disks byte-identical, starts it when asked, leaves it
    defined ONLY on the target and keeps the source's copy (reaped only after the ack);
  * a CHECKSUM MISMATCH aborts and the VM goes back to running on the source, with nothing left on the
    target — the rule "before the commit point, the source owns it";
  * a transfer CUT mid-file resumes with a Range request from the .part size, not from zero;
  * the SOURCE CRASHING right after journaling the handoff finishes from its journal on restart;
  * the two hosts LOSING EACH OTHER at the commit point lock both sides; only force_reclaim resolves it,
    and it works in both directions;
  * the transfer route refuses every credential that is not the target's fresh signature for that
    exact URL, and any migration that is not transferring;
  * a requester who is not an admin of the TARGET is refused by the target itself;
  * a TPM VM is refused before the target hears anything;
  * start is refused while the VM is outgoing (source) or pending commit (target), across a restart.
"""
import asyncio
import base64
import json
import time

import httpx
import pytest

from app.services.nostr.event import build_event
from app.services.vmhost import migrate
from tests.vmhost_migration_fake import (ADMIN, ADMIN_SK, S, S_HTTPS, T, USER, USER_SK, VM, SimulatedCrash,
                                         World, seed_vm, sha, until)


def run(coro):
    return asyncio.run(coro)


async def start_migration(w, **extra):
    args = {"vm": VM, "target": T, "authz": w.authz(), "start_after": True}
    args.update(extra)
    res = await w.call(w.S, "vm.migrate", args)
    assert res and res["ok"], res
    return res["result"]["migration"]["id"]


def state(host, mig):
    r = host.rec(mig)
    return r and r["state"]


# ------------------------------------------------------------------------------------------ happy path
def test_happy_path_moves_the_vm_and_everything_that_makes_it_that_vm(tmp_path):
    async def go():
        w = World(tmp_path, keep_hours=0)
        try:
            disk = seed_vm(w.S)
            want_disk, want_nvram = sha(disk), sha(disk.parent / "nvram.fd")
            await w.S.svc.refresh_index()
            mig = await start_migration(w)
            await until(lambda: state(w.T, mig) == "done" and state(w.S, mig) == "done", what="both done")

            tb, sb = w.T.backend, w.S.backend
            assert VM in tb.domains and VM not in sb.domains, "defined ONLY on the target at the end"
            d = await tb.get(VM)
            assert d.name == "alpha" and d.autostart is True and d.state == "running", "autostart + start_after"
            assert d.meta.assigned == [USER] and d.meta.owner == ADMIN and d.meta.labels == ["web"]
            assert d.meta.migration == {}, "pending-commit marker cleared"
            tdir = w.T.root / VM
            assert tb.disk_sources(VM) == [str(tdir / "disk-vda.qcow2")], "disk path rewritten by name"
            assert tb.nvram_of(VM) == str(tdir / "nvram.fd")
            assert sha(tdir / "disk-vda.qcow2") == want_disk and sha(tdir / "nvram.fd") == want_nvram
            snaps = tb.snapshots[VM]
            assert [s["name"] for s in snaps] == ["clean-install", "before-upgrade"], "parents first"
            assert [s["current"] for s in snaps] == [False, True]
            assert all(str(w.S.root) not in s["xml"] and str(tdir) in s["xml"] for s in snaps)
            assert not (w.T.root / ".incoming" / mig).exists()

            srec = w.S.rec(mig)
            assert srec["acked_at"], "the target acknowledged"
            retained = w.S.root / ".retained" / srec["retained"]
            assert sha(retained / "disk-vda.qcow2") == want_disk, "the source keeps its copy"
            assert not (w.S.root / VM).exists()
            # The requester is told, from BOTH sides, tagged to the authorization they signed.
            sides = {b["progress"]["side"] for (_, _, b) in w.progress if b.get("id") == mig}
            assert sides == {"source", "target"}
            assert any(b["progress"]["phase"] == "transfer" for (_, _, b) in w.progress)
            # The user it is assigned to reaches it on the target now.
            got = await w.call(w.T, "vm.list", {}, sk=USER_SK)
            assert [v["uuid"] for v in got["result"]["vms"]] == [VM]
            # Reaped only after the ack, and only after the keep window (0h here).
            await w.S.migrator.housekeeping()
            assert not retained.exists()
        finally:
            await w.close()
    run(go())


# ------------------------------------------------------------------------------------------ hash mismatch
def test_a_checksum_mismatch_aborts_and_the_vm_stays_running_on_the_source(tmp_path):
    async def go():
        w = World(tmp_path)
        try:
            disk = seed_vm(w.S)
            await w.S.svc.refresh_index()
            real_call = w.S.migrator.rpc.call

            async def corrupt_then_call(peer, op, args, **kw):
                if op == "peer.migrate.begin":           # exported and hashed; now the bytes change
                    data = bytearray(disk.read_bytes())
                    data[len(data) // 2] ^= 0xFF
                    disk.write_bytes(bytes(data))
                return await real_call(peer, op, args, **kw)
            w.S.migrator.rpc.call = corrupt_then_call
            mig = await start_migration(w)
            await until(lambda: state(w.S, mig) == "aborted" and state(w.T, mig) == "aborted", what="aborted")
            assert "checksum mismatch" in w.T.rec(mig)["error"]
            # The abort is journaled first, then the VM is restored — and restarted, because it had been running.
            await until(lambda: w.S.backend.domains[VM]["state"] == "running", what="restarted on the source")
            d = await w.S.backend.get(VM)
            assert d.autostart is True and d.meta.migration == {}
            assert VM not in w.T.backend.domains
            assert not (w.T.root / VM).exists() and not (w.T.root / ".incoming" / mig).exists()
            ok = await w.call(w.S, "vm.power", {"vm": VM, "action": "reboot"})
            assert ok["ok"], ok
        finally:
            await w.close()
    run(go())


# ------------------------------------------------------------------------------------------ cut + resume
def test_a_transfer_cut_mid_file_resumes_with_a_range_request(tmp_path):
    async def go():
        w = World(tmp_path)
        try:
            disk = seed_vm(w.S, size=2 * 1024 * 1024 + 5)
            await w.S.svc.refresh_index()
            cut = 1024 * 1024 + 123
            w.T.http.cut_after = cut
            mig = await start_migration(w)
            await until(lambda: state(w.T, mig) == "done", what="target done")
            ranges = [r for (p, r) in w.T.http.requests if p.endswith(f"/{mig}/0")]
            # Resumed from what the .part file holds (whole chunks that reached the disk), never from 0.
            starts = [int(r.split("=")[1].rstrip("-")) for r in ranges]
            assert starts[0] == 0 and len(starts) == 2 and 0 < starts[1] <= cut, ranges
            assert w.T.http.cuts == 1
            assert sha(w.T.root / VM / "disk-vda.qcow2") == sha(w.S.root / ".retained" / w.S.rec(mig)["retained"]
                                                                / "disk-vda.qcow2") != ""
            assert disk.name == "disk-vda.qcow2"
        finally:
            await w.close()
    run(go())


# ------------------------------------------------------------------------------------------ source crash
def test_the_source_crashing_after_the_commit_point_finishes_from_its_journal(tmp_path):
    async def go():
        w = World(tmp_path, timing={"contact_deadline": 5.0})
        try:
            seed_vm(w.S)
            await w.S.svc.refresh_index()
            w.S.backend.fail["undefine_for_migration"] = SimulatedCrash()
            mig = await start_migration(w)
            await until(lambda: any(c[0] == "undefine_for_migration" for c in w.S.backend.calls), what="crash")
            await asyncio.sleep(0.05)
            assert state(w.S, mig) == "handed_off", "journaled BEFORE the side effect"
            assert VM in w.S.backend.domains, "the crash happened before the undefine"
            await w.S.crash()
            await w.S.restart()
            await until(lambda: state(w.S, mig) == "done" and state(w.T, mig) == "done", what="recovered")
            assert VM not in w.S.backend.domains and VM in w.T.backend.domains
            assert (w.S.root / ".retained" / w.S.rec(mig)["retained"] / "disk-vda.qcow2").exists()
        finally:
            await w.close()
    run(go())


# ------------------------------------------------------------------------------------------ unreachable
@pytest.mark.parametrize("keeper", ["target", "source"])
def test_losing_contact_at_the_commit_point_locks_both_and_force_reclaim_resolves_it(tmp_path, keeper):
    async def go():
        w = World(tmp_path, keep_hours=0)
        try:
            seed_vm(w.S)
            await w.S.svc.refresh_index()

            async def cut_network(u):
                w.relay.partition(S, T)                   # S hands off; its answer never reaches T
            w.S.backend.hooks["undefine_for_migration"] = cut_network
            mig = await start_migration(w, start_after=False)
            await until(lambda: state(w.S, mig) == "locked" and state(w.T, mig) == "locked", what="both locked")

            # Locked means locked: the pending VM on the target cannot be started ...
            r = await w.call(w.T, "vm.power", {"vm": VM, "action": "start"})
            assert not r["ok"] and r["error"]["code"] == "migrating", r
            # ... the retained source copy is NOT reaped without an ack ...
            await w.S.migrator.housekeeping()
            assert (w.S.root / ".retained" / w.S.rec(mig)["retained"]).exists()
            # ... cancel is too late, and force_reclaim demands the explicit confirmation.
            r = await w.call(w.S, "vm.migrate.cancel", {"migration": mig})
            assert not r["ok"] and r["error"]["code"] == "conflict"
            r = await w.call(w.T, "vm.migrate.force_reclaim", {"migration": mig, "side": keeper})
            assert not r["ok"] and r["error"]["code"] == "bad_request"

            for host in (w.T, w.S):
                r = await w.call(host, "vm.migrate.force_reclaim",
                                 {"migration": mig, "side": keeper, "confirm": "split-brain"})
                assert r["ok"], r
                assert "SPLIT-BRAIN" in r["result"]["warning"]
            if keeper == "target":
                assert state(w.T, mig) == "done" and state(w.S, mig) == "released"
                assert VM in w.T.backend.domains and VM not in w.S.backend.domains
                r = await w.call(w.T, "vm.power", {"vm": VM, "action": "start"})
                assert r["ok"], r
            else:
                assert state(w.S, mig) == "reclaimed" and state(w.T, mig) == "released"
                assert VM in w.S.backend.domains and VM not in w.T.backend.domains
                assert w.S.backend.disk_sources(VM) == [str(w.S.root / VM / "disk-vda.qcow2")]
                assert not (w.T.root / VM).exists()
                d = await w.S.backend.get(VM)
                assert d.meta.assigned == [USER] and d.meta.migration == {}
                r = await w.call(w.S, "vm.power", {"vm": VM, "action": "start"})
                assert r["ok"], r
            # A forced decision is not an ack: the retained copy stays for an admin to remove.
            await w.S.migrator.housekeeping()
            if keeper == "target":
                assert (w.S.root / ".retained" / w.S.rec(mig)["retained"]).exists()
        finally:
            await w.close()
    run(go())


# ------------------------------------------------------------------------------------------ transfer route
def _nip98(sk, url, method="GET", created_at=None):
    ev = build_event(sk, 27235, "", [["u", url], ["method", method]], created_at=created_at)
    return "Nostr " + base64.b64encode(json.dumps(ev).encode()).decode()


def test_the_transfer_route_serves_only_the_target_for_that_url_while_transferring(tmp_path):
    from tests.vmhost_migration_fake import T_SK

    async def go():
        w = World(tmp_path)
        try:
            disk = seed_vm(w.S)
            await w.S.svc.refresh_index()
            w.T.http.gate = asyncio.Event()                # the target never gets to pull
            mig = await start_migration(w)
            await until(lambda: state(w.S, mig) == "transferring", what="transferring")
            client = httpx.AsyncClient(transport=httpx.ASGITransport(app=w.app))
            path = f"/api/vmhost/transfer/{mig}/0"
            url = S_HTTPS + path

            async def get(p, auth, rng=None):
                h = {"Authorization": auth} if auth else {}
                if rng:
                    h["Range"] = rng
                return await client.get(S_HTTPS + p, headers=h)

            ok = await get(path, _nip98(T_SK, url), "bytes=10-99")
            assert ok.status_code == 206 and ok.content == disk.read_bytes()[10:100]
            assert ok.headers["content-range"] == f"bytes 10-99/{disk.stat().st_size}"
            man = await get(f"/api/vmhost/transfer/{mig}/manifest", _nip98(T_SK, S_HTTPS + f"/api/vmhost/transfer/{mig}/manifest"))
            assert man.status_code == 200 and json.loads(man.content)["vm"]["uuid"] == VM

            refusals = {
                "no header": await get(path, None),
                "wrong signer": await get(path, _nip98(ADMIN_SK, url)),
                "stale": await get(path, _nip98(T_SK, url, created_at=int(time.time()) - 120)),
                "future": await get(path, _nip98(T_SK, url, created_at=int(time.time()) + 120)),
                "other file's url": await get(path, _nip98(T_SK, S_HTTPS + f"/api/vmhost/transfer/{mig}/1")),
                "prefix url": await get(f"/api/vmhost/transfer/{mig}/0", _nip98(T_SK, url + "0")),
                "other host": await get(path, _nip98(T_SK, "https://evil.test" + path)),
                "query": await get(path, _nip98(T_SK, url + "?x=1")),
                "method": await get(path, _nip98(T_SK, url, method="POST")),
            }
            assert {k: r.status_code for k, r in refusals.items()} == {k: 401 for k in refusals}
            assert all(disk.read_bytes()[:64] not in r.content for r in refusals.values())
            unknown = await get("/api/vmhost/transfer/" + "f" * 32 + "/0", _nip98(T_SK, S_HTTPS + "/api/vmhost/transfer/" + "f" * 32 + "/0"))
            assert unknown.status_code == 404

            r = await w.call(w.S, "vm.migrate.cancel", {"migration": mig})
            assert r["ok"] and r["result"]["migration"]["state"] == "aborted", r
            late = await get(path, _nip98(T_SK, url))
            assert late.status_code == 409 and not late.content.startswith(disk.read_bytes()[:16])
            w.T.http.gate.set()
            await client.aclose()
        finally:
            await w.close()
    run(go())


# ------------------------------------------------------------------------------------------ authorization
def test_a_requester_who_is_not_an_admin_of_the_target_is_refused_by_the_target(tmp_path):
    async def go():
        w = World(tmp_path, target_admins=[])
        try:
            seed_vm(w.S)
            await w.S.svc.refresh_index()
            r = await w.call(w.S, "vm.migrate", {"vm": VM, "target": T, "authz": w.authz()})
            assert not r["ok"] and r["error"]["code"] == "forbidden" and "not an admin of the target" in r["error"]["message"]
            assert w.S.migrator.store.all() == [] and w.T.migrator.store.all() == []
            d = await w.S.backend.get(VM)
            assert d.state == "running" and d.meta.migration == {}
        finally:
            await w.close()
    run(go())


def test_an_authorization_for_a_different_vm_or_source_is_refused(tmp_path):
    async def go():
        w = World(tmp_path)
        try:
            seed_vm(w.S)
            await w.S.svc.refresh_index()
            other = "bbbbbbbb-1111-4111-8111-bbbbbbbbbbbb"
            for authz in (w.authz(vm=other), w.authz(source=USER), w.authz(created_at=int(time.time()) - 3600)):
                r = await w.call(w.S, "vm.migrate", {"vm": VM, "target": T, "authz": authz})
                assert not r["ok"] and r["error"]["code"] == "forbidden", r
            # Somebody else's signature is refused by the SOURCE before it bothers the target.
            r = await w.call(w.S, "vm.migrate", {"vm": VM, "target": T, "authz": w.authz(sk=USER_SK)})
            assert not r["ok"] and r["error"]["code"] == "bad_request"
        finally:
            await w.close()
    run(go())


def test_peer_keys_and_client_ops_do_not_mix(tmp_path):
    async def go():
        w = World(tmp_path)
        try:
            from tests.vmhost_migration_fake import T_SK
            r = await w.call(w.S, "vm.list", {}, sk=T_SK)
            assert not r["ok"] and r["error"]["code"] == "forbidden", "a peer is not a user"
            r = await w.call(w.S, "peer.migrate.status", {"migration": "0" * 32})
            assert not r["ok"] and r["error"]["code"] == "forbidden", "an admin is not a peer"
            r = await w.call(w.S, "peer.migrate.status", {"migration": "0" * 32}, sk=USER_SK, timeout=0.5)
            assert r is None, "a stranger still gets silence"
        finally:
            await w.close()
    run(go())


def test_a_tpm_vm_is_refused_before_the_target_hears_anything(tmp_path):
    async def go():
        w = World(tmp_path)
        try:
            seed_vm(w.S, guest="windows")
            await w.S.svc.refresh_index()
            before = len([1 for (u, e) in w.relay.published if e["pubkey"] == S])
            r = await w.call(w.S, "vm.migrate", {"vm": VM, "target": T, "authz": w.authz()})
            assert not r["ok"] and r["error"]["code"] == "unsupported" and "TPM" in r["error"]["message"], r
            assert len([1 for (u, e) in w.relay.published if e["pubkey"] == S and e["kind"] == 5310]) == 0
            assert before >= 0 and w.S.migrator.store.all() == []
        finally:
            await w.close()
    run(go())


# ------------------------------------------------------------------------------------------ start refusals
def test_start_is_refused_while_outgoing_and_while_pending_commit_even_after_a_restart(tmp_path):
    async def go():
        w = World(tmp_path, timing={"contact_deadline": 60.0})
        try:
            seed_vm(w.S, state="shutoff")
            await w.S.svc.refresh_index()
            w.T.http.gate = asyncio.Event()
            mig = await start_migration(w, start_after=False)
            await until(lambda: state(w.S, mig) == "transferring", what="transferring")
            r = await w.call(w.S, "vm.power", {"vm": VM, "action": "start"})
            assert not r["ok"] and r["error"]["code"] == "migrating", r
            r = await w.call(w.S, "vm.delete", {"vm": VM, "confirm_name": "alpha"})
            assert not r["ok"] and r["error"]["code"] == "migrating", r
            got = await w.call(w.S, "vm.get", {"vm": VM})
            assert got["result"]["vm"]["migration"]["state"] == "outgoing"

            async def hold_commit(u):
                w.relay.partition(S, T)                   # the target defines, then cannot commit
            w.T.backend.hooks["define"] = hold_commit
            w.T.http.gate.set()
            await until(lambda: state(w.T, mig) in ("defined", "locked"), what="target defined")
            r = await w.call(w.T, "vm.power", {"vm": VM, "action": "start"})
            assert not r["ok"] and r["error"]["code"] == "migrating", r
            await w.T.crash()
            await w.T.restart()
            r = await w.call(w.T, "vm.power", {"vm": VM, "action": "start"})
            assert not r["ok"] and r["error"]["code"] == "migrating", "the refusal survives a restart"
            w.relay.heal()
            await until(lambda: state(w.T, mig) == "done" and state(w.S, mig) == "done", what="healed")
            r = await w.call(w.T, "vm.power", {"vm": VM, "action": "start"})
            assert r["ok"], r
        finally:
            await w.close()
    run(go())


def test_a_vm_that_will_not_shut_down_aborts_unless_forced(tmp_path):
    async def go():
        w = World(tmp_path)
        try:
            seed_vm(w.S)
            await w.S.svc.refresh_index()
            w.S.backend.stubborn = True
            w.S.migrator.mcfg.shutdown_timeout_sec = 0
            mig = await start_migration(w)
            await until(lambda: state(w.S, mig) == "aborted", what="aborted")
            assert "did not shut down" in w.S.rec(mig)["error"]
            await until(lambda: state(w.T, mig) == "aborted", what="target told")
            d = await w.S.backend.get(VM)
            assert d.state == "running" and d.meta.migration == {}
            mig2 = await start_migration(w, force_shutdown=True)
            await until(lambda: state(w.T, mig2) == "done", what="forced migration done")
            assert any(c[0] == "destroy" for c in w.S.backend.calls)
        finally:
            await w.close()
    run(go())


# ------------------------------------------------------------------------------------------ config
def test_peer_hosts_parse_and_report_bad_lines():
    from app.services.nostr import nostr_service
    npub = nostr_service.npub_of(T)
    peers, bad = migrate.parse_peer_hosts(
        f"{npub} wss://t.example/relay https://t.example/\n"
        f"# comment\n{S} wss://s.example/relay https://s.example:8443\n"
        f"{S} wss://dupe/relay https://dupe\n"
        "npub1garbage wss://x/relay https://x\n"
        f"{T} ws://ok/relay http://not-https\n"
        f"{T} https://not-a-relay https://x\n"
        f"{T} wss://only-two\n")
    assert [(p.pubkey, p.relay, p.https) for p in peers] == [
        (T, "wss://t.example/relay", "https://t.example"), (S, "wss://s.example/relay", "https://s.example:8443")]
    assert len(bad) == 4


def test_nginx_streams_the_transfer_route_without_setting_headers():
    """The transfer location must exist in both shipped proxy configs, be unbuffered both ways, and set
    NO proxy_set_header (an array directive: one here would drop every server-level header)."""
    import re
    from pathlib import Path
    root = Path(__file__).resolve().parents[1]
    for rel in ("nginx/posterchanai.conf.example", "docker/proxy/posterchanai.conf"):
        text = (root / rel).read_text()
        m = re.search(r"location \^~ /api/vmhost/transfer/ \{(.*?)\}", text, re.S)
        assert m, rel
        body = m.group(1)
        assert "proxy_buffering off;" in body and "proxy_request_buffering off;" in body, rel
        assert "proxy_set_header" not in body, rel
        assert text.index("location ^~ /api/vmhost/transfer/") < text.index("location ^~ /api/ {"), rel
