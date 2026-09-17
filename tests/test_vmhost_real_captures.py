"""Every parser the VM host uses, fed what a REAL host printed.

tests/fixtures/vmhost_real/ was written by `scripts/vmhost_live_probe.py --capture` on nas.lan (libvirt 12.0.0,
QEMU 10.2.3, Gentoo edk2 with qcow2 firmware descriptors). Probe VM uuids are replaced by 00000000-…-00000000000N
and VNC passwords by PCPROBEX; nothing else was edited. `index.json` records the argv, exit status and stderr of
each capture. Re-capture after a libvirt/QEMU upgrade and these tests say which parser the upgrade broke.
"""
import json
from pathlib import Path

import pytest

from app.services.vmhost import backend as B
from app.services.vmhost import domainxml, migrate
from app.services.vmhost.config import VmHostConfig

REAL = Path(__file__).parent / "fixtures" / "vmhost_real"
EFI = "00000000-0000-4000-8000-000000000002"
BIOS = "00000000-0000-4000-8000-000000000003"
PROBE_ROOT = "/var/lib/posterchan/vms-probe"


def cap(name) -> str:
    return (REAL / f"{name}.out").read_text()


def index() -> dict:
    return json.loads((REAL / "index.json").read_text())


def test_every_capture_is_indexed_and_came_from_a_successful_command():
    idx = index()
    assert "libvirt 12" in idx["host"]["virsh_version"] and idx["host"]["qemu_img"].startswith("qemu-img version")
    files = {p.stem for p in REAL.glob("*.out")}
    assert files == set(idx["captures"]), files ^ set(idx["captures"])
    for name, c in idx["captures"].items():
        assert c["rc"] == 0, (name, c)
    for p in REAL.iterdir():
        text = p.read_text()
        assert "passwd='" not in text or "passwd='PCPROBEX'" in text, f"{p.name} carries a real VNC password"


def test_nodeinfo_and_nodememstats():
    assert B.parse_nodeinfo(cap("virsh-nodeinfo")) == {"cores": 12, "ram_total_mib": 98016908 // 1024}
    free = B.parse_nodememstats(cap("virsh-nodememstats"))
    assert 0 < free <= 98016908 // 1024


def test_list_all_uuid():
    assert B.parse_uuid_list(cap("virsh-list-all-uuid")) == ["00000000-0000-4000-8000-000000000001"]


@pytest.mark.parametrize("name,state", [("virsh-dominfo-shutoff", "shutoff"), ("virsh-dominfo-running", "running")])
def test_dominfo(name, state):
    d = B.parse_dominfo(cap(name))
    assert d["uuid"] == EFI and d["state"] == state and d["vcpus"] == 1 and d["ram_mib"] == 512
    assert d["name"].startswith("pcprobe-") and d["autostart"] is False


def test_metadata_as_virsh_returns_it():
    m = domainxml.parse_meta(cap("virsh-metadata"))
    assert m is not None and m.owner == "ad" * 32 and m.firmware == "efi" and m.disk_gib == 2 and m.iso == ""


def test_domblklist_with_and_without_a_cdrom():
    assert B.parse_domblklist(cap("virsh-domblklist-details-inactive")) == [
        {"type": "file", "device": "disk", "target": "vda", "source": f"{PROBE_ROOT}/{EFI}/disk-vda.qcow2"}]
    got = B.parse_domblklist(cap("virsh-domblklist-with-cdrom"))
    assert [(x["device"], x["target"]) for x in got] == [("disk", "vda"), ("disk", "vdb"), ("cdrom", "sda")]
    assert got[2]["source"].startswith(f"{PROBE_ROOT}/isos/pcprobe-") and got[2]["source"].endswith(".iso")


def test_running_dumpxml_yields_the_loopback_display_and_vncdisplay_agrees():
    ep = B.parse_vnc_graphics(cap("virsh-dumpxml-running"))
    assert ep == ("127.0.0.1", 5900) and B.is_loopback(ep[0])
    assert B.parse_vncdisplay(cap("virsh-vncdisplay")) == ep
    assert B.parse_vnc_graphics(cap("virsh-dumpxml-inactive-efi")) is None, "port -1 while shut off"


def test_qmp_replies_success_and_error_both_with_exit_status_zero():
    assert B.parse_qmp_reply(cap("qmp-set-password"))["return"] == {}
    assert index()["captures"]["qmp-error-reply"]["rc"] == 0
    with pytest.raises(B.BackendError, match="SPICE is not in use"):
        B.parse_qmp_reply(cap("qmp-error-reply"))


def test_hardware_reads_back_every_vm_update_field():
    hw = domainxml.read_hardware(cap("virsh-dumpxml-inactive-updated"), f"{PROBE_ROOT}/isos")
    assert hw["boot"] == "cdrom" and hw["input"] == "mouse" and hw["nics"] == 1 and hw["cdrom"]
    assert hw["media"].startswith("pcprobe-") and hw["media"].endswith(".iso")
    assert [d["target"] for d in hw["disks"]] == ["vda", "vdb", "sda"]
    efi = domainxml.read_hardware(cap("virsh-dumpxml-inactive-efi"), f"{PROBE_ROOT}/isos")
    assert efi["boot"] == "disk" and efi["input"] == "tablet" and not efi["cdrom"]


def test_file_disks_and_nvram_seed_on_real_definitions():
    disks = domainxml.file_disks(cap("virsh-dumpxml-inactive-updated"))
    assert [(d["target"], d["format"], d["type"]) for d in disks] == [("vda", "qcow2", "file"), ("vdb", "qcow2", "file")]
    path, tmpl, fmt = domainxml.nvram_seed(cap("virsh-dumpxml-inactive-efi"))
    assert path == f"{PROBE_ROOT}/{EFI}/nvram.fd" and tmpl.endswith("OVMF_VARS_4M.qcow2") and fmt == "qcow2"
    assert domainxml.nvram_seed(cap("virsh-dumpxml-inactive-bios")) == ("", "", "")


@pytest.mark.parametrize("name,want", [
    ("qemu-img-info-qcow2", ("qcow2", "", "", [])),
    ("qemu-img-info-raw", ("raw", "", "", [])),
    ("qemu-img-info-iso", ("raw", "", "", [])),
    ("qemu-img-info-nvram", ("qcow2", "", "", [])),
    ("qemu-img-info-with-snapshot", ("qcow2", "", "", ["clean"])),
])
def test_qemu_img_info_plain_shapes(name, want):
    got = B.parse_img_info(cap(name))
    assert (got["format"], got["backing"], got["data_file"], got["snapshots"]) == want


def test_qemu_img_info_backing_and_data_file_are_detected():
    backing = B.parse_img_info(cap("qemu-img-info-backing"))
    assert backing["format"] == "qcow2" and backing["backing"].endswith("/base.qcow2")
    data = B.parse_img_info(cap("qemu-img-info-datafile"))
    assert data["format"] == "qcow2" and data["data_file"].endswith("/datafile.raw")


def test_snapshot_names_on_a_vm_with_no_libvirt_snapshots():
    assert cap("virsh-snapshot-list-names").strip() == ""


def test_the_migratable_definition_passes_the_source_checks_and_the_target_rebuild():
    xml = cap("virsh-dumpxml-inactive-migratable")
    info = migrate.inspect_domain_xml(xml)
    migrate.Migrator._refusals(info)
    assert info["uuid"] == EFI and info["vcpus"] == 1 and info["ram_mib"] == 768 and not info["tpm"]
    assert [d["source"].rsplit("/", 1)[-1] for d in info["disks"] if d["device"] == "disk"] == \
        ["disk-vda.qcow2", "disk-vdb.qcow2"]
    cfg = VmHostConfig()
    plan = migrate.validate_incoming_domain(xml, vm_uuid=EFI, name=info["name"],
                                            disk_names={"disk-vda.qcow2", "disk-vdb.qcow2"}, cfg=cfg)
    assert plan["firmware"] == "efi" and plan["chipset"] == "q35"
    assert [d["target"] for d in plan["disks"]] == ["vda", "vdb"] and plan["nics"][0]["model"] == "virtio"
    rebuilt = migrate.build_incoming_domain(plan, Path(f"{PROBE_ROOT}/target/{EFI}"), domainxml.VmMeta(owner="ad" * 32),
                                            cfg, {"disk-vda.qcow2": "qcow2", "disk-vdb.qcow2": "qcow2",
                                                  "nvram.fd": "qcow2"}, True)
    root = domainxml.parse_domain(rebuilt)
    assert root.find("os").get("firmware") == "efi" and root.find("os/loader") is None
    assert root.find("os/nvram").get("format") == "qcow2"


def test_what_libvirt_made_of_the_rebuilt_definition():
    """The target's own define, read back: firmware auto-selected from the varstore's format, every path in the
    target's directory, the carried offline-snapshot record intact."""
    xml = cap("virsh-dumpxml-inactive-rebuilt")
    root = domainxml.parse_domain(xml)
    assert root.find("os/nvram").get("format") == "qcow2" and root.find("os/loader").get("format") == "qcow2"
    assert all(d["path"].startswith(f"{PROBE_ROOT}/target/{EFI}/") for d in domainxml.file_disks(xml))
    meta = domainxml.parse_meta(xml)
    assert [(s["name"], s["disks"], s["nvram"]) for s in meta.snapshots] == [("clean", ["vda", "vdb"], True)]
    assert "defined from" in cap("virsh-define-rebuilt")
