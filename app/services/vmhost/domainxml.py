"""Libvirt domain XML for a hosted VM, and the `pc:vm` metadata that records who it belongs to.

Ported from desktop/vm.js's `create()` with the changes a SERVER needs:

  * HEADLESS. The desktop draws the guest with SPICE + virgl through a local viewer; a server has no
    display, and a GL-accelerated virtio GPU with no GL context is a guest that boots to black. So:
    VNC bound to 127.0.0.1 only (the console WebSocket is the single way in), and a plain virtio GPU.
  * A REAL NETWORK. `type="user"` is fine for a desktop session VM and useless for a server one
    (nothing can reach it). Hosts use a libvirt network (`default`) or a bridge.
  * AN EXPLICIT NVRAM PATH inside the VM's own directory, so a delete (and later a migration) knows
    exactly which file holds its EFI variables instead of guessing libvirt's per-distro default.
  * WINDOWS INSTALLERS SEE THEIR DISK. virtio-blk and virtio-net need a driver disc Windows setup does
    not carry, so a Windows guest gets SATA + e1000e; Linux keeps virtio.
  * No <emulator>: libvirt picks the host's own (qemu-system-x86_64 vs /usr/libexec/qemu-kvm).

Nothing here takes XML from a client. Every value is either validated upstream (uuid, clean name,
clamped integers) or escaped here, and the tests parse the output back to prove it.
"""
from __future__ import annotations

import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from xml.sax.saxutils import escape, quoteattr

PC_NS = "https://posterchan.place/ns/vm/1"
PC_KEY = "pc"


def _a(v) -> str:
    return quoteattr(str(v))


@dataclass
class VmMeta:
    owner: str = ""
    created: int = 0
    guest: str = "linux"
    firmware: str = "efi"
    disk_gib: int = 0
    iso: str = ""
    assigned: list = field(default_factory=list)
    labels: list = field(default_factory=list)

    def to_xml(self, *, prefixed: bool = True) -> str:
        """The metadata element. `prefixed` writes `pc:vm xmlns:pc=…` (inside a domain definition);
        unprefixed is the form `virsh metadata --key pc --set` takes."""
        p = "pc:" if prefixed else ""
        ns = f" xmlns:pc={_a(PC_NS)}" if prefixed else ""
        attrs = (f' v="1" owner={_a(self.owner)} created={_a(int(self.created or time.time()))}'
                 f" guest={_a(self.guest)} firmware={_a(self.firmware)} disk_gib={_a(int(self.disk_gib))}"
                 f" iso={_a(self.iso)}")
        kids = "".join(f"<{p}assign pk={_a(pk)}/>" for pk in self.assigned)
        kids += "".join(f"<{p}label>{escape(str(lb))}</{p}label>" for lb in self.labels)
        return f"<{p}vm{ns}{attrs}>{kids}</{p}vm>"


def _local(tag: str) -> str:
    return tag.rsplit("}", 1)[-1].split(":", 1)[-1]


def parse_meta(xml_text) -> VmMeta | None:
    """Read a `pc:vm` element — prefixed or not, standalone or embedded in a whole domain XML.
    None when there is none (a VM this app did not create) or it is unreadable."""
    if not xml_text or not str(xml_text).strip():
        return None
    try:
        root = ET.fromstring(str(xml_text).strip())
    except ET.ParseError:
        return None
    node = None
    for el in root.iter():
        if _local(el.tag) == "vm" and (el.tag.startswith("{" + PC_NS + "}") or el is root
                                       or el.get("v") is not None):
            node = el
            break
    if node is None:
        return None
    pks, labels = [], []
    for ch in node:
        name = _local(ch.tag)
        if name == "assign":
            pk = str(ch.get("pk") or "").strip().lower()
            if len(pk) == 64 and all(c in "0123456789abcdef" for c in pk) and pk not in pks:
                pks.append(pk)
        elif name == "label" and (ch.text or "").strip():
            labels.append(ch.text.strip())

    def _i(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0
    return VmMeta(owner=str(node.get("owner") or ""), created=_i(node.get("created")),
                  guest=str(node.get("guest") or "linux"), firmware=str(node.get("firmware") or "efi"),
                  disk_gib=_i(node.get("disk_gib")), iso=str(node.get("iso") or ""),
                  assigned=pks, labels=labels)


@dataclass
class DomainSpec:
    name: str
    uuid: str
    guest: str            # linux | windows
    firmware: str         # efi | bios
    vcpus: int
    ram_mib: int
    disk_path: str
    nvram_path: str
    iso_path: str = ""
    network: str = "default"
    bridge: str = ""
    meta: VmMeta | None = None


def build_domain_xml(spec: DomainSpec) -> str:
    win = spec.guest == "windows"
    efi = spec.firmware == "efi"
    boots = ('<boot dev="cdrom"/><boot dev="hd"/>' if spec.iso_path else '<boot dev="hd"/>')
    if efi:
        os_xml = (f'<os firmware="efi"><type arch="x86_64" machine="q35">hvm</type>'
                  f'<firmware><feature enabled="{"yes" if win else "no"}" name="secure-boot"/></firmware>'
                  f"<nvram>{escape(spec.nvram_path)}</nvram>{boots}</os>")
    else:
        os_xml = f'<os><type arch="x86_64" machine="q35">hvm</type>{boots}</os>'
    features = "<acpi/><apic/>" + ('<smm state="on"/>' if win else "")
    disk_bus, disk_dev = ("sata", "sda") if win else ("virtio", "vda")
    cd_dev = "sdb" if win else "sda"
    cdrom = (f'<disk type="file" device="cdrom"><driver name="qemu" type="raw"/>'
             f"<source file={_a(spec.iso_path)}/><target dev={_a(cd_dev)} bus=\"sata\"/><readonly/></disk>"
             if spec.iso_path else "")
    nic_model = "e1000e" if win else "virtio"
    if spec.bridge:
        nic = f'<interface type="bridge"><source bridge={_a(spec.bridge)}/><model type="{nic_model}"/></interface>'
    else:
        nic = (f'<interface type="network"><source network={_a(spec.network or "default")}/>'
               f'<model type="{nic_model}"/></interface>')
    tpm = '<tpm model="tpm-crb"><backend type="emulator" version="2.0"/></tpm>' if win else ""
    meta = f"<metadata>{spec.meta.to_xml(prefixed=True)}</metadata>" if spec.meta else ""
    return (
        f'<domain type="kvm"><name>{escape(spec.name)}</name><uuid>{escape(spec.uuid)}</uuid>{meta}'
        f'<memory unit="MiB">{int(spec.ram_mib)}</memory><currentMemory unit="MiB">{int(spec.ram_mib)}</currentMemory>'
        f"<vcpu>{int(spec.vcpus)}</vcpu>{os_xml}<features>{features}</features>"
        f'<cpu mode="host-passthrough" check="none"/>'
        f'<clock offset="{"localtime" if win else "utc"}"/>'
        f"<on_poweroff>destroy</on_poweroff><on_reboot>restart</on_reboot><on_crash>destroy</on_crash>"
        f"<devices>"
        f'<disk type="file" device="disk"><driver name="qemu" type="qcow2"/><source file={_a(spec.disk_path)}/>'
        f'<target dev="{disk_dev}" bus="{disk_bus}"/></disk>{cdrom}{nic}'
        # VNC on loopback ONLY. The console route is the one door; a VNC port on a public address is
        # an unauthenticated keyboard for anyone who can reach it. The password is set per console
        # ticket (QMP set_password) and expires with it.
        f'<graphics type="vnc" autoport="yes" listen="127.0.0.1"><listen type="address" address="127.0.0.1"/></graphics>'
        # No accel3d / gl: a headless host has no GL context to give it (see the module comment).
        f'<video><model type="virtio" heads="1" primary="yes"/></video>'
        f'<channel type="unix"><target type="virtio" name="org.qemu.guest_agent.0"/></channel>'
        f'<input type="tablet" bus="usb"/><input type="keyboard" bus="usb"/>'
        f'<memballoon model="virtio"/>{tpm}'
        f"</devices></domain>"
    )
