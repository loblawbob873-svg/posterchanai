"""Where the VM-hosting phases meet — each rule here only exists because two branches were merged, and
each is driven through the SHIPPED service/transport, never a copy.

  * SESSION KEYS (phase 2) and MIGRATION (phase 3): `vm.migrate.*` needs the admin's REAL key
    (`step_up_required`), and `peer.migrate.*` needs a configured peer host — a session, an admin or a
    user cannot reach it, and a peer host cannot reach anything else.
  * LOAD LIMITS (phase 1 fixes) and SESSIONS: a session key spends its OWNER's bucket, so opening
    sessions buys no extra budget.
  * LOAD LIMITS and MIGRATION: a peer host has its own bucket and busy slots, so a user flood cannot
    stall a migration's peer ops mid-handoff — even when the peer key is ALSO on the user list.
  * THE VNC PASSWORD (phase 1 fixes) and REDEFINES (phases 2 and 3): `virsh dumpxml` omits `passwd`, so
    `vm.update` and a migration's rewritten definition must put one back — or QEMU runs the display
    with no authentication.
  * THE MIGRATION GUARD (phase 3) and HARDWARE/SNAPSHOT OPS (phase 2): the journal holds a VM before its
    metadata tag exists; update/snapshots must refuse it as start/delete/assign do.
"""
import asyncio
import json
import time
from pathlib import Path

import pytest

from app.services.nostr import bip340, nip44, nostr_service
from app.services.nostr.event import build_event
from app.services.vmhost import domainxml, migrate, sessions, transport
from app.services.vmhost.config import VmHostConfig
from app.services.vmhost.service import OPS, VmHostService
from app.services.vmhost.sessions import SESSION_OPS
from app.services.vmhost.storage import Storage
from tests.vmhost_fake import FakeBackend

NODE_SK = bytes.fromhex("01" * 32)
ADMIN_SK = bytes.fromhex("02" * 32)
USER_SK = bytes.fromhex("03" * 32)
SESS_SK = bytes.fromhex("06" * 32)
PEER_SK = bytes.fromhex("07" * 32)
FLOOD_SKS = [bytes.fromhex(f"{0x40 + i:02x}" * 32) for i in range(4)]
pub = lambda sk: bip340.pubkey_from_seckey(sk).hex()  # noqa: E731
NODE, ADMIN, USER, SESS, PEER = map(pub, (NODE_SK, ADMIN_SK, USER_SK, SESS_SK, PEER_SK))
FLOODERS = [pub(sk) for sk in FLOOD_SKS]
U1 = "11111111-1111-4111-8111-111111111111"
MIGRATION_OPS = sorted(op for op in OPS if op.startswith(("vm.migrate", "peer.migrate")))


def run(coro):
    return asyncio.run(coro)


def make(tmp_path, *, allowed=(), peer_also_user=False):
    root = tmp_path / "vms"
    storage = Storage(root)
    storage.ensure()
    allowed = list(allowed) + [USER] + ([PEER] if peer_also_user else [])
    cfg = VmHostConfig(enabled=True, storage_dir=str(root), admin_pubkeys=[ADMIN], allowed_pubkeys=allowed)
    be = FakeBackend()
    (root / U1).mkdir()
    be.add_domain(U1, "alpha", state="shutoff",
                  meta=domainxml.VmMeta(owner=ADMIN, created=1, disk_gib=20, assigned=[USER]))

    async def admins():
        return set()
    svc = VmHostService(cfg, be, node_pubkey=NODE, admin_provider=admins, storage=storage)
    published = []

    async def publish(ev):
        published.append(ev)
        return True
    line = f"{nostr_service.npub_of(PEER)} wss://peer.example/relay https://peer.example"
    migrate.attach(svc, NODE_SK, publish, {"vmhost_peer_hosts": line})
    run(svc.refresh_index())
    return svc, be, root, transport.Transport(svc, NODE_SK, publish)


def req(sk, op, args, rid):
    return transport.build_request(sk, NODE, op, args, rid)


def answer(tr, ev, sk):
    out = run(tr.on_event(ev))
    return None if out is None else json.loads(nip44.decrypt_from(sk, bytes.fromhex(out["pubkey"]), out["content"]))


def open_session(tr, owner_sk=USER_SK, spk_sk=SESS_SK):
    exp = int(time.time()) + 3600
    proof = build_event(spk_sk, sessions.PROOF_KIND, sessions.proof_content(pub(owner_sk), exp, NODE), [])
    res = answer(tr, req(owner_sk, "session.open", {"pk": pub(spk_sk), "exp": exp, "scope": "use", "proof": proof},
                         "open"), owner_sk)
    assert res and res["ok"], res


# ================================================================================ who may call migration ops
def test_the_migration_ops_are_registered_and_none_is_a_session_op():
    assert "vm.migrate" in OPS and "peer.migrate.commit" in OPS, "phase 3's ops are missing from the merged table"
    assert "vm.update" in OPS and "session.open" in OPS, "phase 2's ops are missing from the merged table"
    assert not (set(MIGRATION_OPS) & SESSION_OPS), "a session key may never move a VM between hosts"
    assert {OPS[op][0] for op in MIGRATION_OPS if op.startswith("vm.migrate")} == {"admin"}
    assert {OPS[op][0] for op in MIGRATION_OPS if op.startswith("peer.migrate")} == {"peer"}


@pytest.mark.parametrize("op", MIGRATION_OPS)
def test_an_admins_session_key_cannot_reach_any_migration_op(tmp_path, op):
    svc, be, root, tr = make(tmp_path)
    open_session(tr, owner_sk=ADMIN_SK)
    res = answer(tr, req(SESS_SK, op, {"vm": U1}, "m1"), SESS_SK)
    assert res is not None and res["error"]["code"] == "step_up_required", (op, res)


@pytest.mark.parametrize("op", [o for o in MIGRATION_OPS if o.startswith("peer.migrate")])
def test_peer_ops_are_peer_only(tmp_path, op):
    svc, be, root, tr = make(tmp_path)
    for sk in (ADMIN_SK, USER_SK):
        res = answer(tr, req(sk, op, {}, "p-" + pub(sk)[:6]), sk)
        assert res is not None and res["error"]["code"] == "forbidden", (op, pub(sk)[:8], res)


def test_a_peer_host_can_call_nothing_but_peer_ops(tmp_path):
    svc, be, root, tr = make(tmp_path)
    for i, op in enumerate(("host.whoami", "vm.list", "vm.migrate.status", "session.open", "vm.power")):
        res = answer(tr, req(PEER_SK, op, {"vm": U1, "action": "start"}, f"x{i}"), PEER_SK)
        assert res is not None and res["error"]["code"] == "forbidden", (op, res)
    # ...and a peer op it CAN call gets past the gate (the migration itself answers, not the gate).
    res = answer(tr, req(PEER_SK, "peer.migrate.status", {"migration": "0" * 32}, "ok1"), PEER_SK)
    assert res is not None and (res["ok"] or res["error"]["code"] != "forbidden"), res


# ================================================================================ budgets
def test_a_session_key_spends_its_owners_bucket(tmp_path):
    svc, be, root, tr = make(tmp_path)
    open_session(tr)
    burst = transport.BUCKETS["user"][0]
    answered = [answer(tr, req(USER_SK, "host.whoami", {}, f"w{i}"), USER_SK) for i in range(burst + 5)]
    assert any(a is None for a in answered), "the owner's bucket never ran out"
    assert answer(tr, req(SESS_SK, "host.whoami", {}, "via-session"), SESS_SK) is None, \
        "a session key had a fresh bucket of its own: every session opened multiplies the owner's budget"


def test_a_fresh_owner_is_answered_through_its_session(tmp_path):
    """The control for the test above: the session path itself works when the owner has budget."""
    svc, be, root, tr = make(tmp_path)
    open_session(tr)
    res = answer(tr, req(SESS_SK, "host.whoami", {}, "via-session"), SESS_SK)
    assert res is not None and res["ok"], res


@pytest.mark.parametrize("peer_also_user", [False, True])
def test_a_user_flood_cannot_stall_a_peer_host(tmp_path, peer_also_user):
    """Every user busy slot held by slow operations (and the peer's own key possibly ON the user list):
    a peer's migration op is still admitted, on its own slots and bucket."""
    svc, be, root, tr = make(tmp_path, allowed=FLOODERS, peer_also_user=peer_also_user)

    async def go():
        gate = be.gate["list_domains"] = asyncio.Event()
        svc.invalidate_domains()
        n = transport.MAX_BUSY["user"]
        flood = [asyncio.create_task(tr.on_event(req(FLOOD_SKS[i % len(FLOOD_SKS)], "vm.list", {}, f"f{i}")))
                 for i in range(n)]
        for _ in range(200):
            if tr._busy["user"] >= n:
                break
            await asyncio.sleep(0.005)
        assert tr._busy["user"] >= n, "the flood did not fill the user slots — the test proves nothing"
        out = await tr.on_event(req(PEER_SK, "peer.migrate.status", {"migration": "0" * 32}, "peer-1"))
        gate.set()
        await asyncio.gather(*flood)
        return out
    out = run(go())
    assert out is not None, "a peer host's request was dropped behind a user flood"


def test_peer_hosts_have_their_own_bucket_and_slots():
    assert transport.BUCKETS["peer"][0] > transport.BUCKETS["user"][0]
    assert transport.MAX_BUSY["peer"] > 0


# ================================================================================ VNC password on redefine
def _vnc(xml):
    g = domainxml.parse_domain(xml).find("devices/graphics[@type='vnc']")
    return g


def test_the_fake_reports_definitions_the_way_libvirt_does(tmp_path):
    """Without this the two tests below prove nothing: libvirt's dumpxml leaves passwd OUT."""
    svc, be, root, tr = make(tmp_path)
    assert _vnc(be.domains[U1]["xml"]).get("passwd")
    assert _vnc(run(be.dumpxml(U1))).get("passwd") is None
    assert _vnc(run(be.dumpxml_inactive(U1))).get("passwd") is None


def test_vm_update_never_defines_a_display_without_a_password(tmp_path):
    svc, be, root, tr = make(tmp_path)
    res = run(svc.handle(ADMIN, "vm.update", {"vm": U1, "vcpus": 3}, "u1"))
    assert res["ok"], res
    g = _vnc(be.domains[U1]["xml"])
    assert g.get("passwd") and g.get("passwdValidTo") == domainxml.VNC_PASSWD_EXPIRED, \
        "vm.update redefined the VM from dumpxml and dropped the VNC password: QEMU would run it with NO auth"


def test_a_migrated_definition_gets_a_password_and_a_loopback_display(tmp_path):
    xml = domainxml.build_domain_xml(domainxml.DomainSpec(
        name="alpha", uuid=U1, guest="linux", firmware="efi", vcpus=2, ram_mib=2048,
        disk_path="/elsewhere/disk-vda.qcow2", nvram_path="/elsewhere/nvram.fd"))
    root = domainxml.parse_domain(xml)
    g = root.find("devices/graphics[@type='vnc']")
    for attr in ("passwd", "passwdValidTo"):
        del g.attrib[attr]                                   # what --migratable reports
    g.set("listen", "0.0.0.0")
    for li in g.findall("listen"):
        li.set("address", "0.0.0.0")
    out = migrate.rewrite_domain_xml(domainxml.to_text(root), Path("/vms") / U1, {"disk-vda.qcow2", "nvram.fd"},
                                     domainxml.VmMeta(owner=ADMIN), lambda name: None)
    g2 = _vnc(out)
    assert g2.get("passwd") and g2.get("passwdValidTo") == domainxml.VNC_PASSWD_EXPIRED
    assert g2.get("listen") == "127.0.0.1"
    assert [(li.get("type"), li.get("address")) for li in g2.findall("listen")] == [("address", "127.0.0.1")]


# ================================================================================ the migration guard
def test_update_and_snapshots_refuse_a_vm_the_migration_journal_holds(tmp_path):
    """No `pc:migration` tag yet (the source writes it while quiescing) — only the journal knows."""
    svc, be, root, tr = make(tmp_path)
    assert not domainxml.parse_meta(be.domains[U1]["meta_xml"]).migration
    svc.migrator.store.put({"id": "ab" * 16, "role": "source", "state": "exporting", "vm": U1})
    for i, (op, args) in enumerate((("vm.update", {"vm": U1, "vcpus": 3}),
                                    ("vm.snapshot.create", {"vm": U1, "name": "s1"}),
                                    ("vm.snapshot.list", {"vm": U1}))):
        res = run(svc.handle(ADMIN, op, args, f"g{i}"))
        assert res["ok"] is False and res["error"]["code"] == "migrating", (op, res)
    # Finished: the same VM is editable again even though a stale tag would say otherwise.
    svc.migrator.store.put({"id": "ab" * 16, "role": "source", "state": "aborted", "vm": U1})
    assert run(svc.handle(ADMIN, "vm.update", {"vm": U1, "vcpus": 3}, "after"))["ok"]
