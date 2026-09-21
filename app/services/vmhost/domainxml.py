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

import re
import secrets
import string
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
    # Phase 3 (cold migration): {"id","state","peer"} while a migration holds this VM — "outgoing" on
    # the source, "incoming" (pending commit) on the target. Empty = not migrating. Start refuses both.
    migration: dict = field(default_factory=dict)
    # Offline snapshots (hardware.py): [{name, created, description, disks: [targets], nvram: bool}], oldest first.
    # The DATA lives in each qcow2 (`qemu-img snapshot`) and in `snap-<name>.nvram.fd`; this is the record of what a
    # snapshot is made of, so it travels with the definition (and a migration) and survives an app restart.
    snapshots: list = field(default_factory=list)

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
        if self.migration and self.migration.get("id"):
            mg = self.migration
            kids += (f"<{p}migration id={_a(mg.get('id', ''))} state={_a(mg.get('state', ''))}"
                     f" peer={_a(mg.get('peer', ''))}/>")
        for sn in self.snapshots:
            kids += (f"<{p}snapshot name={_a(sn.get('name', ''))} created={_a(int(sn.get('created') or 0))}"
                     f" disks={_a(','.join(sn.get('disks') or []))} nvram={_a('1' if sn.get('nvram') else '0')}>"
                     f"{escape(_XML_CTRL.sub('', str(sn.get('description') or '')))}</{p}snapshot>")
        return f"<{p}vm{ns}{attrs}>{kids}</{p}vm>"


_XML_CTRL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")      # not representable in XML 1.0
SNAPSHOT_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,47}$")
MAX_SNAPSHOT_RECORDS = 64


def clean_snapshot_record(sn) -> dict | None:
    """One offline-snapshot record with every field validated, or None. Used on read AND on a migration's incoming
    metadata, so a record can never carry a path, a control character or a disk target that is not a disk target."""
    if not isinstance(sn, dict) or not isinstance(sn.get("name"), str) or not SNAPSHOT_NAME_RE.match(sn["name"]):
        return None
    try:
        created = max(0, int(sn.get("created") or 0))
    except (TypeError, ValueError):
        created = 0
    disks = []
    for t in sn.get("disks") or []:
        t = str(t).strip()
        if _DEV_RE.match(t) and t not in disks:
            disks.append(t)
    if not disks:
        return None
    desc = _XML_CTRL.sub("", str(sn.get("description") or ""))[:200]
    return {"name": sn["name"], "created": created, "description": desc, "disks": disks,
            "nvram": bool(sn.get("nvram"))}


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
    pks, labels, migration, snapshots = [], [], {}, []
    for ch in node:
        name = _local(ch.tag)
        if name == "assign":
            pk = str(ch.get("pk") or "").strip().lower()
            if len(pk) == 64 and all(c in "0123456789abcdef" for c in pk) and pk not in pks:
                pks.append(pk)
        elif name == "label" and (ch.text or "").strip():
            labels.append(ch.text.strip())
        elif name == "migration" and ch.get("id"):
            migration = {"id": str(ch.get("id")), "state": str(ch.get("state") or ""),
                         "peer": str(ch.get("peer") or "")}
        elif name == "snapshot":
            sn = clean_snapshot_record({"name": ch.get("name"), "created": ch.get("created"),
                                        "disks": str(ch.get("disks") or "").split(","),
                                        "nvram": ch.get("nvram") == "1", "description": ch.text or ""})
            if sn and all(x["name"] != sn["name"] for x in snapshots) and len(snapshots) < MAX_SNAPSHOT_RECORDS:
                snapshots.append(sn)

    def _i(v):
        try:
            return int(v)
        except (TypeError, ValueError):
            return 0
    return VmMeta(owner=str(node.get("owner") or ""), created=_i(node.get("created")),
                  guest=str(node.get("guest") or "linux"), firmware=str(node.get("firmware") or "efi"),
                  disk_gib=_i(node.get("disk_gib")), iso=str(node.get("iso") or ""),
                  assigned=pks, labels=labels, migration=migration, snapshots=snapshots)


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
    vnc_passwd: str = ""          # blank = a fresh random one (the normal case; tests may pin it)


# VNC's DES challenge uses at most 8 characters. The define-time password is ALREADY EXPIRED
# (`passwdValidTo` in the past): QEMU then runs the display with password auth and nobody holds a
# valid password until a console ticket rotates it over QMP. Without `passwd` QEMU starts the display
# with NO authentication and refuses `set_password` outright — the console was open to anybody who
# reached the loopback port, while the ticket carried a password that protected nothing.
VNC_PASSWD_EXPIRED = "1970-01-01T00:00:01"


def random_vnc_password() -> str:
    return "".join(secrets.choice(string.ascii_letters + string.digits) for _ in range(8))


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
        # an unauthenticated keyboard for anyone who can reach it. The define-time password is random
        # and already expired; each console ticket sets a fresh one over QMP that expires with it.
        f'<graphics type="vnc" autoport="yes" listen="127.0.0.1" passwd={_a(spec.vnc_passwd or random_vnc_password())}'
        f' passwdValidTo="{VNC_PASSWD_EXPIRED}"><listen type="address" address="127.0.0.1"/></graphics>'
        # No accel3d / gl: a headless host has no GL context to give it (see the module comment).
        f'<video><model type="vga" heads="1" primary="yes"/></video>'
        f'<channel type="unix"><target type="virtio" name="org.qemu.guest_agent.0"/></channel>'
        f'<input type="tablet" bus="usb"/><input type="keyboard" bus="usb"/>'
        f'<memballoon model="virtio"/>{tpm}'
        f"</devices></domain>"
    )


# ------------------------------------------------------------------------------------ phase 2: hardware edits
# `vm.update` edits an EXISTING (shut-off) definition. The hypervisor's own inactive XML is read, changed
# here as a tree, defined back ONCE and then read again to confirm — the desktop's vm.js lesson that
# "virsh said ok" and "the domain changed" are different claims (its eject re-read, vm.js:153-159).
#
# Why a redefine and not `change-media`/`attach-disk`: every edit here requires the VM to be OFF, where a
# redefine is exactly what those commands do with `--config`, and ONE define makes a multi-field Save
# atomic — four separate virsh calls that fail on the third leave a half-saved machine. The one
# change-media rule that matters is kept by construction: a cdrom that already has a source has it
# REPLACED (the `--update` semantics), never a second <source> appended (what `--insert` over a filled
# tray amounts to, and what left desktop VMs unable to start, vm.js:142-145).

ET.register_namespace("pc", PC_NS)

_DEV_RE = re.compile(r"^(vd|sd)[a-z]$")


class EditError(ValueError):
    """The definition cannot take this edit (no <devices>, no free slot, …)."""


def _parse(xml_text: str) -> ET.Element:
    try:
        return ET.fromstring(str(xml_text or "").strip())
    except ET.ParseError as e:
        raise EditError(f"unreadable domain XML: {e}")


def parse_domain(xml_text: str) -> ET.Element:
    return _parse(xml_text)


def _devices(root: ET.Element) -> ET.Element:
    dev = root.find("devices")
    if dev is None:
        raise EditError("the definition has no <devices>")
    return dev


def read_hardware(xml_text: str, iso_dir: str = "") -> dict:
    """What the settings panel shows, read from a definition: boot order, pointer, NICs, disks, media.
    `media` is an ISO id when the cdrom holds a file from the library, "(external)" for anything else."""
    root = _parse(xml_text)
    os_el = root.find("os")
    boots = [b.get("dev") for b in (os_el.findall("boot") if os_el is not None else [])]
    dev = root.find("devices")
    disks, media, nics, inputs = [], "", 0, []
    net = {"type": "", "name": ""}
    lib = iso_dir.rstrip("/") + "/" if iso_dir else ""
    if dev is not None:
        for d in dev.findall("disk"):
            tgt = d.find("target")
            src = d.find("source")
            path = src.get("file", "") if src is not None else ""
            entry = {"device": d.get("device", "disk"), "target": tgt.get("dev", "") if tgt is not None else "",
                     "bus": tgt.get("bus", "") if tgt is not None else ""}
            if entry["device"] == "cdrom":
                if path:
                    base = path.rsplit("/", 1)[-1]
                    entry["media"] = base if (lib and path.startswith(lib) and "/" not in path[len(lib):]) else "(external)"
                    media = media or entry["media"]
                else:
                    entry["media"] = ""
            disks.append(entry)
        ifaces = dev.findall("interface")
        nics = len(ifaces)
        if ifaces:
            net = nic_of(ifaces[0])
        inputs = [(i.get("type"), i.get("bus")) for i in dev.findall("input")]
    return {"boot": "cdrom" if boots[:1] == ["cdrom"] else "disk",
            "input": "tablet" if any(t == "tablet" for t, _ in inputs) else "mouse",
            "nics": nics, "net": net, "disks": disks, "media": media,
            "cdrom": any(d["device"] == "cdrom" for d in disks)}


def set_vcpus_ram(root: ET.Element, vcpus, ram_mib) -> None:
    if vcpus is not None:
        v = root.find("vcpu")
        if v is None:
            v = ET.SubElement(root, "vcpu")
        v.attrib.pop("current", None)
        v.text = str(int(vcpus))
    if ram_mib is not None:
        for tag in ("memory", "currentMemory"):
            m = root.find(tag)
            if m is None:
                m = ET.SubElement(root, tag)
            m.set("unit", "MiB")
            m.text = str(int(ram_mib))


def set_boot(root: ET.Element, first: str) -> None:
    os_el = root.find("os")
    if os_el is None:
        raise EditError("the definition has no <os> block")
    for b in os_el.findall("boot"):
        os_el.remove(b)
    # A per-device <boot order=…> and <os><boot> may not coexist; libvirt refuses that define.
    for d in root.iter():
        if d is os_el:
            continue
        for b in list(d.findall("boot")):
            if b.get("order") is not None:
                d.remove(b)
    for dev_name in (["cdrom", "hd"] if first == "cdrom" else ["hd", "cdrom"]):
        ET.SubElement(os_el, "boot", {"dev": dev_name})


def set_input(root: ET.Element, mode: str) -> None:
    dev = _devices(root)
    for i in list(dev.findall("input")):
        if i.get("type") in ("tablet", "mouse"):
            dev.remove(i)
    if mode == "tablet":
        ET.SubElement(dev, "input", {"type": "tablet", "bus": "usb"})
    else:
        ET.SubElement(dev, "input", {"type": "mouse", "bus": "ps2"})


def used_targets(root: ET.Element) -> set:
    out = set()
    for d in _devices(root).findall("disk"):
        t = d.find("target")
        if t is not None and t.get("dev"):
            out.add(t.get("dev"))
    return out


def next_disk_target(root: ET.Element, windows: bool) -> str:
    prefix = "sd" if windows else "vd"
    used = used_targets(root)
    for c in "bcdefghijklmnopqrstuvwxyz":
        if prefix + c not in used:
            return prefix + c
    raise EditError("no free disk slot on this VM")


def add_disk(root: ET.Element, path: str, target: str) -> None:
    if not _DEV_RE.match(target or ""):
        raise EditError("bad disk target")
    bus = "sata" if target.startswith("sd") else "virtio"
    d = ET.SubElement(_devices(root), "disk", {"type": "file", "device": "disk"})
    ET.SubElement(d, "driver", {"name": "qemu", "type": "qcow2"})
    ET.SubElement(d, "source", {"file": path})
    ET.SubElement(d, "target", {"dev": target, "bus": bus})


def add_nic(root: ET.Element, network: str, bridge: str, windows: bool) -> None:
    dev = _devices(root)
    if bridge:
        n = ET.SubElement(dev, "interface", {"type": "bridge"})
        ET.SubElement(n, "source", {"bridge": bridge})
    else:
        n = ET.SubElement(dev, "interface", {"type": "network"})
        ET.SubElement(n, "source", {"network": network or "default"})
    ET.SubElement(n, "model", {"type": "e1000e" if windows else "virtio"})


def nic_of(iface: ET.Element) -> dict:
    """{type, name} of one <interface>: `network` → the libvirt network, `bridge` → the host bridge."""
    t = iface.get("type", "")
    src = iface.find("source")
    name = ""
    if src is not None:
        name = src.get("network", "") if t == "network" else src.get("bridge", "") if t == "bridge" else src.get("dev", "")
    return {"type": t, "name": name}


def set_primary_nic(root: ET.Element, network: str, bridge: str, windows: bool) -> None:
    """Point the FIRST network adapter at `bridge` (when given) or the libvirt `network`, keeping its MAC and
    model so the guest sees the same card on a different wire. A VM with no adapter gets one. The names are
    validated by the caller against the host's live list — this only writes attributes, never markup."""
    dev = _devices(root)
    iface = dev.find("interface")
    if iface is None:
        add_nic(root, network, bridge, windows)
        return
    for child in list(iface):
        if child.tag in ("source", "virtualport"):
            iface.remove(child)
    src = ET.Element("source", {"bridge": bridge} if bridge else {"network": network or "default"})
    iface.set("type", "bridge" if bridge else "network")
    iface.insert(0 if iface.find("mac") is None else 1, src)


def set_media(root: ET.Element, iso_path) -> None:
    """Insert (a path) or eject (None). An existing cdrom's source is REPLACED — `--update`, never a
    second source — and a VM created without an installer gets a SATA cdrom on a free sdX target."""
    dev = _devices(root)
    cd = next((d for d in dev.findall("disk") if d.get("device") == "cdrom"), None)
    if cd is None:
        if iso_path is None:
            return
        used = used_targets(root)
        tgt = next(("sd" + c for c in "abcdefghijklmnopqrstuvwxyz" if "sd" + c not in used), None)
        if not tgt:
            raise EditError("no free slot for a CD drive")
        cd = ET.SubElement(dev, "disk", {"type": "file", "device": "cdrom"})
        ET.SubElement(cd, "driver", {"name": "qemu", "type": "raw"})
        ET.SubElement(cd, "target", {"dev": tgt, "bus": "sata"})
        ET.SubElement(cd, "readonly")
    for s in list(cd.findall("source")):
        cd.remove(s)
    if iso_path is not None:
        cd.insert(1 if cd.find("driver") is not None else 0, ET.Element("source", {"file": iso_path}))


def set_meta(root: ET.Element, meta: VmMeta) -> None:
    md = root.find("metadata")
    if md is None:
        md = ET.Element("metadata")
        root.insert(2, md)
    for ch in list(md):
        if ch.tag.startswith("{" + PC_NS + "}") or _local(ch.tag) == "vm":
            md.remove(ch)
    md.append(ET.fromstring(meta.to_xml(prefixed=True)))


def secure_vnc(root: ET.Element, *, force_loopback: bool = False) -> int:
    """Give every VNC display that has no `passwd` a random, ALREADY-EXPIRED one (see
    VNC_PASSWD_EXPIRED). `virsh dumpxml` leaves `passwd` OUT unless asked for security info, so a
    definition read back, edited and defined again (vm.update, a migration's define on the target)
    would otherwise come up with NO VNC authentication — and every console ticket after it would be
    refused, because QEMU will not set a password on a display that has no password auth.

    `force_loopback`: for a definition that came from ANOTHER host (migration), also pin every VNC
    display to 127.0.0.1 — this host's console route is the only door, whatever the source wrote.
    Returns how many displays it changed."""
    n = 0
    for g in root.findall("devices/graphics"):
        if g.get("type") != "vnc":
            continue
        changed = False
        if not g.get("passwd"):
            g.set("passwd", random_vnc_password())
            g.set("passwdValidTo", VNC_PASSWD_EXPIRED)
            changed = True
        if force_loopback:
            socket_listen = any(li.get("type") == "socket" for li in g.findall("listen"))
            if not socket_listen and (g.get("listen") != "127.0.0.1" or any(
                    li.get("type") != "address" or li.get("address") != "127.0.0.1" for li in g.findall("listen"))
                    or not g.findall("listen")):
                g.set("listen", "127.0.0.1")
                for li in g.findall("listen"):
                    g.remove(li)
                g.insert(0, ET.Element("listen", {"type": "address", "address": "127.0.0.1"}))
                changed = True
        n += int(changed)
    return n


def file_disks(xml_text: str) -> list:
    """[{target, path, format, type}] of a definition's DISKS (not cdroms), in definition order."""
    root = _parse(xml_text)
    out = []
    for d in root.findall("devices/disk"):
        if d.get("device", "disk") != "disk":
            continue
        tgt, src, drv = d.find("target"), d.find("source"), d.find("driver")
        out.append({"target": tgt.get("dev", "") if tgt is not None else "",
                    "path": (src.get("file") or "") if src is not None else "",
                    "format": drv.get("type", "") if drv is not None else "", "type": d.get("type", "file")})
    return out


def nvram_seed(xml_text: str) -> tuple:
    """(nvram path, template path, format) from a definition libvirt has expanded — `("", "", "")` for a VM with no
    EFI variable store. libvirt >= 8 writes the template it auto-selected into the inactive XML at DEFINE time."""
    root = _parse(xml_text)
    nv = root.find("os/nvram")
    if nv is None or not (nv.text or "").strip():
        return "", "", ""
    return nv.text.strip(), (nv.get("template") or "").strip(), (nv.get("format") or "").strip()


def to_text(root: ET.Element) -> str:
    return ET.tostring(root, encoding="unicode")
