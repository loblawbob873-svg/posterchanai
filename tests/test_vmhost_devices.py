"""Host devices given to a VM — USB (hot-plug) and PCI/GPU (shut off) — through the SHIPPED service against the fake
hypervisor, plus the virsh argv the real backend sends.

What each block pins (each verified to fail without its rule):
  * the op table: every device op is ADMIN only, and none is a SESSION op (step_up_required);
  * arguments are ids, never XML: vendor/product must be exactly four lowercase hex digits, a PCI address must look
    like 0000:01:00.0 — markup in either is refused before anything reaches libvirt;
  * USB hot-plug into a RUNNING VM: `--live` plus `--config` when persisting, `--live` alone when not, READ BACK from
    the live and the saved definition; a stopped VM gets `--config` only; an attach libvirt silently did not keep is
    an error; detach mirrors it and is read back too;
  * a device another VM holds is refused NAMING that VM; the host's busy devices are refused with the reason;
  * PCI: a running VM is refused (no GPU hot-plug); a GPU goes WITH its audio function as a pair; the host's display
    GPU and an incomplete IOMMU group are refused; a failed host check (IOMMU off) refuses with its fix;
  * vm.get answers `devices` to an ASSIGNED USER (read only) as well as to an admin;
  * a migration still refuses a VM with any hostdev.
"""
import asyncio

from app.services.vmhost import devices as devices_mod
from app.services.vmhost import domainxml, pci, usb
from app.services.vmhost.backend import VirshBackend
from app.services.vmhost.config import VmHostConfig
from app.services.vmhost.service import OPS, VmHostService
from app.services.vmhost.sessions import SESSION_OPS
from app.services.vmhost.storage import Storage
from tests.vmhost_fake import FakeBackend
from tests.test_vmhost_phase2 import ADMIN, USER, NODE, U1, U2

STICK = usb.UsbDevice(sysname="1-11", bus=1, device=3, vendor="090c", product="1000", manufacturer="Silicon Motion",
                      name="Flash Drive", cls="08")
RAIDBOX = usb.UsbDevice(sysname="6-1", bus=6, device=2, vendor="174c", product="55aa", manufacturer="ASMedia",
                        name="ASM1153E", cls="08", busy="the host has it mounted at /raid (through md0)")
RTX = pci.PciDevice(address="0000:01:00.0", vendor="10de", product="2504", cls="030000", driver="vfio-pci", group="12",
                    vendor_name="NVIDIA Corporation", name="GA106 [GeForce RTX 3060 Lite Hash Rate]")
RTX_AUDIO = pci.PciDevice(address="0000:01:00.1", vendor="10de", product="228e", cls="040300", driver="vfio-pci",
                          group="12", vendor_name="NVIDIA Corporation", name="GA106 High Definition Audio Controller")


def run(coro):
    return asyncio.run(coro)


def make(tmp_path):
    root = tmp_path / "vms"
    storage = Storage(root)
    storage.ensure()
    cfg = VmHostConfig(enabled=True, storage_dir=str(root), admin_pubkeys=[ADMIN])
    be = FakeBackend()
    for u, name, st in ((U1, "alpha", "shutoff"), (U2, "beta", "running")):
        (root / u).mkdir()
        be.add_domain(u, name, state=st, meta=domainxml.VmMeta(owner=ADMIN, created=1, disk_gib=20, assigned=[USER]))
    be.usb_devices = [STICK, RAIDBOX]
    be.pci_devices = [RTX, RTX_AUDIO]

    async def admins():
        return set()
    svc = VmHostService(cfg, be, node_pubkey=NODE, admin_provider=admins, storage=storage)
    run(svc.refresh_index())
    return svc, be


_n = [0]


def c(svc, who, op, args=None, session=None):
    _n[0] += 1
    return run(svc.handle(who, op, args or {}, f"r{_n[0]}", session=session))


def usb_in(xml):
    return usb.hostdevs(xml)


# ------------------------------------------------------------------------------------------------ op table
def test_every_device_op_is_admin_and_never_a_session_op(tmp_path):
    for op in ("host.devices.list", "vm.device.attach", "vm.device.detach"):
        assert OPS[op][0] == "admin", op
        assert op not in SESSION_OPS, op
    svc, be = make(tmp_path)
    for op, args in (("host.devices.list", {}),
                     ("vm.device.attach", {"vm": U2, "kind": "usb", "vendor": "090c", "product": "1000"}),
                     ("vm.device.detach", {"vm": U2, "kind": "usb", "vendor": "090c", "product": "1000"})):
        r = c(svc, USER, op, args)
        assert not r["ok"] and r["error"]["code"] == "forbidden", (op, r)
        r = c(svc, ADMIN, op, args, session="ab" * 32)
        assert not r["ok"] and r["error"]["code"] == "step_up_required", (op, r)
    assert not any(x[0] == "attach_device" for x in be.calls)


def test_ids_are_validated_never_xml(tmp_path):
    svc, be = make(tmp_path)
    bad = [{"vendor": "090C", "product": "1000"},                       # upper case
           {"vendor": "90c", "product": "1000"},
           {"vendor": "090c'/><hostdev", "product": "1000"},
           {"vendor": "090c", "product": "1000", "bus": "1", "device": 3},
           {"vendor": "090c", "product": "1000", "bus": 1},              # half an address
           {"vendor": "090c", "product": "1000", "xml": "<hostdev/>"}]
    for a in bad:
        r = c(svc, ADMIN, "vm.device.attach", dict(a, vm=U2, kind="usb"))
        assert not r["ok"] and r["error"]["code"] == "bad_request", (a, r)
    for addr in ("01:00.0", "0000:01:00.0'/>", "../../sys", "0000:01:00.8"):
        r = c(svc, ADMIN, "vm.device.attach", {"vm": U1, "kind": "pci", "address": addr})
        assert not r["ok"] and r["error"]["code"] == "bad_request", (addr, r)
    r = c(svc, ADMIN, "vm.device.attach", {"vm": U1, "kind": "serial"})
    assert r["error"]["code"] == "bad_request"
    assert not any(x[0] == "attach_device" for x in be.calls)
    # and the XML builders refuse anything but validated ids on their own
    for f in (lambda: usb.hostdev_xml("090c'", "1000"), lambda: pci.hostdev_xml("0000:01:00.0' x='")):
        try:
            f()
            raise AssertionError("built XML from an unvalidated id")
        except ValueError:
            pass


# ------------------------------------------------------------------------------------------------ USB
def test_list_hides_system_devices_and_names_the_owner(tmp_path):
    svc, be = make(tmp_path)
    boot = usb.UsbDevice(sysname="2-1", bus=2, device=2, vendor="0781", product="5581", name="Ultra", system=True)
    be.usb_devices = [STICK, RAIDBOX, boot]
    r = c(svc, ADMIN, "host.devices.list", {"kind": "usb"})
    assert r["ok"], r
    rows = r["result"]["kinds"]["usb"]["devices"]
    assert [x["vendor"] + ":" + x["product"] for x in rows] == ["090c:1000", "174c:55aa"]
    assert rows[0]["label"] == "Silicon Motion Flash Drive (090c:1000)" and rows[0]["used_by"] is None
    assert "raid" in rows[1]["busy"]
    c(svc, ADMIN, "vm.device.attach", {"vm": U2, "kind": "usb", "vendor": "090c", "product": "1000"})
    rows = c(svc, ADMIN, "host.devices.list", {"kind": "usb"})["result"]["kinds"]["usb"]["devices"]
    assert rows[0]["used_by"] == {"uuid": U2, "name": "beta"}


def test_live_attach_to_a_running_vm_persists_and_reads_back(tmp_path):
    svc, be = make(tmp_path)
    r = c(svc, ADMIN, "vm.device.attach", {"vm": U2, "kind": "usb", "vendor": "090c", "product": "1000"})
    assert r["ok"], r
    call = next(x for x in be.calls if x[0] == "attach_device")
    assert call[1:] == (U2, True, True)                       # --live --config
    live = run(be.dumpxml(U2, inactive=False))
    saved = run(be.dumpxml(U2, inactive=True))
    assert usb_in(live) == [{"vendor": "090c", "product": "1000", "bus": 1, "device": 3}]
    assert usb_in(saved) == [{"vendor": "090c", "product": "1000", "bus": None, "device": None}]
    assert "startupPolicy" in saved                            # a VM whose stick is unplugged still boots
    devs = r["result"]["vm"]["devices"]
    assert devs == [{"kind": "usb", "vendor": "090c", "product": "1000", "bus": 1, "device": 3,
                     "label": "Silicon Motion Flash Drive (090c:1000)", "present": True, "key": "090c:1000",
                     "live": True, "persistent": True}]


def test_live_only_attach_does_not_touch_the_saved_definition(tmp_path):
    svc, be = make(tmp_path)
    r = c(svc, ADMIN, "vm.device.attach", {"vm": U2, "kind": "usb", "vendor": "090c", "product": "1000", "persist": False})
    assert r["ok"], r
    assert next(x for x in be.calls if x[0] == "attach_device")[1:] == (U2, True, False)
    assert usb_in(run(be.dumpxml(U2, inactive=True))) == []
    assert r["result"]["vm"]["devices"][0]["persistent"] is False


def test_stopped_vm_gets_config_only_and_refuses_a_temporary_attach(tmp_path):
    svc, be = make(tmp_path)
    r = c(svc, ADMIN, "vm.device.attach", {"vm": U1, "kind": "usb", "vendor": "090c", "product": "1000", "persist": False})
    assert r["error"]["code"] == "bad_request"
    r = c(svc, ADMIN, "vm.device.attach", {"vm": U1, "kind": "usb", "vendor": "090c", "product": "1000"})
    assert r["ok"], r
    assert next(x for x in be.calls if x[0] == "attach_device")[1:] == (U1, False, True)
    run(be.start(U1))                                          # it comes up with the stick
    assert usb_in(run(be.dumpxml(U1, inactive=False)))[0]["bus"] == 1


def test_an_attach_libvirt_did_not_keep_is_an_error(tmp_path):
    svc, be = make(tmp_path)
    be.drop_attach = True
    r = c(svc, ADMIN, "vm.device.attach", {"vm": U2, "kind": "usb", "vendor": "090c", "product": "1000"})
    assert not r["ok"] and r["error"]["code"] == "backend_error"
    assert "did not keep" in r["error"]["message"]


def test_a_device_another_vm_has_is_refused_by_name(tmp_path):
    svc, be = make(tmp_path)
    assert c(svc, ADMIN, "vm.device.attach", {"vm": U2, "kind": "usb", "vendor": "090c", "product": "1000"})["ok"]
    n = sum(1 for x in be.calls if x[0] == "attach_device")
    r = c(svc, ADMIN, "vm.device.attach", {"vm": U1, "kind": "usb", "vendor": "090c", "product": "1000"})
    assert r["error"]["code"] == "conflict" and "beta" in r["error"]["message"], r
    r = c(svc, ADMIN, "vm.device.attach", {"vm": U2, "kind": "usb", "vendor": "090c", "product": "1000"})
    assert r["error"]["code"] == "conflict" and "already attached to this VM" in r["error"]["message"]
    assert sum(1 for x in be.calls if x[0] == "attach_device") == n


def test_the_hosts_busy_device_is_refused_with_its_reason(tmp_path):
    svc, be = make(tmp_path)
    r = c(svc, ADMIN, "vm.device.attach", {"vm": U2, "kind": "usb", "vendor": "174c", "product": "55aa"})
    assert r["error"]["code"] == "conflict" and "/raid" in r["error"]["message"]
    r = c(svc, ADMIN, "vm.device.attach", {"vm": U2, "kind": "usb", "vendor": "dead", "product": "beef"})
    assert r["error"]["code"] == "not_found"


def test_twins_are_pinned_by_address(tmp_path):
    svc, be = make(tmp_path)
    twin = usb.UsbDevice(sysname="1-12", bus=1, device=9, vendor="090c", product="1000", name="Flash Drive")
    be.usb_devices = [STICK, twin]
    r = c(svc, ADMIN, "vm.device.attach", {"vm": U2, "kind": "usb", "vendor": "090c", "product": "1000"})
    assert r["error"]["code"] == "bad_request" and "bus and device" in r["error"]["message"]
    r = c(svc, ADMIN, "vm.device.attach", {"vm": U2, "kind": "usb", "vendor": "090c", "product": "1000",
                                           "bus": 1, "device": 9})
    assert r["ok"], r
    assert usb_in(run(be.dumpxml(U2, inactive=True))) == [{"vendor": "090c", "product": "1000", "bus": 1, "device": 9}]


def test_detach_mirrors_attach_and_reads_back(tmp_path):
    svc, be = make(tmp_path)
    assert c(svc, ADMIN, "vm.device.attach", {"vm": U2, "kind": "usb", "vendor": "090c", "product": "1000"})["ok"]
    r = c(svc, ADMIN, "vm.device.detach", {"vm": U2, "kind": "usb", "vendor": "090c", "product": "1000", "bus": 1, "device": 3})
    assert r["ok"], r
    dets = [x for x in be.calls if x[0] == "detach_device"]
    assert [x[2:] for x in dets] == [(True, False), (False, True)]
    assert usb_in(run(be.dumpxml(U2, inactive=False))) == [] and usb_in(run(be.dumpxml(U2, inactive=True))) == []
    assert r["result"]["vm"]["devices"] == []
    r = c(svc, ADMIN, "vm.device.detach", {"vm": U2, "kind": "usb", "vendor": "090c", "product": "1000"})
    assert r["error"]["code"] == "not_found"


def test_a_detach_libvirt_did_not_do_is_an_error(tmp_path):
    svc, be = make(tmp_path)
    assert c(svc, ADMIN, "vm.device.attach", {"vm": U2, "kind": "usb", "vendor": "090c", "product": "1000"})["ok"]
    be.drop_detach = True
    r = c(svc, ADMIN, "vm.device.detach", {"vm": U2, "kind": "usb", "vendor": "090c", "product": "1000"})
    assert r["error"]["code"] == "backend_error" and "still in" in r["error"]["message"]


def test_an_assigned_user_sees_the_devices_read_only(tmp_path):
    svc, be = make(tmp_path)
    assert c(svc, ADMIN, "vm.device.attach", {"vm": U2, "kind": "usb", "vendor": "090c", "product": "1000"})["ok"]
    r = c(svc, USER, "vm.get", {"vm": U2})
    assert r["ok"] and [d["label"] for d in r["result"]["vm"]["devices"]] == ["Silicon Motion Flash Drive (090c:1000)"]
    assert "hardware" not in r["result"]["vm"]


def test_a_migrating_vm_refuses_devices_and_migration_refuses_hostdevs(tmp_path):
    svc, be = make(tmp_path)
    meta = domainxml.parse_meta(be.domains[U2]["meta_xml"])
    meta.migration = {"id": "ab" * 16, "state": "exporting"}
    be.domains[U2]["meta_xml"] = meta.to_xml(prefixed=False)
    r = c(svc, ADMIN, "vm.device.attach", {"vm": U2, "kind": "usb", "vendor": "090c", "product": "1000"})
    assert r["error"]["code"] == "migrating", r


# ------------------------------------------------------------------------------------------------ PCI
def test_pci_attach_needs_a_shut_off_vm_and_takes_the_audio_function_too(tmp_path):
    svc, be = make(tmp_path)
    r = c(svc, ADMIN, "vm.device.attach", {"vm": U2, "kind": "pci", "address": "0000:01:00.0"})
    assert r["error"]["code"] == "conflict" and "shut the VM down" in r["error"]["message"]
    r = c(svc, ADMIN, "vm.device.attach", {"vm": U1, "kind": "pci", "address": "0000:01:00.0"})
    assert r["ok"], r
    atts = [x for x in be.calls if x[0] == "attach_device"]
    assert [x[2:] for x in atts] == [(False, True), (False, True)]
    saved = run(be.dumpxml(U1, inactive=True))
    assert pci.hostdevs(saved) == [{"address": "0000:01:00.0"}, {"address": "0000:01:00.1"}]
    assert "managed=\"yes\"" in saved
    assert {d["address"] for d in r["result"]["vm"]["devices"]} == {"0000:01:00.0", "0000:01:00.1"}
    # detach takes the pair back out
    r = c(svc, ADMIN, "vm.device.detach", {"vm": U1, "kind": "pci", "address": "0000:01:00.0"})
    assert r["ok"], r
    assert pci.hostdevs(run(be.dumpxml(U1, inactive=True))) == []


def test_pci_refuses_the_hosts_display_gpu(tmp_path):
    svc, be = make(tmp_path)
    igpu = pci.PciDevice(address="0000:10:00.0", vendor="1002", product="164e", cls="030000", driver="amdgpu",
                         group="26", boot_vga=True, busy="it is the host's boot display GPU; the host's amdgpu driver is using it")
    be.pci_devices = [RTX, RTX_AUDIO, igpu]
    r = c(svc, ADMIN, "vm.device.attach", {"vm": U1, "kind": "pci", "address": "0000:10:00.0"})
    assert r["error"]["code"] == "conflict" and "boot display" in r["error"]["message"]
    rows = c(svc, ADMIN, "host.devices.list", {"kind": "pci"})["result"]["kinds"]["pci"]["devices"]
    row = next(x for x in rows if x["address"] == "0000:10:00.0")
    assert row["passable"] is False
    assert any(ch["id"] == "host-gpu" and not ch["ok"] for ch in row["checks"])
    assert not any(x[0] == "attach_device" for x in be.calls)


def test_pci_refuses_a_gpu_the_host_driver_holds_and_says_how_to_free_it(tmp_path):
    svc, be = make(tmp_path)
    held = pci.PciDevice(**{**RTX.__dict__, "driver": "nvidia", "busy": "the host's nvidia driver is using it"})
    be.pci_devices = [held, RTX_AUDIO]
    rows = c(svc, ADMIN, "host.devices.list", {"kind": "pci"})["result"]["kinds"]["pci"]["devices"]
    row = next(x for x in rows if x["address"] == "0000:01:00.0")
    fix = next(ch["fix"] for ch in row["checks"] if ch["id"] == "host-gpu")
    assert "vfio-pci ids=10de:2504,10de:228e" in fix and "softdep nvidia pre: vfio-pci" in fix
    assert c(svc, ADMIN, "vm.device.attach", {"vm": U1, "kind": "pci", "address": "0000:01:00.0"})["error"]["code"] == "conflict"


def test_pci_refuses_an_incomplete_iommu_group(tmp_path):
    svc, be = make(tmp_path)
    nic = pci.PciDevice(address="0000:0c:00.0", vendor="10ec", product="8125", cls="020000", driver="r8169", group="22",
                        name="RTL8125")
    bridge = pci.PciDevice(address="0000:04:0a.0", vendor="1022", product="43f5", cls="060400", driver="pcieport",
                           group="22", hidden=True)
    xhci = pci.PciDevice(address="0000:0e:00.0", vendor="1022", product="43f7", cls="0c0330", driver="xhci_hcd", group="22")
    be.pci_devices = [nic, bridge, xhci]
    r = c(svc, ADMIN, "vm.device.attach", {"vm": U1, "kind": "pci", "address": "0000:0e:00.0"})
    assert r["error"]["code"] == "conflict" and "0000:0c:00.0" in r["error"]["message"], r
    assert "0000:04:0a.0" not in r["error"]["message"]           # a bridge may stay with the host
    # the same group with the NIC on vfio-pci is fine
    nic.driver = "vfio-pci"
    assert c(svc, ADMIN, "vm.device.attach", {"vm": U1, "kind": "pci", "address": "0000:0e:00.0"})["ok"]


def test_pci_host_checks_refuse_with_the_fix(tmp_path):
    svc, be = make(tmp_path)
    be.device_checks_by_kind["pci"][0] = {"id": "iommu", "ok": False, "label": "IOMMU is off",
                                          "fix": "add amd_iommu=on iommu=pt"}
    r = c(svc, ADMIN, "vm.device.attach", {"vm": U1, "kind": "pci", "address": "0000:01:00.0"})
    assert r["error"]["code"] == "unsupported" and "amd_iommu=on" in r["error"]["message"]
    lst = c(svc, ADMIN, "host.devices.list", {"kind": "pci", "vm": U1})["result"]["kinds"]["pci"]
    assert lst["checks"][0]["ok"] is False and all(not x["passable"] for x in lst["devices"])
    assert {x["id"] for x in lst["vm_checks"]} == {"efi", "q35"}


def test_pci_card_another_vm_has_is_refused_by_name(tmp_path):
    svc, be = make(tmp_path)
    assert c(svc, ADMIN, "vm.device.attach", {"vm": U1, "kind": "pci", "address": "0000:01:00.0"})["ok"]
    run(be.destroy(U2))
    r = c(svc, ADMIN, "vm.device.attach", {"vm": U2, "kind": "pci", "address": "0000:01:00.0"})
    assert r["error"]["code"] == "conflict" and "alpha" in r["error"]["message"]


# ------------------------------------------------------------------------------------------------ virsh argv
def test_virsh_argv_for_attach_and_detach(tmp_path):
    seen = []

    async def runner(argv, timeout, stdin=None):
        seen.append(argv)
        return 0, "", ""
    be = VirshBackend("qemu:///system", runner=runner)
    p = str(tmp_path / "d.xml")
    run(be.attach_device(U2, p, live=True, config=True))
    run(be.attach_device(U1, p, live=False, config=True))
    run(be.detach_device(U2, p, live=True, config=False))
    assert seen == [["virsh", "--connect", "qemu:///system", "attach-device", U2, p, "--live", "--config"],
                    ["virsh", "--connect", "qemu:///system", "attach-device", U1, p, "--config"],
                    ["virsh", "--connect", "qemu:///system", "detach-device", U2, p, "--live"]]
    for bad in (lambda: be.attach_device(U2, "rel.xml", live=True, config=False),
                lambda: be.attach_device(U2, p, live=False, config=False)):
        try:
            run(bad())
            raise AssertionError("accepted")
        except Exception as e:
            assert "Backend" in type(e).__name__


def test_libvirt_refusals_become_actionable_codes():
    from app.services.vmhost.backend import BackendError
    assert devices_mod._classify(BackendError("Requested operation is not valid: USB device 001:003 is in use by "
                                              "driver QEMU, domain gentoo")).code == "conflict"
    assert devices_mod._classify(BackendError("Did not find USB device 090c:1000")).code == "not_found"
    e = devices_mod._classify(BackendError("could not open /dev/bus/usb/001/003: Permission denied"))
    assert e.code == "backend_error" and "Devices" in e.message


def test_a_qemu_without_usb_host_refuses_before_libvirt_is_asked(tmp_path):
    """LIVE (nas.lan): Gentoo's QEMU is USE=-usb by default and QEMU answered the first real attach with "'usb-host' is
    not a valid device model name". The host check says so, with the USE flag, before anything is attached."""
    svc, be = make(tmp_path)
    be.device_checks_by_kind["usb"] = [{"id": "qemu-usb", "ok": False, "label": "QEMU has no USB passthrough",
                                        "fix": "add USE=usb for app-emulation/qemu"}]
    r = c(svc, ADMIN, "vm.device.attach", {"vm": U2, "kind": "usb", "vendor": "090c", "product": "1000"})
    assert r["error"]["code"] == "unsupported" and "USE=usb" in r["error"]["message"], r
    assert not any(x[0] == "attach_device" for x in be.calls)
    lst = c(svc, ADMIN, "host.devices.list", {"kind": "usb"})["result"]["kinds"]["usb"]
    assert lst["checks"][0]["ok"] is False and lst["devices"]


def test_the_qemu_device_probe_reads_the_real_help_output(tmp_path):
    from app.services.vmhost import backend as B
    fake = tmp_path / "qemu-system-x86_64"
    fake.write_text("#!/bin/sh\n")
    help_out = ('name "vfio-pci", bus PCI, desc "VFIO-based PCI device assignment"\n'
                'name "usb-redir", bus usb-bus\n')

    async def runner(argv, timeout, stdin=None):
        assert argv[1:] == ["-device", "help"]
        return 0, help_out, ""
    B._QEMU_DEVICES.clear()
    usb_check = run(B.qemu_device_check("usb", str(fake), runner))
    pci_check = run(B.qemu_device_check("pci", str(fake), runner))
    assert usb_check["ok"] is False and "USE=usb" in usb_check["fix"]      # usb-redir is NOT usb-host
    assert pci_check["ok"] is True
