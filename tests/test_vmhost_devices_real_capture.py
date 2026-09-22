"""What `host.devices.list` REALLY answered on nas.lan (libvirt 12, QEMU 10.2.3, 2026-09-22) — captured by
`scripts/vmhost_live_probe.py --steps 1,2,3,11,10 --usb 090c:1000` through the shipped service and real sysfs,
never edited. Each assertion is a decision that must stay right on real hardware:

  * the USB-SATA bridge carrying a RAID member of /raid is BUSY, named through the LVM volume on top of md0;
  * no hub and no root hub is offered;
  * this QEMU (Gentoo default, USE=-usb) has no `usb-host`, and the check says which USE flag to set — the FIRST real
    attach died inside QEMU with "'usb-host' is not a valid device model name" before this check existed;
  * the RTX 3060 the app computes on is refused (nvidia driver) with the exact vfio-pci ids line, and is paired with
    its HDMI audio; the APU's boot display GPU is refused and takes its .1 audio, NOT the board's audio at .6;
  * the NIC carrying the host's network and the SATA controller under /raid are busy; the root NVMe is not listed.
"""
import json
from pathlib import Path

CAP = json.loads((Path(__file__).parent / "fixtures" / "vmhost_real_devices" / "nas-host-devices-list.json").read_text())


def by(kind, key):
    return {x[key]: x for x in CAP["kinds"][kind]["devices"]}


def test_usb_on_the_real_host():
    usb = by("usb", "id")
    raid = next(x for x in usb.values() if x["vendor"] == "174c")
    assert "/raid" in raid["busy"] and "vg-nas" in raid["busy"]
    assert not any(x["vendor"] == "1d6b" or x["class"] == "09" for x in usb.values())
    stick = next(x for x in usb.values() if x["vendor"] == "090c")
    assert stick["busy"] == "" and stick["used_by"] is None and stick["label"] == "Samsung Flash Drive (090c:1000)"
    chk = CAP["kinds"]["usb"]["checks"][0]
    assert chk["id"] == "qemu-usb" and chk["ok"] is False and "USE=usb" in chk["fix"]


def test_pci_on_the_real_host():
    pci = by("pci", "address")
    rtx = pci["0000:01:00.0"]
    assert rtx["passable"] is False and rtx["with"] == ["0000:01:00.1"]
    fix = next(c["fix"] for c in rtx["checks"] if c["id"] == "host-gpu")
    assert "options vfio-pci ids=10de:2504,10de:228e" in fix and "softdep nvidia pre: vfio-pci" in fix
    apu = pci["0000:10:00.0"]
    assert apu["passable"] is False and "boot display" in apu["busy"] and apu["with"] == ["0000:10:00.1"]
    assert "enp12s0" in pci["0000:0c:00.0"]["busy"] and "/raid" in pci["0000:0f:00.0"]["busy"]
    assert "0000:02:00.0" not in pci                         # the NVMe the host boots from
    assert all(c["ok"] for c in CAP["kinds"]["pci"]["checks"])
    assert {c["id"] for c in CAP["kinds"]["pci"]["vm_checks"]} == {"efi", "q35"}
