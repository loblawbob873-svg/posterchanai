"""The VM host's decisions, driven through the SHIPPED `VmHostService.handle` against a fake hypervisor.

What each block pins, and why it is worth pinning:
  * the ROLE MATRIX — who is an admin, who is a user, and that a stranger gets NO answer at all (an
    answer of any kind confirms this npub runs a host);
  * ASSIGNED-ONLY VISIBILITY — a user sees exactly the VMs assigned to them; somebody else's VM is
    `not_found`, never `forbidden`, and a user's view never lists the other people on a VM;
  * CONFLICTS and CAPACITY — refusals, not silent clamps: asking for 64 GiB and getting 16 is a VM
    that does not do what its owner thinks;
  * PATH CONFINEMENT — `../`, a slash, and a symlink out of the ISO library are all refused, the last
    one only because the path is RESOLVED before it is checked;
  * the METADATA ROUND TRIP — assignment survives serialisation to the libvirt `pc:vm` element;
  * the BUSY LOCK — a second operation on a VM that is mid-operation is told `busy`, not queued
    behind a hung libvirt call for ever.
"""
import asyncio
import os

import pytest

from app.services.vmhost import domainxml
from app.services.vmhost import service as service_mod
from app.services.vmhost.config import VmHostConfig
from app.services.vmhost.service import VmHostService
from app.services.vmhost.storage import PathEscape, Storage, clean_name
from app.services.vmhost.backend import BackendError
from tests.vmhost_fake import FakeBackend

NODE = "0a" * 32
DB_ADMIN = "1b" * 32
LIST_ADMIN = "2c" * 32
ALLOWED = "3d" * 32
ASSIGNEE = "4e" * 32
STRANGER = "5f" * 32
U1 = "11111111-1111-4111-8111-111111111111"
U2 = "22222222-2222-4222-8222-222222222222"


def run(coro):
    return asyncio.run(coro)


def make(tmp_path, **cfgkw):
    root = tmp_path / "vms"
    storage = Storage(root)
    storage.ensure()
    cfg = VmHostConfig(enabled=True, storage_dir=str(root), admin_pubkeys=[LIST_ADMIN],
                       allowed_pubkeys=[ALLOWED], **cfgkw)
    be = FakeBackend()

    async def admins():
        return {DB_ADMIN}
    svc = VmHostService(cfg, be, node_pubkey=NODE, admin_provider=admins, storage=storage)
    return svc, be, root


def seed_two(be, root):
    """Two VMs made by PosterChan: U1 assigned to ASSIGNEE, U2 assigned to nobody."""
    for u, name, pks in ((U1, "alpha", [ASSIGNEE]), (U2, "beta", [])):
        (root / u).mkdir()
        be.add_domain(u, name, meta=domainxml.VmMeta(owner=DB_ADMIN, created=1, disk_gib=10, assigned=pks))


async def call(svc, who, op, args=None, rid="r1"):
    return await svc.handle(who, op, args or {}, rid)


# ------------------------------------------------------------------------------------ roles
def test_the_role_matrix(tmp_path):
    svc, be, root = make(tmp_path)
    seed_two(be, root)

    async def go():
        await svc.refresh_index()
        return {k: await svc.role_of(v) for k, v in dict(node=NODE, db=DB_ADMIN, listed=LIST_ADMIN,
                                                          allowed=ALLOWED, assignee=ASSIGNEE,
                                                          stranger=STRANGER, junk="npub1nope").items()}
    roles = run(go())
    assert roles == {"node": "admin", "db": "admin", "listed": "admin", "allowed": "user",
                     "assignee": "user", "stranger": None, "junk": None}


def test_an_assignment_is_the_grant_and_its_removal_revokes_it(tmp_path):
    svc, be, root = make(tmp_path)
    seed_two(be, root)

    async def go():
        await svc.refresh_index()
        before = await svc.role_of(STRANGER)
        r = await call(svc, DB_ADMIN, "vm.assign", {"vm": U2, "pubkey": STRANGER})
        during = await svc.role_of(STRANGER)
        r2 = await call(svc, DB_ADMIN, "vm.unassign", {"vm": U2, "pubkey": STRANGER}, rid="r2")
        after = await svc.role_of(STRANGER)
        return before, r, during, r2, after
    before, r, during, r2, after = run(go())
    assert before is None and during == "user" and after is None
    assert r["ok"] and STRANGER in r["result"]["vm"]["assigned"]
    assert r2["ok"] and STRANGER not in r2["result"]["vm"]["assigned"]


@pytest.mark.parametrize("op", sorted(service_mod.OPS))
def test_a_stranger_is_answered_with_silence_and_touches_nothing(tmp_path, op):
    svc, be, root = make(tmp_path)
    seed_two(be, root)
    run(svc.refresh_index())
    be.calls.clear()
    res = run(call(svc, STRANGER, op, {"vm": U1, "name": "x", "action": "start"}))
    assert res is None, "a stranger must get NO response event at all"
    assert be.calls == [], f"a stranger's {op} reached the hypervisor: {be.calls}"


@pytest.mark.parametrize("op", ["vm.create", "vm.delete", "vm.assign", "vm.unassign", "iso.list"])
@pytest.mark.parametrize("who", [ALLOWED, ASSIGNEE])
def test_users_cannot_do_admin_things(tmp_path, op, who):
    svc, be, root = make(tmp_path)
    seed_two(be, root)
    run(svc.refresh_index())
    domains_before = dict(be.domains)
    res = run(call(svc, who, op, {"vm": U1, "name": "evil", "pubkey": who, "confirm_name": "alpha"}))
    assert res["ok"] is False and res["error"]["code"] == "forbidden"
    assert be.domains.keys() == domains_before.keys()


# ------------------------------------------------------------------------------------ visibility
def test_a_user_sees_only_their_assigned_vms(tmp_path):
    svc, be, root = make(tmp_path)
    seed_two(be, root)

    async def go():
        await svc.refresh_index()
        return (await call(svc, ASSIGNEE, "vm.list"), await call(svc, ALLOWED, "vm.list"),
                await call(svc, DB_ADMIN, "vm.list"),
                await call(svc, ASSIGNEE, "vm.get", {"vm": U2}), await call(svc, ASSIGNEE, "vm.get", {"vm": U1}))
    mine, allowed, admin, other, own = run(go())
    assert [v["uuid"] for v in mine["result"]["vms"]] == [U1]
    assert allowed["result"]["vms"] == [], "allowlisted but unassigned sees nothing"
    assert sorted(v["uuid"] for v in admin["result"]["vms"]) == [U1, U2]
    assert other["ok"] is False and other["error"]["code"] == "not_found", \
        "somebody else's VM must be not_found — forbidden would confirm it exists"
    assert own["ok"] and own["result"]["vm"]["name"] == "alpha"


def test_a_users_view_never_lists_the_other_people_on_a_vm(tmp_path):
    svc, be, root = make(tmp_path)
    (root / U1).mkdir()
    be.add_domain(U1, "shared", meta=domainxml.VmMeta(owner=DB_ADMIN, assigned=[ASSIGNEE, ALLOWED]))
    run(svc.refresh_index())
    v = run(call(svc, ASSIGNEE, "vm.get", {"vm": U1}))["result"]["vm"]
    assert v["assigned"] == [ASSIGNEE]
    assert "owner" not in v
    a = run(call(svc, DB_ADMIN, "vm.get", {"vm": U1}))["result"]["vm"]
    assert sorted(a["assigned"]) == sorted([ASSIGNEE, ALLOWED])


def test_host_info_gives_capacity_to_admins_only(tmp_path):
    svc, be, root = make(tmp_path)
    seed_two(be, root)
    run(svc.refresh_index())
    user = run(call(svc, ASSIGNEE, "host.info"))["result"]
    admin = run(call(svc, DB_ADMIN, "host.info"))["result"]
    assert "ram" not in user and "cpu" not in user and user["vms"]["total"] == 1
    assert admin["ram"]["total_mib"] == 32768 and admin["ram"]["committed_mib"] == 4096
    assert admin["vms"]["total"] == 2
    who = run(call(svc, ASSIGNEE, "host.whoami"))["result"]
    assert who["role"] == "user" and who["host"]["pubkey"] == NODE


# ------------------------------------------------------------------------------------ power
def test_power_on_an_assigned_vm_and_its_conflicts(tmp_path):
    svc, be, root = make(tmp_path)
    seed_two(be, root)

    async def go():
        await svc.refresh_index()
        out = {}
        out["start"] = await call(svc, ASSIGNEE, "vm.power", {"vm": U1, "action": "start"}, "a")
        out["again"] = await call(svc, ASSIGNEE, "vm.power", {"vm": U1, "action": "start"}, "b")
        out["reboot"] = await call(svc, ASSIGNEE, "vm.power", {"vm": U1, "action": "reboot"}, "c")
        out["off"] = await call(svc, ASSIGNEE, "vm.power", {"vm": U1, "action": "destroy"}, "d")
        out["off2"] = await call(svc, ASSIGNEE, "vm.power", {"vm": U1, "action": "shutdown"}, "e")
        out["bad"] = await call(svc, ASSIGNEE, "vm.power", {"vm": U1, "action": "format-c"}, "f")
        out["notmine"] = await call(svc, ASSIGNEE, "vm.power", {"vm": U2, "action": "start"}, "g")
        return out
    o = run(go())
    assert o["start"]["ok"] and o["start"]["result"]["vm"]["state"] == "running"
    assert o["again"]["error"]["code"] == "conflict"
    assert o["reboot"]["ok"]
    assert o["off"]["ok"] and o["off"]["result"]["vm"]["state"] == "shutoff"
    assert o["off2"]["error"]["code"] == "conflict"
    assert o["bad"]["error"]["code"] == "bad_request"
    assert o["notmine"]["error"]["code"] == "not_found"
    assert be.domains[U2]["state"] == "shutoff"


def test_a_busy_vm_says_busy_instead_of_queueing(tmp_path, monkeypatch):
    monkeypatch.setattr(service_mod, "LOCK_WAIT", 0.2)
    svc, be, root = make(tmp_path)
    seed_two(be, root)

    async def go():
        await svc.refresh_index()
        gate = asyncio.Event()
        be.gate["start"] = gate
        first = asyncio.create_task(call(svc, ASSIGNEE, "vm.power", {"vm": U1, "action": "start"}, "one"))
        await asyncio.sleep(0.05)
        second = await call(svc, DB_ADMIN, "vm.power", {"vm": U1, "action": "destroy"}, "two")
        gate.set()
        return second, await first
    second, first = run(go())
    assert second["ok"] is False and second["error"]["code"] == "busy"
    assert first["ok"] is True


# ------------------------------------------------------------------------------------ create
def test_create_builds_a_confined_vm_with_metadata(tmp_path):
    svc, be, root = make(tmp_path)
    (root / "isos" / "debian.iso").write_bytes(b"ISO")
    res = run(call(svc, LIST_ADMIN, "vm.create", {"name": "web 1", "guest": "linux", "firmware": "efi",
                                                  "vcpus": 2, "ram_mib": 2048, "disk_gib": 20,
                                                  "iso": "debian.iso", "start": True, "autostart": True}))
    assert res["ok"], res
    vm = res["result"]["vm"]
    assert vm["name"] == "web-1" and vm["state"] == "running" and vm["managed"] and vm["autostart"]
    d = be.domains[vm["uuid"]]
    assert (root / vm["uuid"] / "disk-vda.qcow2").is_file()
    assert (root / vm["uuid"] / "domain.xml").is_file()
    meta = domainxml.parse_meta(d["meta_xml"])
    assert meta.owner == LIST_ADMIN and meta.disk_gib == 20 and meta.iso == "debian.iso"
    assert str(root / "isos" / "debian.iso") in d["xml"]
    assert 'listen="127.0.0.1"' in d["xml"]


def test_create_refuses_bad_input_and_duplicates(tmp_path):
    svc, be, root = make(tmp_path)
    seed_two(be, root)
    run(svc.refresh_index())
    base = {"vcpus": 1, "ram_mib": 512, "disk_gib": 5}
    assert run(call(svc, DB_ADMIN, "vm.create", {**base, "name": "alpha"}))["error"]["code"] == "conflict"
    assert run(call(svc, DB_ADMIN, "vm.create", {**base, "name": "///"}, "2"))["error"]["code"] == "bad_request"
    assert run(call(svc, DB_ADMIN, "vm.create", {**base, "name": "x", "guest": "bsd"}, "3"))["error"]["code"] == "bad_request"
    assert run(call(svc, DB_ADMIN, "vm.create", {**base, "name": "x", "vcpus": "lots"}, "4"))["error"]["code"] == "bad_request"
    assert len(be.domains) == 2
    assert sorted(p.name for p in root.iterdir()) == sorted([U1, U2, "isos", ".state"])


@pytest.mark.parametrize("args,why", [
    ({"vcpus": 17}, "over the per-VM vCPU limit"),
    ({"vcpus": 12}, "more vCPUs than host cores"),
    ({"ram_mib": 70000}, "over the per-VM RAM limit"),
    ({"ram_mib": 29000}, "more RAM than is uncommitted after the reserve"),
    ({"disk_gib": 390}, "more disk than is free after the reserve"),
])
def test_capacity_is_refused_not_clamped(tmp_path, args, why):
    svc, be, root = make(tmp_path)
    seed_two(be, root)                     # 4096 MiB already committed
    run(svc.refresh_index())
    req = {"name": "big", "vcpus": 2, "ram_mib": 1024, "disk_gib": 10, **args}
    res = run(call(svc, DB_ADMIN, "vm.create", req))
    assert res["ok"] is False and res["error"]["code"] == "insufficient_capacity", (why, res)
    assert "big" not in {d["name"] for d in be.domains.values()}


def test_overcommit_lets_memory_and_cpu_through(tmp_path):
    svc, be, root = make(tmp_path, allow_overcommit=True)
    seed_two(be, root)
    run(svc.refresh_index())
    res = run(call(svc, DB_ADMIN, "vm.create", {"name": "big", "vcpus": 12, "ram_mib": 29000, "disk_gib": 10}))
    assert res["ok"], res


def test_a_failed_define_leaves_nothing_behind(tmp_path):
    svc, be, root = make(tmp_path)
    be.fail["define"] = BackendError("XML error: boom")
    res = run(call(svc, DB_ADMIN, "vm.create", {"name": "oops", "vcpus": 1, "ram_mib": 512, "disk_gib": 5}))
    assert res["ok"] is False and res["error"]["code"] == "backend_error"
    assert be.domains == {}
    assert sorted(p.name for p in root.iterdir()) == sorted(["isos", ".state"])


def test_a_retried_create_makes_one_vm(tmp_path):
    svc, be, root = make(tmp_path)
    req = {"name": "once", "vcpus": 1, "ram_mib": 512, "disk_gib": 5}

    async def go():
        a, b = await asyncio.gather(call(svc, DB_ADMIN, "vm.create", req, "same-id"),
                                    call(svc, DB_ADMIN, "vm.create", req, "same-id"))
        c = await call(svc, DB_ADMIN, "vm.create", req, "same-id")
        d = await call(svc, DB_ADMIN, "vm.delete", {"vm": a["result"]["vm"]["uuid"], "confirm_name": "once"},
                       "same-id")
        return a, b, c, d
    a, b, c, d = run(go())
    assert a == b == c and a["ok"]
    assert len(be.domains) == 1
    assert d["ok"] is False and d["error"]["code"] == "bad_request", "an id reused for another op"


# ------------------------------------------------------------------------------------ confinement
@pytest.mark.parametrize("iso", ["../etc/passwd.iso", "isos/../../x.iso", "a/b.iso", "/etc/x.iso",
                                 "..\\x.iso", ".hidden.iso", "x.img"])
def test_iso_ids_cannot_name_a_path(tmp_path, iso):
    svc, be, root = make(tmp_path)
    res = run(call(svc, DB_ADMIN, "vm.create", {"name": "x", "vcpus": 1, "ram_mib": 512, "disk_gib": 5,
                                                 "iso": iso}))
    assert res["ok"] is False and res["error"]["code"] == "bad_request", (iso, res)
    assert be.domains == {}


def test_a_symlink_out_of_the_iso_library_is_refused(tmp_path):
    svc, be, root = make(tmp_path)
    secret = tmp_path / "outside.iso"
    secret.write_bytes(b"not yours")
    os.symlink(secret, root / "isos" / "sneaky.iso")
    (root / "isos" / "real.iso").write_bytes(b"ISO")
    with pytest.raises(PathEscape):
        svc.storage.iso_path("sneaky.iso")
    assert [i["id"] for i in svc.storage.list_isos()] == ["real.iso"]
    res = run(call(svc, DB_ADMIN, "vm.create", {"name": "x", "vcpus": 1, "ram_mib": 512, "disk_gib": 5,
                                                 "iso": "sneaky.iso"}))
    assert res["ok"] is False and res["error"]["code"] == "bad_request"
    listed = run(call(svc, DB_ADMIN, "iso.list"))["result"]["isos"]
    assert [i["id"] for i in listed] == ["real.iso"]


def test_a_missing_iso_is_not_found(tmp_path):
    svc, be, root = make(tmp_path)
    res = run(call(svc, DB_ADMIN, "vm.create", {"name": "x", "vcpus": 1, "ram_mib": 512, "disk_gib": 5,
                                                 "iso": "nope.iso"}))
    assert res["error"]["code"] == "not_found"


def test_vm_ids_are_validated_before_any_path_is_built(tmp_path):
    svc, be, root = make(tmp_path)
    for bad in ("../../etc", "x", U1 + "/..", ""):
        with pytest.raises(PathEscape):
            svc.storage.vm_dir(bad)
        res = run(call(svc, DB_ADMIN, "vm.get", {"vm": bad}))
        assert res["error"]["code"] == "bad_request"


def test_clean_name_matches_the_desktop_rule():
    assert clean_name("  my VM!! ") == "my-VM"
    assert clean_name("..hidden") == "hidden"
    assert clean_name("a" * 80) == "a" * 48
    assert clean_name("<script>") == "script"


# ------------------------------------------------------------------------------------ delete
def test_delete_needs_the_name_a_stopped_vm_and_one_we_made(tmp_path):
    svc, be, root = make(tmp_path)
    seed_two(be, root)
    be.add_domain(U1.replace("1", "3"), "foreign")             # defined outside PosterChan: no metadata
    foreign = U1.replace("1", "3")
    run(svc.refresh_index())
    be.domains[U1]["state"] = "running"
    assert run(call(svc, DB_ADMIN, "vm.delete", {"vm": U2, "confirm_name": "wrong"}))["error"]["code"] == "bad_request"
    assert run(call(svc, DB_ADMIN, "vm.delete", {"vm": U1, "confirm_name": "alpha"}, "2"))["error"]["code"] == "conflict"
    assert run(call(svc, DB_ADMIN, "vm.delete", {"vm": foreign, "confirm_name": "foreign"}, "3"))["error"]["code"] == "unsupported"
    kept = run(call(svc, DB_ADMIN, "vm.delete", {"vm": U2, "confirm_name": "beta"}, "4"))
    assert kept["ok"] and U2 not in be.domains and (root / U2).is_dir(), "delete_disks false keeps the files"
    (root / "keepme").mkdir()
    be.domains[U1]["state"] = "shutoff"
    gone = run(call(svc, DB_ADMIN, "vm.delete", {"vm": U1, "confirm_name": "alpha", "delete_disks": True}, "5"))
    assert gone["ok"] and not (root / U1).exists() and (root / "keepme").is_dir()


def test_delete_refuses_a_vm_directory_that_is_a_symlink(tmp_path):
    svc, be, root = make(tmp_path)
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "precious").write_text("x")
    os.symlink(target, root / U1)
    be.add_domain(U1, "linked", meta=domainxml.VmMeta(owner=DB_ADMIN))
    res = run(call(svc, DB_ADMIN, "vm.delete", {"vm": U1, "confirm_name": "linked", "delete_disks": True}))
    assert res["ok"] is False
    assert (target / "precious").exists()


# ------------------------------------------------------------------------------------ metadata
def test_metadata_round_trips_through_the_libvirt_element():
    m = domainxml.VmMeta(owner=DB_ADMIN, created=1700000000, guest="windows", firmware="efi", disk_gib=64,
                         iso="win11.iso", assigned=[ASSIGNEE, ALLOWED], labels=['a "quoted" <label>'])
    for text in (m.to_xml(prefixed=False), m.to_xml(prefixed=True),
                 f'<domain><name>x</name><metadata>{m.to_xml(prefixed=True)}</metadata></domain>'):
        back = domainxml.parse_meta(text)
        assert back == m, text
    assert domainxml.parse_meta("<domain><name>x</name></domain>") is None
    assert domainxml.parse_meta("not xml <") is None
    bogus = domainxml.parse_meta('<vm v="1"><assign pk="nothex"/><assign pk="%s"/></vm>' % ("AB" * 32))
    assert bogus.assigned == ["ab" * 32]


def test_assignment_is_stored_on_the_domain_and_rebuilt_on_restart(tmp_path):
    svc, be, root = make(tmp_path)
    seed_two(be, root)
    run(svc.refresh_index())
    run(call(svc, DB_ADMIN, "vm.assign", {"vm": U2, "pubkey": ALLOWED}))
    # A fresh process: nothing in memory, only what libvirt holds.
    svc2 = VmHostService(svc.cfg, be, node_pubkey=NODE, admin_provider=svc._admin_provider, storage=svc.storage)
    run(svc2.refresh_index())
    assert run(svc2.role_of(ASSIGNEE)) == "user"
    assert [v["uuid"] for v in run(call(svc2, ALLOWED, "vm.list"))["result"]["vms"]] == [U2]


# ------------------------------------------------------------------------------------ virsh backend
def test_virsh_parsers_read_real_output():
    from app.services.vmhost import backend as b
    dominfo = ("Id:             3\nName:           web1\nUUID:           %s\nOS Type:        hvm\n"
               "State:          running\nCPU(s):         4\nCPU time:       12.3s\n"
               "Max memory:     4194304 KiB\nUsed memory:    4194304 KiB\nPersistent:     yes\n"
               "Autostart:      enable\n" % U1.upper())
    d = b.parse_dominfo(dominfo)
    assert d == {"uuid": U1, "name": "web1", "state": "running", "vcpus": 4, "ram_mib": 4096, "autostart": True}
    assert b.normalize_state("shut off") == "shutoff" and b.normalize_state("in shutdown") == "stopping"
    assert b.parse_vncdisplay("127.0.0.1:3\n\n") == ("127.0.0.1", 5903)
    assert b.parse_vncdisplay("\n") is None
    assert b.parse_uuid_list(f"{U1}\n{U2}\n\n") == [U1, U2]
    assert b.parse_nodeinfo("CPU(s):              16\nMemory size:         65536000 KiB\n") == \
        {"cores": 16, "ram_total_mib": 64000}


def test_virsh_argv_is_strict_and_never_a_shell():
    from app.services.vmhost import backend as b
    seen = []

    async def runner(argv, timeout, stdin=None):
        seen.append(argv)
        if argv[3] == "dominfo":
            return 0, "Name: x\nUUID: %s\nState: shut off\nCPU(s): 1\nMax memory: 1048576 KiB\n" % U1, ""
        if argv[3] == "metadata" and "--set" not in argv:
            return 0, '<vm v="1" owner="%s"/>' % DB_ADMIN, ""
        if argv[3] == "qemu-monitor-command":
            return 0, '{"return":{},"id":"libvirt-1"}', ""      # QMP success reply, as virsh prints it
        return 0, "", ""
    v = b.VirshBackend("qemu:///system", runner=runner)

    async def go():
        await v.start(U1)
        await v.set_metadata(U1, domainxml.VmMeta(owner=DB_ADMIN, assigned=[ASSIGNEE]), live=True)
        await v.img_create("/srv/vms/x/disk-vda.qcow2", 20)
        await v.set_vnc_password(U1, "abcd1234", 60)
        return await v.get(U1)
    d = run(go())
    assert all(isinstance(a, list) and all(isinstance(x, str) for x in a) for a in seen)
    assert seen[0] == ["virsh", "--connect", "qemu:///system", "start", U1]
    meta_set = seen[1]
    assert meta_set[:5] == ["virsh", "--connect", "qemu:///system", "metadata", U1]
    assert "--config" in meta_set and "--live" in meta_set and domainxml.PC_NS in meta_set
    assert ["qemu-img", "create", "-f", "qcow2", "--", "/srv/vms/x/disk-vda.qcow2", "20G"] in seen
    assert d.meta.owner == DB_ADMIN and d.state == "shutoff" and d.ram_mib == 1024
    with pytest.raises(BackendError):
        run(v.set_vnc_password(U1, "bad pw; x", 60))
    with pytest.raises(BackendError):
        b.VirshBackend("qemu:///system; touch /tmp/x")
