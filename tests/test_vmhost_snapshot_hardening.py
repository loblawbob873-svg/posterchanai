"""Snapshots: a revert must not roll back WHO may use the VM, EFI machines are refused up front, and a snapshot
is not taken into a full disk.

  * `virsh snapshot-revert` restores the WHOLE definition the snapshot captured, `pc:vm` metadata included — so
    reverting to a snapshot taken before a user was UNASSIGNED silently gave that user the VM back (and dropped
    anyone assigned since, and any migration tag). The service re-applies the CURRENT metadata after a revert.
  * libvirt refuses internal snapshots of a VM whose firmware variables live in a raw pflash file — every EFI VM
    this host creates — with an error that reads like a host fault. Refused as `unsupported`, before trying.
  * an internal snapshot grows the qcow2 (and a running VM's snapshot stores its RAM there): refused when the
    host lacks the free disk for it.
"""
import asyncio

from app.services.vmhost import domainxml
from tests.test_vmhost_phase2 import ADMIN, NEWUSER, USER, c, make

U3 = "33333333-3333-4333-8333-333333333333"


def run(coro):
    return asyncio.run(coro)


def bios_vm(svc, be, root, state="shutoff", assigned=(USER,)):
    (root / U3).mkdir(exist_ok=True)
    be.add_domain(U3, "gamma", state=state, firmware="bios",
                  meta=domainxml.VmMeta(owner=ADMIN, created=1, disk_gib=20, firmware="bios", assigned=list(assigned)))
    run(svc.refresh_index())


def test_a_revert_does_not_give_an_unassigned_user_the_vm_back(tmp_path):
    svc, be, root = make(tmp_path)
    bios_vm(svc, be, root)
    assert c(svc, ADMIN, "vm.snapshot.create", {"vm": U3, "name": "before"}, "s1")["ok"]
    assert c(svc, ADMIN, "vm.unassign", {"vm": U3, "pubkey": USER}, "u1")["ok"]
    assert c(svc, ADMIN, "vm.assign", {"vm": U3, "pubkey": NEWUSER}, "a1")["ok"]
    res = c(svc, ADMIN, "vm.snapshot.revert", {"vm": U3, "name": "before", "confirm": True}, "r1")
    assert res["ok"], res
    meta = domainxml.parse_meta(be.domains[U3]["meta_xml"])
    assert meta.assigned == [NEWUSER], f"the revert rolled the access list back: {meta.assigned}"
    assert U3 not in {v["uuid"] for v in c(svc, USER, "vm.list", {}, "l1")["result"]["vms"]}
    assert c(svc, USER, "vm.power", {"vm": U3, "action": "start"}, "p1")["error"]["code"] == "not_found"


def test_snapshots_of_an_efi_vm_are_refused_as_unsupported(tmp_path):
    svc, be, root = make(tmp_path)                    # alpha (U1) is an EFI VM, like every VM this host creates
    res = c(svc, ADMIN, "vm.snapshot.create", {"vm": "11111111-1111-4111-8111-111111111111", "name": "x"}, "e1")
    assert not res["ok"] and res["error"]["code"] == "unsupported" and "EFI" in res["error"]["message"], res
    assert not any(call[0] == "snapshot_create" for call in be.calls)


def test_a_snapshot_is_not_taken_into_a_full_disk(tmp_path):
    svc, be, root = make(tmp_path)
    bios_vm(svc, be, root, state="running")
    be.stats["disk_free_gib"] = svc.cfg.reserve_disk_gib + 1           # RAM (2 GiB) will not fit
    res = c(svc, ADMIN, "vm.snapshot.create", {"vm": U3, "name": "x"}, "d1")
    assert not res["ok"] and res["error"]["code"] == "insufficient_capacity", res
    assert not any(call[0] == "snapshot_create" for call in be.calls)
    be.stats["disk_free_gib"] = svc.cfg.reserve_disk_gib + 8
    assert c(svc, ADMIN, "vm.snapshot.create", {"vm": U3, "name": "x"}, "d2")["ok"]
