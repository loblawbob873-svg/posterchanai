"""USB passthrough: which physical USB devices this host has, which of them a VM may be given, and the
<hostdev> XML that gives one — no libvirt calls here (backend.py and devices.py do those).

WHAT IS NEVER OFFERED, decided from sysfs and never from anything a client sends:
  * a HUB or a ROOT HUB (`usbN`, or class 09 on the device or any interface) — passing a hub takes every
    device behind it, the keyboard included;
  * the device the host BOOTS FROM, or any device holding a filesystem the host needs (`/`, `/boot`, …),
    followed through the block layer: a USB disk that is a member of an md array under a mounted LVM
    volume is as much "the host's disk" as one mounted directly. Measured on nas.lan: a USB-SATA bridge
    carries `sdd`, a RAID member of `/raid` — offering it would hand a VM a live array member.
Any other device the host is USING (mounted anywhere, swap, an md/dm member) is listed but `busy`, with the
reason, and attaching it is refused: the host's driver lets go of it the instant QEMU claims it, and a
filesystem mounted from it is then written to a device that is gone.

ARGUMENTS ARE IDS, NEVER XML OR PATHS. A client names a device by vendor/product (exactly four lowercase hex
digits each) and optionally bus/device numbers; the XML is built here from those validated values with
ElementTree, and the device must exist on this host right now.
"""
from __future__ import annotations

import os
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

HEX4 = re.compile(r"^[0-9a-f]{4}\Z")         # \Z, not $: `$` also matches before a trailing newline
SYSTEM_MOUNTS = ("/", "/boot", "/boot/efi", "/efi", "/usr", "/var")
CLASS_NAMES = {
    "01": "audio", "02": "communications", "03": "input device", "05": "physical", "06": "camera / imaging",
    "07": "printer", "08": "storage", "09": "hub", "0a": "communications", "0b": "smart card",
    "0d": "content security", "0e": "video", "0f": "health", "10": "audio/video", "11": "billboard",
    "dc": "diagnostic", "e0": "wireless / bluetooth", "ef": "miscellaneous", "fe": "application",
    "ff": "vendor specific",
}


@dataclass
class UsbDevice:
    sysname: str                 # "1-11" — the sysfs name, never sent to a client as an id
    bus: int
    device: int
    vendor: str                  # "0781"
    product: str                 # "5581"
    manufacturer: str = ""
    name: str = ""               # the product string, or the usb.ids name
    cls: str = ""                # device class, or the first interface's when the device says "per interface"
    speed: str = ""              # Mbit/s as sysfs reports it
    hub: bool = False
    system: bool = False         # the host needs it (root/boot) — never offered
    busy: str = ""               # the host is using it — listed, refused
    blocks: list = field(default_factory=list)

    @property
    def label(self) -> str:
        who = " ".join(x for x in (self.manufacturer, self.name) if x) or "USB device"
        return f"{who} ({self.vendor}:{self.product})"

    def view(self) -> dict:
        return {"vendor": self.vendor, "product": self.product, "bus": self.bus, "device": self.device,
                "manufacturer": self.manufacturer, "name": self.name, "label": self.label,
                "class": self.cls, "class_name": CLASS_NAMES.get(self.cls, ""), "speed": self.speed,
                "busy": self.busy}


def _read(path: str, limit: int = 256) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit).strip()
    except OSError:
        return ""


def _clean_text(s: str) -> str:
    """A sysfs string as something safe to show: printable, one line, at most 64 characters."""
    s = "".join(ch for ch in str(s or "") if ch.isprintable())
    return re.sub(r"\s+", " ", s).strip()[:64]


_IDS_CACHE: dict = {}
USB_IDS = ("/usr/share/hwdata/usb.ids", "/usr/share/misc/usb.ids", "/usr/share/usb.ids")


def ids_names(vendor: str, product: str, ids_paths=USB_IDS) -> tuple:
    """(vendor name, product name) from a usb.ids / pci.ids database (the same format) — what `lsusb` and
    `lspci` print. A missing database or id → ""."""
    key = (tuple(ids_paths), vendor, product)
    if key in _IDS_CACHE:
        return _IDS_CACHE[key]
    out = ("", "")
    for p in ids_paths:
        try:
            with open(p, "r", encoding="utf-8", errors="replace") as f:
                vname, inside = "", False
                for line in f:
                    if line.startswith("#") or not line.strip():
                        continue
                    if not line.startswith("\t"):
                        if inside:
                            break
                        if line[:4].lower() == vendor and line[4:6] == "  ":
                            vname, inside = line[6:].strip(), True
                        elif line.startswith("C "):
                            break                        # the class table follows the vendors
                        continue
                    if inside and not line.startswith("\t\t") and line[1:5].lower() == product:
                        out = (vname, line[7:].strip())
                        break
                if inside:
                    out = out if out[1] else (vname, "")
                    break
        except OSError:
            continue
    if len(_IDS_CACHE) > 1024:
        _IDS_CACHE.clear()
    _IDS_CACHE[key] = (_clean_text(out[0]), _clean_text(out[1]))
    return _IDS_CACHE[key]


def _zpool_status():
    """`zpool status -P` (full vdev paths); None when it could not be asked — no zpool binary, a timeout, a nonzero
    exit. The caller decides what None means: nothing when ZFS is not loaded, "every disk may be a pool member"
    when it is."""
    import shutil
    import subprocess
    z = shutil.which("zpool")
    if not z:
        return None
    try:
        r = subprocess.run([z, "status", "-P"], capture_output=True, text=True, timeout=10)
    except (OSError, subprocess.SubprocessError):
        return None
    return r.stdout if r.returncode == 0 else None


class Mounts(dict):
    """{block device name: [mountpoint | "swap", …]}, plus `unresolved`: the mountpoints of the host's own
    filesystems that could not be traced to any block device. A caller must treat an unresolved SYSTEM mount as
    "any disk may be the one the host runs from" — failing closed, never open."""
    unresolved: list


def _mounts(mountinfo: str, swaps: str, sys_root: str = "/sys", zpool_status=None, root_dev=None) -> Mounts:
    """Which block devices the host's filesystems and swap live on. Every way a mount names its device is tried:

      * the SOURCE path (`/dev/mapper/luks-…` → dm-0) — the only one that works for btrfs, whose major:minor
        field is an anonymous 0:NN;
      * the major:minor field through /sys/dev/block/M:m — the only one that works for `/dev/root`, which is not
        a real node;
      * for `/`, stat('/').st_dev through the same table;
      * btrfs: every member in /sys/fs/btrfs/<fsid>/devices shares the mounts of the filesystem (a multi-device
        root lives on several disks, only one of which is the SOURCE);
      * ZFS: `zpool status -P` names each pool's vdevs; a dataset `pool/x` lives on all of them.
    What none of these can place is returned in `unresolved`."""
    out = Mounts()
    out.unresolved = []
    block = os.path.join(sys_root, "class", "block")

    def known(name: str) -> bool:
        return bool(name) and os.path.exists(os.path.join(block, name))

    def by_path(src: str) -> str:
        return os.path.basename(os.path.realpath(src)) if src.startswith("/dev/") else ""

    def by_majmin(mm: str) -> str:
        if not re.fullmatch(r"\d+:\d+", mm or "") or mm.startswith("0:"):
            return ""
        return os.path.basename(os.path.realpath(os.path.join(sys_root, "dev", "block", mm)))

    rows = []
    try:
        with open(mountinfo, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                left, _, right = line.partition(" - ")
                parts, rparts = left.split(), right.split()
                if len(parts) >= 5 and len(rparts) >= 2:
                    rows.append((parts[2], parts[4].replace("\\040", " "), rparts[0], rparts[1]))
    except OSError:
        out.unresolved.append("/")                        # no mount table: no disk can be shown not to be the root
    btrfs_left, zfs = [], {}
    for mm, mp, fstype, src in rows:
        # bcachefs names every member in the source: /dev/sda1:/dev/sdb1
        srcs = src.split(":") if fstype == "bcachefs" else [src]
        names = {n for n in [by_path(x) for x in srcs] + [by_majmin(mm)] if known(n)}
        if not names and mp == "/":
            try:
                st = root_dev if root_dev is not None else os.stat("/").st_dev
                n = by_majmin(f"{os.major(st)}:{os.minor(st)}")
                if known(n):
                    names.add(n)
            except OSError:
                pass
        for n in names:
            out.setdefault(n, []).append(mp)
        if fstype == "zfs":
            zfs.setdefault(src.split("/", 1)[0], []).append(mp)
        elif not names and fstype in ("btrfs", "bcachefs"):
            btrfs_left.append(mp)
        elif not names and (src.startswith("/dev/") or fstype in ("ext2", "ext3", "ext4", "xfs", "f2fs", "vfat",
                                                                   "bcachefs", "jfs", "reiserfs", "ntfs3")):
            out.unresolved.append(mp)
    # btrfs and bcachefs: one filesystem, several disks — every member shares the filesystem's mounts
    members = {}
    fsdir = os.path.join(sys_root, "fs", "btrfs")
    for fs in _ls(fsdir):
        if os.path.isdir(os.path.join(fsdir, fs, "devices")):
            members["btrfs:" + fs] = _ls(os.path.join(fsdir, fs, "devices"))
    bdir = os.path.join(sys_root, "fs", "bcachefs")
    for fs in _ls(bdir):
        devs = [os.path.basename(os.path.realpath(os.path.join(bdir, fs, d, "block")))
                for d in _ls(os.path.join(bdir, fs)) if d.startswith("dev-")]
        if devs:
            members["bcachefs:" + fs] = devs
    given = False
    for fs, devs in members.items():
        mps = sorted({m for d in devs for m in out.get(d, []) if m != "swap"})
        if btrfs_left and (not mps or len(members) == 1):
            mps = sorted(set(mps) | set(btrfs_left))       # cannot tell which fs: every member keeps them all
            given = True
        for d in devs:
            for m in mps:
                if m not in out.setdefault(d, []):
                    out[d].append(m)
    if btrfs_left and not given:
        out.unresolved.extend(btrfs_left)                 # no filesystem took them: say so, never drop them
    # ZFS: every vdev of every IMPORTED pool is in use — mounted or not (a pool with nothing mounted is still
    # written by the host: scrubs, resilvers, zvols); a mounted dataset lives on every vdev of its pool
    text = _zpool_status() if zpool_status is None else zpool_status
    if text is None and os.path.isdir(os.path.join(sys_root, "module", "zfs")):
        out.unresolved.append("/")                        # ZFS is loaded and cannot say which disks are its pools
    pool, vdevs = "", {}
    for line in (text or "").splitlines():
        m = re.match(r"\s*pool:\s*(\S+)", line)
        if m:
            pool = m.group(1)
            continue
        tok = line.split()
        if pool and tok and tok[0].startswith("/dev/"):
            vdevs.setdefault(pool, []).append(by_path(tok[0]))
    for p, devs in vdevs.items():
        for d in devs:
            out.setdefault(d, []).extend(zfs.get(p) or [f"ZFS pool {p}"])
    for p, mps in zfs.items():
        if not vdevs.get(p):
            out.unresolved.extend(mps)
    try:
        with open(swaps, "r", encoding="utf-8", errors="replace") as f:
            for line in list(f)[1:]:
                p = line.split()
                if p and p[0].startswith("/dev/") and by_path(p[0]):
                    out.setdefault(by_path(p[0]), []).append("swap")
    except OSError:
        out.unresolved.append("swap")                     # no swap table: any disk may be the host's swap
    return out


def _ls(p: str) -> list:
    try:
        return sorted(os.listdir(p))
    except OSError:
        return []


def iface_up(net_dir: str, name: str) -> bool:
    """Is a network interface in use? IFF_UP from `flags` (operstate is "unknown" for many up interfaces — tun,
    some USB adapters, bridges' ports); anything that cannot be read counts as up unless operstate says down."""
    f = _read(os.path.join(net_dir, name, "flags"))
    try:
        return bool(int(f, 16) & 0x1)
    except ValueError:
        return _read(os.path.join(net_dir, name, "operstate")) != "down"


def root_unresolved(mounts) -> list:
    """The host's own filesystems that could not be traced to a disk."""
    return [m for m in getattr(mounts, "unresolved", []) if m in SYSTEM_MOUNTS]


def _uses(sys_block: str, name: str, mounts: dict, depth: int = 0) -> list:
    """Everything the host does with block device `name`: its own mounts, and — through `holders/` (md, dm,
    LVM, LUKS) — the mounts of every device built on top of it. [(mountpoint, via-holder or "")]."""
    if depth > 8:
        return []
    out = [(m, "") for m in mounts.get(name, [])]
    hdir = os.path.join(sys_block, name, "holders")
    try:
        holders = sorted(os.listdir(hdir))
    except OSError:
        holders = []
    for h in holders:
        sub = _uses(sys_block, h, mounts, depth + 1)
        label = _read(os.path.join(sys_block, h, "dm", "name"), 128) or h
        out.extend((m, v or label) for m, v in sub)
        if not sub:
            out.append(("", label))                      # held by a device nothing mounts: still in use
    return out


def scan(sys_root: str = "/sys", mountinfo: str = "/proc/self/mountinfo", swaps: str = "/proc/swaps",
         ids_paths=None, zpool_status=None, root_dev=None) -> list:
    """Every USB device on this host that is not a hub or a root hub, with `system`/`busy` decided. Raises
    FileNotFoundError when the host has no USB bus in sysfs at all."""
    devs_dir = os.path.join(sys_root, "bus", "usb", "devices")
    names = sorted(os.listdir(devs_dir))                  # FileNotFoundError: no USB here
    sys_block = os.path.join(sys_root, "class", "block")
    try:
        blocks = [(b, os.path.realpath(os.path.join(sys_block, b))) for b in sorted(os.listdir(sys_block))]
    except OSError:
        blocks = []
    mounts = _mounts(mountinfo, swaps, sys_root, zpool_status, root_dev)
    lost = root_unresolved(mounts)
    # ANY filesystem (or swap) the host has that could not be traced to a disk may be on this one: a disk is busy
    # then, not only when it is the root that got lost — a VM taking a disk the host has /raid mounted from is the
    # same loss, just not of the OS.
    untraced = list(dict.fromkeys(getattr(mounts, "unresolved", [])))
    net = os.path.join(sys_root, "class", "net")
    out = []
    for n in names:
        base = os.path.join(devs_dir, n)
        if ":" in n or n.startswith("usb"):
            continue                                       # an interface, or a root hub
        bus, dev = _read(os.path.join(base, "busnum")), _read(os.path.join(base, "devnum"))
        vendor, product = _read(os.path.join(base, "idVendor")).lower(), _read(os.path.join(base, "idProduct")).lower()
        if not (bus.isdigit() and dev.isdigit() and HEX4.match(vendor) and HEX4.match(product)):
            continue
        cls = _read(os.path.join(base, "bDeviceClass")).lower()
        icls = []
        for sub in names:
            if sub.startswith(n + ":"):
                c = _read(os.path.join(devs_dir, sub, "bInterfaceClass")).lower()
                if c:
                    icls.append(c)
        hub = cls == "09" or "09" in icls
        if cls in ("", "00") and icls:
            cls = icls[0]
        d = UsbDevice(sysname=n, bus=int(bus), device=int(dev), vendor=vendor, product=product,
                      manufacturer=_clean_text(_read(os.path.join(base, "manufacturer"))),
                      name=_clean_text(_read(os.path.join(base, "product"))), cls=cls,
                      speed=_clean_text(_read(os.path.join(base, "speed"), 16)), hub=hub)
        if not d.name or not d.manufacturer:
            vn, pn = ids_names(vendor, product, ids_paths or USB_IDS)
            d.manufacturer = d.manufacturer or vn
            d.name = d.name or pn
        real = os.path.realpath(base) + os.sep
        why = []
        for b, breal in blocks:
            if not breal.startswith(real):
                continue
            d.blocks.append(b)
            for mp, via in _uses(sys_block, b, mounts):
                if mp in SYSTEM_MOUNTS:
                    d.system = True
                why.append((mp, via, b))
        if why:
            mp, via, b = why[0]
            if mp == "swap":
                d.busy = f"the host uses {b} as swap"
            elif mp.startswith("ZFS pool "):
                d.busy = f"{b} is a member of {mp} on the host"
            elif mp and via:
                d.busy = f"the host has it mounted at {mp} (through {via})"
            elif mp:
                d.busy = f"the host has it mounted at {mp}"
            else:
                d.busy = f"the host is using {b} (part of {via})"
        elif d.blocks and untraced:
            d.busy = ("the host could not tell which disk " + ", ".join(lost or untraced) + " is on, so no disk is "
                      "given to a VM (refusing rather than risk one the host is using)")
        up = [i for i in net_under(net, real) if iface_up(net, i)]
        if up and not d.busy:
            d.busy = f"the host's network interface {up[0]} is up on it"
        out.append(d)
    return out


def net_under(net_dir: str, real_prefix: str) -> list:
    """Network interfaces whose device lives under `real_prefix` (a USB adapter, a PCI card)."""
    try:
        names = sorted(os.listdir(net_dir))
    except OSError:
        return []
    pre = real_prefix if real_prefix.endswith(os.sep) else real_prefix + os.sep
    return [n for n in names if os.path.realpath(os.path.join(net_dir, n)).startswith(pre)]


def offered(devices: list) -> list:
    """What a VM may be offered: no hubs, nothing the host boots from."""
    return [d for d in devices if not d.hub and not d.system]


# ---------------------------------------------------------------------------------------------- hostdev XML
def _int(v):
    try:
        return int(str(v), 0)
    except (TypeError, ValueError):
        return None


def _hexid(v) -> str:
    """libvirt writes ids as `0x0781`; this module speaks `0781`."""
    s = str(v or "").strip().lower()
    s = s[2:] if s.startswith("0x") else s
    return s.zfill(4) if re.fullmatch(r"[0-9a-f]{1,4}", s) else ""


def hostdevs(xml_text: str) -> list:
    """The USB host devices of a domain definition: [{vendor, product, bus, device}] (a missing field is None,
    `bus`/`device` are what libvirt resolved on a running domain, or what the definition pins)."""
    try:
        root = ET.fromstring(str(xml_text or "").strip())
    except ET.ParseError:
        return []
    out = []
    for h in root.findall("devices/hostdev"):
        if h.get("mode", "subsystem") != "subsystem" or h.get("type") != "usb":
            continue
        src = h.find("source")
        if src is None:
            continue
        v, p, a = src.find("vendor"), src.find("product"), src.find("address")
        out.append({"vendor": _hexid(v.get("id")) if v is not None else None,
                    "product": _hexid(p.get("id")) if p is not None else None,
                    "bus": _int(a.get("bus")) if a is not None else None,
                    "device": _int(a.get("device")) if a is not None else None})
    return out


def matches(entry: dict, dev) -> bool:
    """Does a <hostdev> entry name this physical device? The way libvirt resolves one: by bus/device when the
    entry pins them, and by vendor/product otherwise (and both must agree when both are present)."""
    vendor = dev["vendor"] if isinstance(dev, dict) else dev.vendor
    product = dev["product"] if isinstance(dev, dict) else dev.product
    bus = dev.get("bus") if isinstance(dev, dict) else dev.bus
    num = dev.get("device") if isinstance(dev, dict) else dev.device
    have_addr = entry.get("bus") is not None and entry.get("device") is not None
    have_ids = bool(entry.get("vendor") and entry.get("product"))
    if not (have_addr or have_ids):
        return False
    if have_ids and (entry["vendor"] != vendor or entry["product"] != product):
        return False
    if have_addr and bus is not None and num is not None and (entry["bus"] != bus or entry["device"] != num):
        return False
    return True


def hostdev_xml(vendor: str, product: str, bus=None, device=None, *, optional: bool = True) -> str:
    """The <hostdev> for one device, from VALIDATED values only (checked again here — this is the line that
    writes into libvirt). `optional`: a VM whose device is unplugged still starts (startupPolicy)."""
    if not (HEX4.match(str(vendor)) and HEX4.match(str(product))):
        raise ValueError("vendor/product must be four hex digits")
    h = ET.Element("hostdev", {"mode": "subsystem", "type": "usb", "managed": "yes"})
    src = ET.SubElement(h, "source", {"startupPolicy": "optional"} if optional else {})
    ET.SubElement(src, "vendor", {"id": "0x" + vendor})
    ET.SubElement(src, "product", {"id": "0x" + product})
    if bus is not None and device is not None:
        if not (isinstance(bus, int) and isinstance(device, int) and 1 <= bus <= 999 and 1 <= device <= 999):
            raise ValueError("bus/device must be numbers")
        ET.SubElement(src, "address", {"bus": str(bus), "device": str(device)})
    return ET.tostring(h, encoding="unicode")
