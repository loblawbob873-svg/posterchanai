"""Offline snapshots: `qemu-img snapshot` on every qcow2 disk + a copy of the EFI variable store, while the VM is off.

Why not libvirt's internal snapshots any more (measured on the live host, scripts/vmhost_live_probe.py step 7):
libvirt 12 with a qcow2 varstore took an internal snapshot of a RUNNING EFI VM, while a raw OVMF varstore is refused
— the same op worked or failed by distro. The offline path behaves the same everywhere, and a revert never touches
the domain definition, so assignments, hardware and a migration tag stay CURRENT.
"""
import asyncio
import json
import os
from pathlib import Path

import pytest

from app.services.vmhost import domainxml
from app.services.vmhost.backend import BackendError, VirshBackend, parse_img_info
from tests.test_vmhost_phase2 import ADMIN, NEWUSER, USER, c, make
from tests.vmhost_fake import FakeBackend

REAL = Path(__file__).parent / "fixtures" / "vmhost_real"


def run(coro):
    return asyncio.run(coro)


def new_vm(svc, be, name="snap", start=False, nvram=b"VARS-at-create"):
    res = c(svc, ADMIN, "vm.create", {"name": name, "vcpus": 1, "ram_mib": 512, "disk_gib": 2}, "create-" + name)
    assert res["ok"], res
    u = res["result"]["vm"]["uuid"]
    disk = svc.storage.disk_path(u)
    disk.write_bytes(b"QFI\xfb" + b"\0" * 2000 + b"original guest data")
    if nvram is not None:
        svc.storage.nvram_path(u).write_bytes(nvram)
    if start:
        be.domains[u]["state"] = "running"
    return u, disk


def snaps(svc, u, rid="list"):
    res = c(svc, ADMIN, "vm.snapshot.list", {"vm": u}, rid)
    assert res["ok"], res
    return res["result"]["snapshots"]


def test_create_list_revert_delete_restores_disk_and_variables(tmp_path):
    svc, be, root = make(tmp_path)
    u, disk = new_vm(svc, be)
    nv = svc.storage.nvram_path(u)
    res = c(svc, ADMIN, "vm.snapshot.create", {"vm": u, "name": "clean", "description": "fresh"}, "s1")
    assert res["ok"], res
    assert [(s["name"], s["state"], s["description"], s["disks"]) for s in res["result"]["snapshots"]] == \
        [("clean", "ok", "fresh", ["vda"])]
    assert svc.storage.snapshot_nvram_path(u, "clean").read_bytes() == b"VARS-at-create"
    assert oct(os.stat(svc.storage.snapshot_nvram_path(u, "clean")).st_mode & 0o777) == "0o600"
    record = domainxml.parse_meta(be.domains[u]["meta_xml"]).snapshots
    assert [r["name"] for r in record] == ["clean"] and record[0]["nvram"] is True
    # the guest changes things
    disk.write_bytes(disk.read_bytes().replace(b"original", b"MODIFIED"))
    nv.write_bytes(b"VARS-changed-by-firmware")
    assert c(svc, ADMIN, "vm.snapshot.revert", {"vm": u, "name": "clean"}, "r0")["error"]["code"] == "bad_request"
    res = c(svc, ADMIN, "vm.snapshot.revert", {"vm": u, "name": "clean", "confirm": True}, "r1")
    assert res["ok"], res
    assert b"original guest data" in disk.read_bytes() and b"MODIFIED" not in disk.read_bytes()
    assert nv.read_bytes() == b"VARS-at-create"
    assert c(svc, ADMIN, "vm.snapshot.revert", {"vm": u, "name": "nope", "confirm": True}, "r2")["error"]["code"] \
        == "not_found"
    res = c(svc, ADMIN, "vm.snapshot.delete", {"vm": u, "name": "clean"}, "d1")
    assert res["ok"] and res["result"]["snapshots"] == []
    assert not svc.storage.snapshot_nvram_path(u, "clean").exists()
    assert be._tags(str(disk)) == [] and domainxml.parse_meta(be.domains[u]["meta_xml"]).snapshots == []


def test_every_change_needs_the_vm_shut_off_but_list_does_not(tmp_path):
    svc, be, root = make(tmp_path)
    u, disk = new_vm(svc, be)
    assert c(svc, ADMIN, "vm.snapshot.create", {"vm": u, "name": "a"}, "s1")["ok"]
    be.domains[u]["state"] = "running"
    for op, args in (("vm.snapshot.create", {"name": "b"}), ("vm.snapshot.revert", {"name": "a", "confirm": True}),
                     ("vm.snapshot.delete", {"name": "a"})):
        res = c(svc, ADMIN, op, {"vm": u, **args}, op.replace(".", "-"))
        assert res["error"]["code"] == "conflict" and "shut the VM down" in res["error"]["message"], (op, res)
    assert [s["name"] for s in snaps(svc, u)] == ["a"]
    assert not any(call[0].startswith("img_snapshot") and call[2] != "a" for call in be.calls)


def test_names_are_validated_and_unique_and_a_leftover_tag_blocks_the_name(tmp_path):
    svc, be, root = make(tmp_path)
    u, disk = new_vm(svc, be)
    assert c(svc, ADMIN, "vm.snapshot.create", {"vm": u, "name": "bad name"}, "1")["error"]["code"] == "bad_request"
    assert c(svc, ADMIN, "vm.snapshot.create", {"vm": u, "name": "-x"}, "2")["error"]["code"] == "bad_request"
    assert c(svc, ADMIN, "vm.snapshot.create", {"vm": u, "name": "a"}, "3")["ok"]
    assert c(svc, ADMIN, "vm.snapshot.create", {"vm": u, "name": "a"}, "4")["error"]["code"] == "conflict"
    # a create that died after qemu-img but before the record: the tag is there and nothing names it
    be._write_tags(str(disk), be._tags(str(disk)) + ["crashed"])
    lst = {s["name"]: s["state"] for s in snaps(svc, u)}
    assert lst == {"a": "ok", "crashed": "orphan"}
    res = c(svc, ADMIN, "vm.snapshot.create", {"vm": u, "name": "crashed"}, "5")
    assert res["error"]["code"] == "conflict" and "leftover" in res["error"]["message"]
    assert c(svc, ADMIN, "vm.snapshot.revert", {"vm": u, "name": "crashed", "confirm": True}, "6")["error"]["code"] \
        == "not_found"
    assert c(svc, ADMIN, "vm.snapshot.delete", {"vm": u, "name": "crashed"}, "7")["ok"]
    assert be._tags(str(disk)) == ["a"]


def test_a_failure_half_way_leaves_nothing_behind(tmp_path):
    svc, be, root = make(tmp_path)
    u, disk = new_vm(svc, be)
    assert c(svc, ADMIN, "vm.update", {"vm": u, "add_disk_gib": 1}, "add")["ok"]
    vdb = svc.storage.extra_disk_path(u, "vdb")
    real = be.img_snapshot_create

    async def second_fails(path, name):
        if path.endswith("disk-vdb.qcow2"):
            raise BackendError("qemu-img: Failed to create snapshot: No space left on device")
        await real(path, name)
    be.img_snapshot_create = second_fails
    res = c(svc, ADMIN, "vm.snapshot.create", {"vm": u, "name": "half"}, "h1")
    assert res["error"]["code"] == "backend_error", res
    assert be._tags(str(disk)) == [] and be._tags(str(vdb)) == []
    assert not svc.storage.snapshot_nvram_path(u, "half").exists()
    assert domainxml.parse_meta(be.domains[u]["meta_xml"]).snapshots == []


def test_a_revert_keeps_the_current_access_and_hardware(tmp_path):
    svc, be, root = make(tmp_path)
    u, disk = new_vm(svc, be)
    assert c(svc, ADMIN, "vm.assign", {"vm": u, "pubkey": USER}, "a1")["ok"]
    assert c(svc, ADMIN, "vm.snapshot.create", {"vm": u, "name": "before"}, "s1")["ok"]
    assert c(svc, ADMIN, "vm.unassign", {"vm": u, "pubkey": USER}, "u1")["ok"]
    assert c(svc, ADMIN, "vm.assign", {"vm": u, "pubkey": NEWUSER}, "a2")["ok"]
    assert c(svc, ADMIN, "vm.update", {"vm": u, "vcpus": 3}, "up")["ok"]
    res = c(svc, ADMIN, "vm.snapshot.revert", {"vm": u, "name": "before", "confirm": True}, "r1")
    assert res["ok"], res
    meta = domainxml.parse_meta(be.domains[u]["meta_xml"])
    assert meta.assigned == [NEWUSER] and be.domains[u]["vcpus"] == 3
    assert [s["name"] for s in meta.snapshots] == ["before"], "the revert kept the snapshot list itself"
    assert c(svc, USER, "vm.power", {"vm": u, "action": "start"}, "p1")["error"]["code"] == "not_found"


def test_a_disk_added_after_the_snapshot_blocks_its_revert_not_its_delete(tmp_path):
    svc, be, root = make(tmp_path)
    u, disk = new_vm(svc, be)
    assert c(svc, ADMIN, "vm.snapshot.create", {"vm": u, "name": "one-disk"}, "s1")["ok"]
    assert c(svc, ADMIN, "vm.update", {"vm": u, "add_disk_gib": 1}, "add")["ok"]
    assert {s["name"]: s["state"] for s in snaps(svc, u)} == {"one-disk": "disks_changed"}
    res = c(svc, ADMIN, "vm.snapshot.revert", {"vm": u, "name": "one-disk", "confirm": True}, "r1")
    assert res["error"]["code"] == "conflict" and "disks changed" in res["error"]["message"]
    assert c(svc, ADMIN, "vm.snapshot.delete", {"vm": u, "name": "one-disk"}, "d1")["ok"]


def test_a_snapshot_taken_before_the_first_start_reverts_to_no_variables(tmp_path):
    svc, be, root = make(tmp_path)
    u, disk = new_vm(svc, be, nvram=None)
    res = c(svc, ADMIN, "vm.snapshot.create", {"vm": u, "name": "never-booted"}, "s1")
    assert res["ok"] and domainxml.parse_meta(be.domains[u]["meta_xml"]).snapshots[0]["nvram"] is False
    svc.storage.nvram_path(u).write_bytes(b"VARS after a boot")
    assert c(svc, ADMIN, "vm.snapshot.revert", {"vm": u, "name": "never-booted", "confirm": True}, "r1")["ok"]
    assert not svc.storage.nvram_path(u).exists(), "no template here, so libvirt seeds it again on the next start"


def test_raw_disks_and_capacity_are_refused_before_anything_is_written(tmp_path):
    svc, be, root = make(tmp_path)
    u, disk = new_vm(svc, be)
    be.stats["disk_free_gib"] = svc.cfg.reserve_disk_gib
    res = c(svc, ADMIN, "vm.snapshot.create", {"vm": u, "name": "x"}, "c1")
    assert res["error"]["code"] == "insufficient_capacity", res
    be.stats["disk_free_gib"] = 400
    be.domains[u]["xml"] = be.domains[u]["xml"].replace('type="qcow2"', 'type="raw"')
    res = c(svc, ADMIN, "vm.snapshot.create", {"vm": u, "name": "x"}, "c2")
    assert res["error"]["code"] == "unsupported" and "qcow2" in res["error"]["message"], res
    assert not any(call[0] == "img_snapshot_create" for call in be.calls)


@pytest.mark.skipif(os.geteuid() == 0, reason="root reads any file")
def test_a_variable_store_libvirt_created_as_qemu_is_named_not_crashed_on(tmp_path):
    svc, be, root = make(tmp_path)
    u, disk = new_vm(svc, be)
    os.chmod(svc.storage.nvram_path(u), 0)
    try:
        res = c(svc, ADMIN, "vm.snapshot.create", {"vm": u, "name": "x"}, "c1")
        assert res["error"]["code"] == "unsupported" and "qemu user" in res["error"]["message"], res
    finally:
        os.chmod(svc.storage.nvram_path(u), 0o600)


def test_a_user_cannot_touch_snapshots(tmp_path):
    svc, be, root = make(tmp_path)
    u, disk = new_vm(svc, be)
    assert c(svc, ADMIN, "vm.assign", {"vm": u, "pubkey": USER}, "a1")["ok"]
    for op in ("vm.snapshot.list", "vm.snapshot.create", "vm.snapshot.revert", "vm.snapshot.delete"):
        assert c(svc, USER, op, {"vm": u, "name": "x", "confirm": True}, op.replace(".", "-"))["error"]["code"] == "forbidden"


def test_the_metadata_record_is_validated_on_read():
    xml = ('<vm v="1" owner="' + "a" * 64 + '"><snapshot name="ok" created="5" disks="vda" nvram="1">d</snapshot>'
           '<snapshot name="../etc" created="1" disks="vda" nvram="0"/>'
           '<snapshot name="nodisk" created="1" disks="" nvram="0"/>'
           '<snapshot name="bad-target" created="1" disks="vda,../x" nvram="0"/>'
           '<snapshot name="ok" created="9" disks="vda" nvram="0">duplicate</snapshot></vm>')
    got = domainxml.parse_meta(xml).snapshots
    assert got == [{"name": "ok", "created": 5, "description": "d", "disks": ["vda"], "nvram": True},
                   {"name": "bad-target", "created": 1, "description": "", "disks": ["vda"], "nvram": False}]


# ------------------------------------------------------------------------------ the real backend
def test_qemu_img_snapshot_argv_ends_options_and_validates_the_name():
    seen = []

    async def runner(argv, timeout, stdin=None):
        seen.append((argv, timeout))
        return 0, "", ""
    be = VirshBackend(runner=runner)
    run(be.img_snapshot_create("/v/u/disk-vda.qcow2", "clean"))
    run(be.img_snapshot_apply("/v/u/disk-vda.qcow2", "clean"))
    run(be.img_snapshot_delete("/v/u/disk-vda.qcow2", "clean"))
    assert [a for a, _ in seen] == [["qemu-img", "snapshot", f, "clean", "--", "/v/u/disk-vda.qcow2"]
                                    for f in ("-c", "-a", "-d")]
    assert all(t >= 60 for _, t in seen)
    for bad in ("--all", "-l", "", "a b", "x" * 49):
        with pytest.raises(BackendError):
            run(be.img_snapshot_create("/v/u/disk-vda.qcow2", bad))

    async def refuses(argv, timeout, stdin=None):
        return 1, "", "qemu-img: Could not delete snapshot 'nope': snapshot not found"
    with pytest.raises(BackendError, match="snapshot not found"):
        run(VirshBackend(runner=refuses).img_snapshot_delete("/v/u/disk-vda.qcow2", "nope"))


def test_real_qemu_img_info_lists_the_tags_including_a_repeated_one():
    """The live host's `qemu-img info --output=json` after the probe took snapshots (and qemu-img's own behaviour:
    `-c s1` twice makes two snapshots with one tag)."""
    info = parse_img_info((REAL / "qemu-img-info-with-snapshot.out").read_text())
    assert info["format"] == "qcow2" and info["snapshots"] == ["clean"]
    doubled = json.loads((REAL / "qemu-img-info-with-snapshot.out").read_text())
    doubled["snapshots"] = doubled["snapshots"] * 2
    assert parse_img_info(json.dumps(doubled))["snapshots"] == ["clean", "clean"]
    assert parse_img_info((REAL / "qemu-img-info-qcow2.out").read_text())["snapshots"] == []


def test_fake_tags_travel_inside_the_image_bytes(tmp_path):
    be = FakeBackend()
    p = tmp_path / "disk-vda.qcow2"
    p.write_bytes(b"QFI\xfb" + os.urandom(3000))
    run(be.img_snapshot_create(str(p), "a"))
    q = tmp_path / "copy.qcow2"
    q.write_bytes(p.read_bytes())
    assert run(be.img_info(str(q)))["snapshots"] == ["a"]


# ------------------------------------------------------------------------------ migration carries them
def _migrate_world(tmp_path, prepare):
    from tests.vmhost_migration_fake import ADMIN as M_ADMIN, T, VM, World, seed_vm, until

    async def go():
        w = World(tmp_path)
        try:
            seed_vm(w.S, state="shutoff", snapshots=False)
            await w.S.svc.refresh_index()
            res = await w.S.svc.handle(M_ADMIN, "vm.snapshot.create", {"vm": VM, "name": "before-upgrade"}, "snap1")
            assert res["ok"], res
            await prepare(w, VM)
            res = await w.call(w.S, "vm.migrate", {"vm": VM, "target": T, "authz": w.authz()})
            assert res["ok"], res
            mig = res["result"]["migration"]["id"]
            await until(lambda: (w.T.rec(mig) or {}).get("state") in ("done", "aborted")
                        and (w.S.rec(mig) or {}).get("state") in ("done", "aborted"), what="settled")
            return w, VM, w.T.rec(mig), M_ADMIN
        except BaseException:
            await w.close()
            raise
    return go()


def test_a_migration_carries_offline_snapshots_and_they_revert_on_the_target(tmp_path):
    async def nothing(w, vm):
        pass

    async def go():
        w, vm, trec, admin = await _migrate_world(tmp_path, nothing)
        try:
            assert trec["state"] == "done", (trec.get("error"), trec.get("history"))
            copy = w.T.root / vm / "snap-before-upgrade.nvram.fd"
            assert copy.read_bytes() == (w.S.root / ".retained").joinpath(
                next(p.name for p in (w.S.root / ".retained").iterdir()), "snap-before-upgrade.nvram.fd").read_bytes()
            res = await w.T.svc.handle(admin, "vm.snapshot.list", {"vm": vm}, "l1")
            assert [(s["name"], s["state"]) for s in res["result"]["snapshots"]] == [("before-upgrade", "ok")], res
            (w.T.root / vm / "nvram.fd").write_bytes(b"changed on the target")
            res = await w.T.svc.handle(admin, "vm.snapshot.revert", {"vm": vm, "name": "before-upgrade",
                                                                     "confirm": True}, "r1")
            assert res["ok"], res
            assert (w.T.root / vm / "nvram.fd").read_bytes() == copy.read_bytes()
        finally:
            await w.close()
    run(go())


def test_a_record_the_transferred_bytes_do_not_back_is_dropped_on_the_target(tmp_path):
    async def ghost(w, vm):
        be = w.S.backend
        meta = domainxml.parse_meta(be.domains[vm]["meta_xml"])
        meta.snapshots.append({"name": "ghost", "created": 1, "description": "", "disks": ["vda"], "nvram": False})
        be.domains[vm]["meta_xml"] = meta.to_xml(prefixed=False)

    async def go():
        w, vm, trec, admin = await _migrate_world(tmp_path, ghost)
        try:
            assert trec["state"] == "done", trec.get("error")
            names = [s["name"] for s in domainxml.parse_meta(w.T.backend.domains[vm]["meta_xml"]).snapshots]
            assert names == ["before-upgrade"], names
        finally:
            await w.close()
    run(go())


def test_a_hostile_variable_store_copy_is_probed_and_refused(tmp_path):
    async def backed(w, vm):
        p = w.S.root / vm / "snap-before-upgrade.nvram.fd"
        p.write_bytes(b"QFI\xfb\x00\x00\x00\x03" + (512).to_bytes(8, "big") + os.urandom(2000))

    async def go():
        w, vm, trec, admin = await _migrate_world(tmp_path, backed)
        try:
            assert trec["state"] == "aborted" and "snap-before-upgrade.nvram.fd" in (trec.get("error") or ""), trec
            assert vm not in w.T.backend.domains
        finally:
            await w.close()
    run(go())
