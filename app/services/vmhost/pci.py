"""PCI passthrough (GPUs and other cards): which PCI devices this host has, what stands between each one and a
VM, and the <hostdev> XML that gives one — read from sysfs only; nothing here binds, unbinds or loads anything.

A PCI device is given to a VM with a `managed='yes'` hostdev: when the VM STARTS, libvirt (running as root on
qemu:///system) detaches it from its host driver and binds it to vfio-pci; when the VM stops, it gives it back.
So attaching is a change to the SAVED definition only, and only while the VM is shut off — a GPU cannot be
hot-plugged into a running guest.

WHAT THIS MODULE DECIDES, each measured, each reported as a sentence that says what to change:
  * HOST PRECONDITIONS: the IOMMU is on (/sys/kernel/iommu_groups is not empty — AMD enables it by default, Intel
    needs `intel_iommu=on`), and vfio-pci exists (loaded, built in, or in modules.dep).
  * THE IOMMU GROUP: VFIO hands a VM a whole group or nothing. Every other endpoint in the device's group must go
    with it or already be on vfio-pci / pci-stub / no driver; bridges are allowed to stay (VFIO permits them).
    A group that fails is refused, naming the members that are in the way.
  * THE HOST IS USING IT: the host's boot display GPU (`boot_vga`), a GPU bound to a host graphics driver
    (nvidia, amdgpu, i915, …) — libvirt would pull it from under whatever is using it, e.g. the app's own CUDA
    work on nas.lan's RTX 3060 — a card with a connected display, a network card whose interface is up, a storage
    controller with something mounted. The one the host BOOTS FROM is not listed at all.
  * A GPU GOES WITH ITS OTHER FUNCTIONS: its HDMI audio (.1) and anything else in the same slot and group, as one
    attach — a guest driver that finds half a card fails.
"""
from __future__ import annotations

import os
import platform
import re
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field

from . import usb

ADDR = re.compile(r"^([0-9a-f]{4}):([0-9a-f]{2}):([0-1][0-9a-f])\.([0-7])\Z")
PCI_IDS = ("/usr/share/hwdata/pci.ids", "/usr/share/misc/pci.ids", "/usr/share/pci.ids")
HOST_GPU_DRIVERS = frozenset({"nvidia", "nouveau", "amdgpu", "radeon", "i915", "xe", "ast", "mgag200", "bochs-drm",
                              "virtio-pci", "qxl", "vmwgfx", "simpledrm", "efifb"})
FREE_DRIVERS = frozenset({"", "vfio-pci", "pci-stub"})
# Never offered: memory controllers, bridges, system peripherals (IOMMU, timers), SMBus, encryption (PSP/CCP),
# non-essential instrumentation (AMD "dummy function" / "reserved SPP").
HIDDEN_CLASS = re.compile(r"^(05|06|08|0c05|10|13)")
CLASS_NAMES = {"01": "storage controller", "02": "network card", "03": "graphics card", "04": "multimedia / audio",
               "07": "communications", "09": "input", "0c03": "USB controller", "0c": "serial bus", "0d": "wireless",
               "0b": "processor", "12": "accelerator", "11": "signal processing"}


@dataclass
class PciDevice:
    address: str                 # "0000:01:00.0" — the id a client names it by
    vendor: str                  # "10de"
    product: str                 # "2504"
    cls: str                     # "030000"
    driver: str = ""
    group: str = ""              # IOMMU group number, "" when there is no IOMMU
    vendor_name: str = ""
    name: str = ""
    boot_vga: bool = False
    system: bool = False         # the host boots from it — never listed
    busy: str = ""               # the host is using it — listed, refused
    hidden: bool = False         # a bridge / host plumbing
    displays: list = field(default_factory=list)

    @property
    def is_gpu(self) -> bool:
        return self.cls.startswith("03")

    @property
    def slot(self) -> str:
        return self.address.rsplit(".", 1)[0]

    @property
    def label(self) -> str:
        who = " ".join(x for x in (self.vendor_name, self.name) if x) or "PCI device"
        return f"{who} ({self.vendor}:{self.product})"

    def class_name(self) -> str:
        return CLASS_NAMES.get(self.cls[:4]) or CLASS_NAMES.get(self.cls[:2], "")


def _read(path: str, limit: int = 256) -> str:
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            return f.read(limit).strip()
    except OSError:
        return ""


def _link_name(path: str) -> str:
    try:
        return os.path.basename(os.readlink(path))
    except OSError:
        return ""


def _under(sys_class_dir: str, real_dev: str) -> list:
    """Entries of a /sys/class/<x> directory whose device lives under `real_dev` (the realpath of a PCI device)."""
    try:
        names = sorted(os.listdir(sys_class_dir))
    except OSError:
        return []
    out = []
    for n in names:
        if os.path.realpath(os.path.join(sys_class_dir, n)).startswith(real_dev + os.sep):
            out.append(n)
    return out


def host_checks(sys_root: str = "/sys", modules_dir: str | None = None, cpuinfo: str = "/proc/cpuinfo",
                etc: str = "/etc", uri: str = "qemu:///system") -> list:
    """The host-wide preconditions, measured: [{id, ok, label, fix}]."""
    groups = os.path.join(sys_root, "kernel", "iommu_groups")
    try:
        n_groups = len(os.listdir(groups))
    except OSError:
        n_groups = 0
    vendor = ""
    try:
        with open(cpuinfo, "r", encoding="utf-8", errors="replace") as f:
            head = f.read(4096)
        vendor = "amd" if "AuthenticAMD" in head else "intel" if "GenuineIntel" in head else ""
    except OSError:
        pass
    param = {"amd": "amd_iommu=on iommu=pt", "intel": "intel_iommu=on iommu=pt"}.get(
        vendor, "intel_iommu=on iommu=pt (Intel) or amd_iommu=on iommu=pt (AMD)")
    where = []
    if os.path.exists(os.path.join(etc, "kernel", "cmdline")):
        where.append(f"add `{param}` to /etc/kernel/cmdline and reinstall the kernel (kernel-install / emerge --config "
                     "of the kernel package) so systemd-boot picks it up")
    if os.path.exists(os.path.join(etc, "default", "grub")):
        where.append(f"add `{param}` to GRUB_CMDLINE_LINUX in /etc/default/grub, then run grub-mkconfig -o "
                     "/boot/grub/grub.cfg")
    if not where:
        where.append(f"add `{param}` to the kernel command line in your boot loader's configuration")
    fw = "SVM and IOMMU (AMD-Vi)" if vendor == "amd" else "VT-d" if vendor == "intel" else "VT-d / AMD-Vi"
    iommu_fix = (f"Turn on {fw} in the firmware (BIOS/UEFI) setup, " + "; or ".join(where) + ", then reboot.")
    out = [{"id": "iommu", "ok": n_groups > 0,
            "label": f"IOMMU is on ({n_groups} groups)" if n_groups else "IOMMU is off",
            "fix": "" if n_groups else iommu_fix}]
    rel = platform.release()
    mdir = modules_dir or os.path.join("/lib/modules", rel)
    vfio = os.path.isdir(os.path.join(sys_root, "module", "vfio_pci")) or \
        os.path.isdir(os.path.join(sys_root, "bus", "pci", "drivers", "vfio-pci"))
    if not vfio:
        for f in ("modules.builtin", "modules.dep"):
            try:
                with open(os.path.join(mdir, f), "r", encoding="utf-8", errors="replace") as fh:
                    if re.search(r"vfio[-_]pci\.ko", fh.read()):
                        vfio = True
                        break
            except OSError:
                continue
    out.append({"id": "vfio", "ok": vfio, "label": "vfio-pci is available" if vfio else "vfio-pci is missing",
                "fix": "" if vfio else "Build the running kernel with CONFIG_VFIO and CONFIG_VFIO_PCI (a Gentoo "
                                       "dist-kernel has both), then reboot into it."})
    system = uri.startswith("qemu:///system") or uri.startswith("qemu+")
    out.append({"id": "system", "ok": system,
                "label": "libvirt runs as root (qemu:///system)" if system else "libvirt runs as your user (qemu:///session)",
                "fix": "" if system else "PCI passthrough needs the system libvirt: only root can hand a card to "
                                         "vfio-pci. Use a server host (Admin → VMs) for this VM."})
    return out


def scan(sys_root: str = "/sys", mountinfo: str = "/proc/self/mountinfo", swaps: str = "/proc/swaps",
         ids_paths=PCI_IDS, zpool_status=None, root_dev=None) -> list:
    """Every PCI device on this host with `hidden`/`system`/`busy` decided (the caller filters)."""
    base = os.path.join(sys_root, "bus", "pci", "devices")
    names = sorted(os.listdir(base))                                  # FileNotFoundError: no PCI bus
    sys_block = os.path.join(sys_root, "class", "block")
    mounts = usb._mounts(mountinfo, swaps, sys_root, zpool_status, root_dev)
    lost = usb.root_unresolved(mounts)
    usb_dir = os.path.join(sys_root, "bus", "usb", "devices")
    out = []
    for a in names:
        if not ADDR.match(a):
            continue
        d = os.path.join(base, a)
        vendor = _read(os.path.join(d, "vendor")).lower().removeprefix("0x")
        product = _read(os.path.join(d, "device")).lower().removeprefix("0x")
        cls = _read(os.path.join(d, "class")).lower().removeprefix("0x")
        if not (usb.HEX4.match(vendor) and usb.HEX4.match(product) and re.fullmatch(r"[0-9a-f]{6}", cls)):
            continue
        dev = PciDevice(address=a, vendor=vendor, product=product, cls=cls, driver=_link_name(os.path.join(d, "driver")),
                        group=_link_name(os.path.join(d, "iommu_group")), boot_vga=_read(os.path.join(d, "boot_vga")) == "1",
                        hidden=bool(HIDDEN_CLASS.match(cls)))
        dev.vendor_name, dev.name = usb.ids_names(vendor, product, ids_paths)
        real = os.path.realpath(d)
        why = []
        for b in _under(sys_block, real):
            for mp, via in usb._uses(sys_block, b, mounts):
                if mp in usb.SYSTEM_MOUNTS:
                    dev.system = True
                if mp and not why:
                    why.append(f"the host has {b} mounted at {mp}" + (f" (through {via})" if via else ""))
                elif not mp and not why:
                    why.append(f"the host is using {b} (part of {via})")
        if not dev.system and lost and _under(sys_block, real):
            dev.system = True                             # the root disk could be behind it: never offered
        elif not why and mounts.unresolved and _under(sys_block, real):
            why.append("the host could not tell which disk " + ", ".join(dict.fromkeys(mounts.unresolved)) +
                       " is on, and this controller has disks behind it")
        if cls.startswith("0c03"):
            # a USB controller carrying the host's keyboard/mouse: giving it away takes the host's input
            hid = []
            for u in _under(usb_dir, real):
                if ":" in u and _read(os.path.join(usb_dir, u, "bInterfaceClass")).lower() == "03":
                    hid.append(u.split(":")[0])
            if hid:
                why.append("the host's HID devices (keyboard, mouse, controller …) are connected through it (" +
                           ", ".join(sorted(set(hid))) + ")")
        for n in _under(os.path.join(sys_root, "class", "net"), real):
            if usb.iface_up(os.path.join(sys_root, "class", "net"), n):
                why.append(f"the host's network interface {n} is up")
                break
        drm = os.path.join(sys_root, "class", "drm")
        for c in _under(drm, real):
            if "-" in c and _read(os.path.join(drm, c, "status")) == "connected":
                dev.displays.append(c.split("-", 1)[1])
        if dev.boot_vga:
            why.insert(0, "it is the host's boot display GPU")
        if dev.displays:
            why.append("a display is connected to it (" + ", ".join(dev.displays) + ")")
        if dev.is_gpu and dev.driver in HOST_GPU_DRIVERS:
            why.append(f"the host's {dev.driver} driver is using it")
        dev.busy = "; ".join(why)
        out.append(dev)
    return out


def group_members(sys_root: str, group: str) -> list:
    try:
        return sorted(os.listdir(os.path.join(sys_root, "kernel", "iommu_groups", group, "devices")))
    except OSError:
        return []


def plan(dev: PciDevice, devices: list, groups: dict) -> dict:
    """What attaching `dev` takes: {attach: [PciDevice], blockers: [str], checks: [{id, ok, label, fix}]}.
    `groups`: {group number: [member addresses]}."""
    by_addr = {d.address: d for d in devices}
    members = groups.get(dev.group, []) if dev.group else []
    take = [dev]
    if dev.is_gpu:
        # every other function of the card that shares its group, and its HDMI audio at function .1 even when that
        # sits in a group of its own. NOT any audio in the slot: on an AMD APU (nas.lan, 0000:10:00.x) the same
        # slot also carries the motherboard's own HD audio at .6, which is not the GPU's to give away.
        for d in devices:
            if d.slot != dev.slot or d.address == dev.address or d.hidden:
                continue
            if (dev.group and d.group == dev.group) or (d.address.endswith(".1") and d.cls.startswith("0403")):
                take.append(d)
    names = {d.address for d in take}
    blockers, checks = [], []
    if not dev.group:
        checks.append({"id": "group", "ok": False, "label": "No IOMMU group",
                       "fix": "The IOMMU is off, so no device can be passed through — see the host checks above."})
    else:
        extra = []
        for m in members:
            if m in names:
                continue
            md = by_addr.get(m)
            if md is None or md.cls.startswith("06"):
                continue                                          # bridges may stay with the host
            if md.driver not in FREE_DRIVERS:
                extra.append(md)
        for t in take[1:]:
            if t.group and t.group != dev.group:
                for m in groups.get(t.group, []):
                    md = by_addr.get(m)
                    if m not in names and md is not None and not md.cls.startswith("06") and md.driver not in FREE_DRIVERS:
                        extra.append(md)
        ok = not extra
        checks.append({"id": "group", "ok": ok,
                       "label": f"IOMMU group {dev.group} can be given whole" if ok else
                                f"IOMMU group {dev.group} also holds devices the host uses",
                       "fix": "" if ok else "A VM gets a whole IOMMU group or nothing. Also in this group: " +
                              ", ".join(f"{m.address} {m.label} ({m.driver or 'no driver'})" for m in extra) +
                              ". Move the card to another slot, turn on ACS in the firmware setup, or bind those "
                              "devices to vfio-pci too."})
        if extra:
            blockers.append("its IOMMU group " + dev.group + " also holds " +
                            ", ".join(f"{m.address} ({m.label})" for m in extra))
    for t in take:
        if t.system:                                          # a sibling function the host (may) boot through
            blockers.append(f"{t.address}: the host may boot from a disk behind it")
        elif t.busy:
            blockers.append(f"{t.address}: {t.busy}")
    gpu_busy = [t for t in take if t.is_gpu and t.driver in HOST_GPU_DRIVERS]
    if any(t.boot_vga for t in take):
        checks.append({"id": "host-gpu", "ok": False, "label": "This is the host's own display GPU",
                       "fix": "The host shows its console on this card. Give the VM a second GPU instead, or set the "
                              "other card as the primary display in the firmware setup."})
    elif gpu_busy:
        ids = ",".join(f"{t.vendor}:{t.product}" for t in take)
        drv = gpu_busy[0].driver
        checks.append({"id": "host-gpu", "ok": False, "label": f"The host's {drv} driver is using it",
                       "fix": "Hand it to vfio-pci at boot instead: create /etc/modprobe.d/vfio.conf containing "
                              f"`options vfio-pci ids={ids}` and `softdep {drv.replace('-', '_')} pre: vfio-pci`, "
                              "rebuild the initramfs (dracut --force), and reboot. The host then no longer uses "
                              "this GPU."})
    elif dev.is_gpu:
        checks.append({"id": "host-gpu", "ok": True, "label": "Not used by the host (" + (dev.driver or "no driver") + ")",
                       "fix": ""})
    other = [t for t in take if t.busy and not (t.is_gpu and (t.boot_vga or t.driver in HOST_GPU_DRIVERS))]
    if other:
        checks.append({"id": "in-use", "ok": False, "label": "The host is using it",
                       "fix": "; ".join(f"{t.address}: {t.busy}" for t in other) + " — stop using it on the host first."})
    return {"attach": take, "blockers": blockers, "checks": checks}


def vm_checks(domain_xml: str) -> list:
    """What makes a passed-through card work in THIS guest — advisory, never a refusal: UEFI firmware and the
    q35 machine (PCIe). A legacy-BIOS i440fx guest often boots a passed GPU to a black screen."""
    try:
        root = ET.fromstring(str(domain_xml or "").strip())
    except ET.ParseError:
        return []
    os_el = root.find("os")
    typ = os_el.find("type") if os_el is not None else None
    machine = (typ.get("machine") or "") if typ is not None else ""
    efi = os_el is not None and (os_el.get("firmware") == "efi" or os_el.find("loader") is not None)
    q35 = "q35" in machine
    return [{"id": "efi", "ok": efi, "label": "UEFI firmware" if efi else "Legacy BIOS firmware",
             "fix": "" if efi else "GPUs are most reliable with UEFI (OVMF) firmware — create the VM with UEFI."},
            {"id": "q35", "ok": q35, "label": f"{machine or 'unknown'} machine" + ("" if q35 else " (not q35)"),
             "fix": "" if q35 else "A q35 machine gives the card a real PCIe slot — create the VM as q35."}]


def hostdevs(xml_text: str) -> list:
    """The PCI host devices of a definition: [{address}] (normalised to 0000:01:00.0)."""
    try:
        root = ET.fromstring(str(xml_text or "").strip())
    except ET.ParseError:
        return []
    out = []
    for h in root.findall("devices/hostdev"):
        if h.get("mode", "subsystem") != "subsystem" or h.get("type") != "pci":
            continue
        a = h.find("source/address")
        if a is None:
            continue
        try:
            dom, bus, slot, fn = (int(str(a.get(k) or "0"), 0) for k in ("domain", "bus", "slot", "function"))
        except ValueError:
            continue
        out.append({"address": f"{dom:04x}:{bus:02x}:{slot:02x}.{fn:x}"})
    return out


def hostdev_xml(address: str) -> str:
    m = ADDR.match(str(address or ""))
    if not m:
        raise ValueError("a PCI address looks like 0000:01:00.0")
    dom, bus, slot, fn = m.groups()
    h = ET.Element("hostdev", {"mode": "subsystem", "type": "pci", "managed": "yes"})
    src = ET.SubElement(h, "source")
    ET.SubElement(src, "address", {"domain": "0x" + dom, "bus": "0x" + bus, "slot": "0x" + slot, "function": "0x" + fn})
    return ET.tostring(h, encoding="unicode")
