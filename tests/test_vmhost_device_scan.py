"""The host device scanners (usb.scan / pci.scan / pci.host_checks) against FAKE sysfs trees shaped like nas.lan's
(measured read-only 2026-09-22): root hubs and hubs are never offered, the disk the host boots from is never
listed, a USB disk that is an md member under a mounted LVM volume is BUSY (followed through holders/), the
host's boot GPU and a GPU on the nvidia driver are busy, an up NIC is busy, and the IOMMU / vfio preconditions
are measured and say what to change.
"""
import os

from app.services.vmhost import pci, usb

PCI_ROOT = "devices/pci0000:00"


def w(path, text):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as f:
        f.write(text + "\n")


def link(target, name):
    os.makedirs(os.path.dirname(name), exist_ok=True)
    os.symlink(target, name)


def usb_dev(sys, ctrl, name, vendor, product, *, cls="00", ifaces=("08",), manufacturer="", product_s="", bus=1, dev=2):
    real = os.path.join(sys, PCI_ROOT, ctrl, f"usb{bus}", name) if not name.startswith("usb") else \
        os.path.join(sys, PCI_ROOT, ctrl, name)
    for f, v in (("busnum", str(bus)), ("devnum", str(dev)), ("idVendor", vendor), ("idProduct", product),
                 ("bDeviceClass", cls), ("speed", "480")):
        w(os.path.join(real, f), v)
    if manufacturer:
        w(os.path.join(real, "manufacturer"), manufacturer)
    if product_s:
        w(os.path.join(real, "product"), product_s)
    link(real, os.path.join(sys, "bus/usb/devices", name))
    for i, c in enumerate(ifaces):
        ir = os.path.join(real, f"{name}:1.{i}")
        w(os.path.join(ir, "bInterfaceClass"), c)
        link(ir, os.path.join(sys, "bus/usb/devices", f"{name}:1.{i}"))
    return real


def block(sys, parent_real, name, parts=(), holders=None):
    real = os.path.join(parent_real, "host6/target6:0:0/6:0:0:0/block", name)
    os.makedirs(os.path.join(real, "holders"), exist_ok=True)
    link(real, os.path.join(sys, "class/block", name))
    for p in parts:
        pr = os.path.join(real, p)
        os.makedirs(os.path.join(pr, "holders"), exist_ok=True)
        link(pr, os.path.join(sys, "class/block", p))
    for child, holder in (holders or {}).items():
        base = os.path.join(sys, "class/block", child)
        os.symlink("../../" + holder, os.path.join(os.path.realpath(base), "holders", holder))


def virtual_block(sys, name, holders=(), dm_name=""):
    real = os.path.join(sys, "devices/virtual/block", name)
    os.makedirs(os.path.join(real, "holders"), exist_ok=True)
    if dm_name:
        w(os.path.join(real, "dm/name"), dm_name)
    for h in holders:
        os.symlink("../../" + h, os.path.join(real, "holders", h))
    link(real, os.path.join(sys, "class/block", name))


def build_usb(tmp_path):
    sys = str(tmp_path / "sys")
    usb_dev(sys, "0000:0e:00.0", "usb1", "1d6b", "0002", cls="09", ifaces=("09",), bus=1, dev=1)       # root hub
    usb_dev(sys, "0000:0e:00.0", "1-4", "05e3", "0610", cls="09", ifaces=("09",), bus=1, dev=5)        # a hub
    stick = usb_dev(sys, "0000:0e:00.0", "1-11", "090c", "1000", manufacturer="SMI", product_s="Flash Drive", bus=1, dev=3)
    raid = usb_dev(sys, "0000:0e:00.0", "1-12", "174c", "55aa", bus=1, dev=4)
    boot = usb_dev(sys, "0000:0e:00.0", "1-13", "0781", "5581", product_s="Ultra", bus=1, dev=6)
    kbd = usb_dev(sys, "0000:0e:00.0", "1-14", "046d", "c31c", ifaces=("03",), product_s="Keyboard", bus=1, dev=7)
    block(sys, os.path.join(stick, "1-11:1.0"), "sde", parts=("sde1",))
    block(sys, os.path.join(raid, "1-12:1.0"), "sdd", parts=("sdd1",))
    virtual_block(sys, "dm-1", dm_name="vg-nas")
    virtual_block(sys, "md0", holders=("dm-1",))
    os.symlink("../../md0", os.path.join(os.path.realpath(os.path.join(sys, "class/block/sdd1")), "holders", "md0"))
    block(sys, os.path.join(boot, "1-13:1.0"), "sdf", parts=("sdf1",))
    mi = tmp_path / "mountinfo"
    mi.write_text("22 1 0:30 / / rw - btrfs /dev/sdf1 rw\n"
                  "40 22 253:1 / /raid rw - xfs /dev/dm-1 rw\n"
                  "41 22 0:5 / /dev rw - devtmpfs devtmpfs rw\n")
    sw = tmp_path / "swaps"
    sw.write_text("Filename Type Size Used Priority\n")
    return sys, str(mi), str(sw), kbd


def test_usb_scan_filters_hubs_and_the_boot_disk_and_follows_holders(tmp_path):
    sys, mi, sw, _ = build_usb(tmp_path)
    ids = tmp_path / "usb.ids"
    ids.write_text("# comment\n046d  Logitech, Inc.\n\tc31c  Keyboard K120\n090c  Silicon Motion\n\t1000  Flash Drive\n")
    devs = usb.scan(sys, mi, sw, ids_paths=(str(ids),))
    by = {d.vendor + ":" + d.product: d for d in devs}
    assert "1d6b:0002" not in by                                   # root hub never scanned in
    assert by["05e3:0610"].hub is True
    assert by["0781:5581"].system is True                          # holds / (btrfs: the SOURCE, not 0:30)
    assert "raid" in by["174c:55aa"].busy and "vg-nas" in by["174c:55aa"].busy
    assert by["090c:1000"].busy == "" and by["090c:1000"].blocks == ["sde", "sde1"]
    assert by["090c:1000"].label == "SMI Flash Drive (090c:1000)"
    assert by["046d:c31c"].manufacturer == "Logitech, Inc."        # empty sysfs string → usb.ids
    assert by["046d:c31c"].cls == "03"                              # per-interface class read from the interface
    offered = [d.vendor + ":" + d.product for d in usb.offered(devs)]
    assert sorted(offered) == ["046d:c31c", "090c:1000", "174c:55aa"]


def test_usb_scan_without_a_usb_bus_raises(tmp_path):
    try:
        usb.scan(str(tmp_path / "nothing"))
        raise AssertionError("scanned nothing as an empty host")
    except FileNotFoundError:
        pass


def pci_dev(sys, addr, vendor, product, cls, driver="", group="", boot_vga=None, parent=""):
    real = os.path.join(sys, PCI_ROOT, *( [parent] if parent else []), addr)
    w(os.path.join(real, "vendor"), "0x" + vendor)
    w(os.path.join(real, "device"), "0x" + product)
    w(os.path.join(real, "class"), "0x" + cls)
    if boot_vga is not None:
        w(os.path.join(real, "boot_vga"), "1" if boot_vga else "0")
    if driver:
        os.makedirs(os.path.join(sys, "bus/pci/drivers", driver), exist_ok=True)
        os.symlink(os.path.join(sys, "bus/pci/drivers", driver), os.path.join(real, "driver"))
    if group:
        gd = os.path.join(sys, "kernel/iommu_groups", group, "devices")
        os.makedirs(gd, exist_ok=True)
        os.symlink(real, os.path.join(gd, addr))
        os.symlink(os.path.join(sys, "kernel/iommu_groups", group), os.path.join(real, "iommu_group"))
    link(real, os.path.join(sys, "bus/pci/devices", addr))
    return real


def build_pci(tmp_path, *, nvidia="nvidia"):
    sys = str(tmp_path / "sys")
    pci_dev(sys, "0000:00:00.0", "1022", "14d8", "060000")                                  # host bridge
    pci_dev(sys, "0000:00:01.1", "1022", "14db", "060400", "pcieport", "1")
    gpu = pci_dev(sys, "0000:01:00.0", "10de", "2504", "030000", nvidia, "12", boot_vga=False)
    pci_dev(sys, "0000:01:00.1", "10de", "228e", "040300", "snd_hda_intel", "12")
    nvme = pci_dev(sys, "0000:02:00.0", "15b7", "5017", "010802", "nvme", "13")
    pci_dev(sys, "0000:04:0a.0", "1022", "43f5", "060400", "pcieport", "22")
    nic = pci_dev(sys, "0000:0c:00.0", "10ec", "8125", "020000", "r8169", "22")
    pci_dev(sys, "0000:0e:00.0", "1022", "43f7", "0c0330", "xhci_hcd", "24")
    igpu = pci_dev(sys, "0000:10:00.0", "1002", "164e", "030000", "amdgpu", "26", boot_vga=True)
    # drm connectors, a net interface, the root disk
    for card, dev, status in (("card1", gpu, "disconnected"), ("card0", igpu, "connected")):
        cr = os.path.join(dev, "drm", card)
        os.makedirs(cr, exist_ok=True)
        link(cr, os.path.join(sys, "class/drm", card))
        con = os.path.join(cr, f"{card}-HDMI-A-1")
        w(os.path.join(con, "status"), status)
        link(con, os.path.join(sys, "class/drm", f"{card}-HDMI-A-1"))
    nr = os.path.join(nic, "net", "enp12s0")
    w(os.path.join(nr, "operstate"), "up")
    link(nr, os.path.join(sys, "class/net", "enp12s0"))
    br = os.path.join(nvme, "nvme/nvme0/nvme0n1")
    os.makedirs(os.path.join(br, "holders"), exist_ok=True)
    link(br, os.path.join(sys, "class/block", "nvme0n1"))
    pr = os.path.join(br, "nvme0n1p2")
    os.makedirs(os.path.join(pr, "holders"), exist_ok=True)
    link(pr, os.path.join(sys, "class/block", "nvme0n1p2"))
    virtual_block(sys, "dm-0", dm_name="luks-root")
    os.symlink("../../dm-0", os.path.join(os.path.realpath(pr), "holders", "dm-0"))
    mi = tmp_path / "mountinfo"
    mi.write_text("22 1 0:30 / / rw - btrfs /dev/dm-0 rw\n")
    sw = tmp_path / "swaps"
    sw.write_text("Filename Type Size Used Priority\n")
    ids = tmp_path / "pci.ids"
    ids.write_text("10de  NVIDIA Corporation\n\t2504  GA106 [GeForce RTX 3060 Lite Hash Rate]\n"
                   "\t228e  GA106 High Definition Audio Controller\n")
    return sys, str(mi), str(sw), (str(ids),)


def groups_of(devs):
    g = {}
    for d in devs:
        if d.group:
            g.setdefault(d.group, []).append(d.address)
    return g


def test_pci_scan_decides_hidden_system_and_busy(tmp_path):
    sys, mi, sw, ids = build_pci(tmp_path)
    devs = pci.scan(sys, mi, sw, ids_paths=ids)
    by = {d.address: d for d in devs}
    assert by["0000:00:00.0"].hidden and by["0000:00:01.1"].hidden and by["0000:04:0a.0"].hidden
    assert by["0000:02:00.0"].system is True                        # the root disk's controller, through LUKS
    assert "enp12s0" in by["0000:0c:00.0"].busy
    assert "boot display" in by["0000:10:00.0"].busy and "connected" in by["0000:10:00.0"].busy
    assert "nvidia" in by["0000:01:00.0"].busy
    assert by["0000:01:00.0"].label == "NVIDIA Corporation GA106 [GeForce RTX 3060 Lite Hash Rate] (10de:2504)"
    assert by["0000:0e:00.0"].busy == ""


def test_pci_plan_pairs_the_audio_and_judges_the_group(tmp_path):
    sys, mi, sw, ids = build_pci(tmp_path, nvidia="vfio-pci")
    devs = pci.scan(sys, mi, sw, ids_paths=ids)
    by = {d.address: d for d in devs}
    p = pci.plan(by["0000:01:00.0"], devs, groups_of(devs))
    assert [t.address for t in p["attach"]] == ["0000:01:00.0", "0000:01:00.1"]
    assert p["blockers"] == []                                       # snd_hda_intel goes WITH it, so not a blocker
    # the xhci shares nothing: fine; the NIC's group has a bridge only: its blocker is being UP, not the group
    assert pci.plan(by["0000:0e:00.0"], devs, groups_of(devs))["blockers"] == []
    nicp = pci.plan(by["0000:0c:00.0"], devs, groups_of(devs))
    assert len(nicp["blockers"]) == 1 and "enp12s0" in nicp["blockers"][0]


def test_pci_plan_refuses_a_gpu_on_a_host_driver_with_the_fix(tmp_path):
    sys, mi, sw, ids = build_pci(tmp_path)
    devs = pci.scan(sys, mi, sw, ids_paths=ids)
    by = {d.address: d for d in devs}
    p = pci.plan(by["0000:01:00.0"], devs, groups_of(devs))
    assert p["blockers"]
    ch = next(c for c in p["checks"] if c["id"] == "host-gpu")
    assert not ch["ok"] and "options vfio-pci ids=10de:2504,10de:228e" in ch["fix"]
    ig = pci.plan(by["0000:10:00.0"], devs, groups_of(devs))
    assert next(c for c in ig["checks"] if c["id"] == "host-gpu")["label"] == "This is the host's own display GPU"


def test_host_checks_measure_iommu_and_vfio_and_name_the_file_to_edit(tmp_path):
    sys = str(tmp_path / "sys")
    os.makedirs(os.path.join(sys, "kernel/iommu_groups"))
    cpu = tmp_path / "cpuinfo"
    cpu.write_text("vendor_id\t: GenuineIntel\n")
    etc = tmp_path / "etc"
    (etc / "kernel").mkdir(parents=True)
    (etc / "kernel" / "cmdline").write_text("quiet\n")
    mods = tmp_path / "mods"
    mods.mkdir()
    (mods / "modules.dep").write_text("kernel/drivers/net/foo.ko:\n")
    ch = {c["id"]: c for c in pci.host_checks(sys, str(mods), str(cpu), str(etc), "qemu:///system")}
    assert ch["iommu"]["ok"] is False and "intel_iommu=on" in ch["iommu"]["fix"] and "/etc/kernel/cmdline" in ch["iommu"]["fix"]
    assert ch["vfio"]["ok"] is False and "CONFIG_VFIO_PCI" in ch["vfio"]["fix"]
    assert ch["system"]["ok"] is True
    os.makedirs(os.path.join(sys, "kernel/iommu_groups/0"))
    (mods / "modules.dep").write_text("kernel/drivers/vfio/pci/vfio-pci.ko: kernel/drivers/vfio/vfio.ko\n")
    ch = {c["id"]: c for c in pci.host_checks(sys, str(mods), str(cpu), str(etc), "qemu:///session")}
    assert ch["iommu"]["ok"] and ch["vfio"]["ok"]
    assert ch["system"]["ok"] is False and "root" in ch["system"]["fix"]


def test_vm_checks_want_uefi_and_q35():
    good = "<domain><os firmware='efi'><type machine='pc-q35-10.2'>hvm</type></os></domain>"
    bad = "<domain><os><type machine='pc-i440fx-10.2'>hvm</type></os></domain>"
    assert all(c["ok"] for c in pci.vm_checks(good))
    assert not any(c["ok"] for c in pci.vm_checks(bad))


def test_an_apu_takes_its_hdmi_audio_but_not_the_boards_audio(tmp_path):
    """nas.lan, measured: the Raphael iGPU (10:00.0) has its HDMI audio at .1 (group 27) and the MOTHERBOARD's HD audio
    at .6 (group 31) in the same slot. The first goes with the GPU; the second is not the GPU's to give away."""
    sys = str(tmp_path / "sys")
    pci_dev(sys, "0000:10:00.0", "1002", "164e", "030000", "vfio-pci", "26", boot_vga=False)
    pci_dev(sys, "0000:10:00.1", "1002", "1640", "040300", "snd_hda_intel", "27")
    pci_dev(sys, "0000:10:00.6", "1022", "15e3", "040300", "snd_hda_intel", "31")
    devs = pci.scan(sys, str(tmp_path / "none"), str(tmp_path / "none"), ids_paths=())
    by = {d.address: d for d in devs}
    assert [t.address for t in pci.plan(by["0000:10:00.0"], devs, groups_of(devs))["attach"]] == \
        ["0000:10:00.0", "0000:10:00.1"]


def test_a_usb_disk_in_an_array_nothing_mounts_is_still_busy(tmp_path):
    """An md member whose array is assembled but not mounted (a rebuild, a degraded set being repaired) is in use by
    the host all the same: handing it to a VM pulls a disk out of a live array."""
    sys = str(tmp_path / "sys")
    disk = usb_dev(sys, "0000:0e:00.0", "1-2", "174c", "55aa", bus=1, dev=2)
    block(sys, os.path.join(disk, "1-2:1.0"), "sdx", parts=("sdx1",))
    virtual_block(sys, "md7")
    os.symlink("../../md7", os.path.join(os.path.realpath(os.path.join(sys, "class/block/sdx1")), "holders", "md7"))
    none = str(tmp_path / "none")
    d = usb.scan(sys, none, none, ids_paths=(none,))[0]
    assert d.busy == "the host is using sdx1 (part of md7)" and not d.system


# ---- the root disk, however the mount names it (review: /dev/root, multi-device btrfs, ZFS, swap) --------------
def _usb_disk(tmp_path, name="1-2", blk="sdx", part="sdx1", vendor="0781"):
    sys = str(tmp_path / "sys")
    d = usb_dev(sys, "0000:0e:00.0", name, vendor, "5581", bus=1, dev=int(name.split("-")[1]) + 1)
    block(sys, os.path.join(d, name + ":1.0"), blk, parts=(part,))
    return sys, d


def _majmin(sys, mm, name):
    link(os.path.realpath(os.path.join(sys, "class/block", name)), os.path.join(sys, "dev/block", mm))


def _one(sys, mi_text, tmp_path, **kw):
    mi = tmp_path / "mi"
    mi.write_text(mi_text)
    sw = kw.pop("swaps", "Filename Type Size Used Priority\n")
    (tmp_path / "sw").write_text(sw)
    none = str(tmp_path / "none")
    return usb.scan(sys, str(mi), str(tmp_path / "sw"), ids_paths=(none,), **kw)


def test_dev_root_is_traced_through_its_major_minor(tmp_path):
    sys, _ = _usb_disk(tmp_path)
    _majmin(sys, "8:17", "sdx1")
    d = _one(sys, "22 1 8:17 / / rw - ext4 /dev/root rw\n", tmp_path, zpool_status="")[0]
    assert d.system is True


def test_a_btrfs_root_with_an_anonymous_device_is_traced_through_stat(tmp_path):
    sys, _ = _usb_disk(tmp_path)
    _majmin(sys, "8:17", "sdx1")
    d = _one(sys, "22 1 0:30 / / rw - btrfs /dev/root rw\n", tmp_path, zpool_status="",
             root_dev=os.makedev(8, 17))[0]
    assert d.system is True


def test_every_member_of_a_multi_device_btrfs_root_is_the_root(tmp_path):
    sys, _ = _usb_disk(tmp_path)
    virtual_block(sys, "nvme0n1p2")
    for m in ("nvme0n1p2", "sdx1"):
        os.makedirs(os.path.join(sys, "fs/btrfs/1234-abcd/devices"), exist_ok=True)
        os.symlink("../../../../class/block/" + m, os.path.join(sys, "fs/btrfs/1234-abcd/devices", m))
    d = _one(sys, "22 1 0:30 /@ / rw - btrfs /dev/nvme0n1p2 rw\n", tmp_path, zpool_status="")[0]
    assert d.system is True, "the USB half of a two-disk btrfs root was offered"


def test_a_zfs_root_is_traced_through_zpool_status(tmp_path):
    sys, _ = _usb_disk(tmp_path)
    zs = "  pool: rpool\n state: ONLINE\nconfig:\n\n\tNAME        STATE\n\trpool       ONLINE\n\t  /dev/sdx1  ONLINE\n"
    d = _one(sys, "22 1 0:40 / / rw - zfs rpool/ROOT/gentoo rw\n", tmp_path, zpool_status=zs)[0]
    assert d.system is True


def test_a_root_nothing_can_trace_refuses_every_disk(tmp_path):
    sys, _ = _usb_disk(tmp_path)
    d = _one(sys, "22 1 0:40 / / rw - zfs rpool/ROOT/gentoo rw\n", tmp_path, zpool_status="")[0]
    assert "could not tell" in d.busy and not d.system
    # and a PCI controller with any disk behind it is not offered at all
    psys = str(tmp_path / "psys")
    ctl = pci_dev(psys, "0000:02:00.0", "15b7", "5017", "010802", "nvme", "13")
    br = os.path.join(ctl, "nvme/nvme0/nvme0n1")
    os.makedirs(os.path.join(br, "holders"))
    link(br, os.path.join(psys, "class/block", "nvme0n1"))
    (tmp_path / "sw").write_text("x\n")
    devs = pci.scan(psys, str(tmp_path / "mi"), str(tmp_path / "sw"), ids_paths=(), zpool_status="")
    assert devs[0].system is True


def test_swap_on_a_usb_disk_is_busy(tmp_path):
    sys, _ = _usb_disk(tmp_path)
    d = _one(sys, "", tmp_path, swaps="Filename Type Size Used Priority\n/dev/sdx1 partition 8G 0 -2\n",
             zpool_status="")[0]
    assert d.busy == "the host uses sdx1 as swap"


def test_a_usb_network_adapter_that_is_up_is_busy(tmp_path):
    sys = str(tmp_path / "sys")
    nicd = usb_dev(sys, "0000:0e:00.0", "4-1", "0bda", "8156", ifaces=("ff",), bus=4, dev=3)
    nr = os.path.join(nicd, "4-1:1.0", "net", "enp10s0f3u4")
    w(os.path.join(nr, "operstate"), "up")
    link(nr, os.path.join(sys, "class/net", "enp10s0f3u4"))
    d = _one(sys, "", tmp_path, zpool_status="")[0]
    assert "enp10s0f3u4" in d.busy


def test_a_usb_controller_carrying_the_hosts_keyboard_is_busy(tmp_path):
    sys = str(tmp_path / "sys")
    pci_dev(sys, "0000:11:00.0", "1022", "15b8", "0c0330", "xhci_hcd", "32")
    usb_dev(sys, "0000:11:00.0", "3-1", "046d", "c31c", ifaces=("03",), bus=3, dev=2)
    (tmp_path / "e").write_text("")
    d = next(x for x in pci.scan(sys, str(tmp_path / "e"), str(tmp_path / "e"), ids_paths=()) if x.address == "0000:11:00.0")
    assert "HID devices" in d.busy


# ---- review round 2: ZFS with nothing mounted, IFF_UP, bcachefs, no mount table -----------------------------
ZS = "  pool: tank\n state: ONLINE\nconfig:\n\n\tNAME        STATE\n\ttank        ONLINE\n\t  /dev/sdx1  ONLINE\n"


def test_a_zfs_pool_with_nothing_mounted_is_still_in_use(tmp_path):
    sys, _ = _usb_disk(tmp_path)
    d = _one(sys, "22 1 8:1 / / rw - ext4 /dev/nvme0n1p2 rw\n", tmp_path, zpool_status=ZS)[0]
    assert d.busy == "sdx1 is a member of ZFS pool tank on the host", d.busy


def test_an_interface_that_is_up_but_reports_unknown_is_in_use(tmp_path):
    """operstate is "unknown" for plenty of interfaces that are up (tun, some USB NICs); IFF_UP is the fact."""
    sys = str(tmp_path / "sys")
    nicd = usb_dev(sys, "0000:0e:00.0", "4-1", "0bda", "8156", ifaces=("ff",), bus=4, dev=3)
    nr = os.path.join(nicd, "4-1:1.0", "net", "usb0")
    w(os.path.join(nr, "operstate"), "unknown")
    w(os.path.join(nr, "flags"), "0x1003")
    link(nr, os.path.join(sys, "class/net", "usb0"))
    assert "usb0" in _one(sys, "", tmp_path, zpool_status="")[0].busy


def test_every_member_of_a_multi_device_bcachefs_root_is_the_root(tmp_path):
    sys, _ = _usb_disk(tmp_path)
    virtual_block(sys, "nvme0n1p2")
    # the source names both members
    d = _one(sys, "22 1 0:31 / / rw - bcachefs /dev/nvme0n1p2:/dev/sdx1 rw\n", tmp_path, zpool_status="")[0]
    assert d.system is True
    # and /sys/fs/bcachefs/<uuid>/dev-N/block names them when the source is only one of them
    for i, m in enumerate(("nvme0n1p2", "sdx1")):
        os.makedirs(os.path.join(sys, f"fs/bcachefs/abcd/dev-{i}"), exist_ok=True)
        os.symlink(os.path.realpath(os.path.join(sys, "class/block", m)), os.path.join(sys, f"fs/bcachefs/abcd/dev-{i}/block"))
    d = _one(sys, "22 1 0:31 / / rw - bcachefs /dev/nvme0n1p2 rw\n", tmp_path, zpool_status="")[0]
    assert d.system is True


def test_an_unreadable_mount_table_refuses_every_disk(tmp_path):
    sys, _ = _usb_disk(tmp_path)
    none = str(tmp_path / "no-such-mountinfo")
    (tmp_path / "sw").write_text("x\n")
    d = usb.scan(sys, none, str(tmp_path / "sw"), ids_paths=(none,), zpool_status="")[0]
    assert "could not tell" in d.busy
