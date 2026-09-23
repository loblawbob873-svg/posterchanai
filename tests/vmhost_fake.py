"""An in-memory hypervisor for the VM-host tests.

Speaks the same Protocol as `VirshBackend`, keeps domains in a dict, writes disks as real (tiny)
files under the test's storage root so path confinement and deletion are exercised against a real
filesystem, and supports failure injection and a gate for making an operation slow (the busy-lock
tests). Metadata goes through `domainxml.VmMeta.to_xml` / `parse_meta` exactly as libvirt would store
and return it, so a round trip through the fake is a round trip through the real serialisation.
"""
from __future__ import annotations

import copy
import json
import os
import re
import xml.etree.ElementTree as ET

from app.services.vmhost import domainxml
from app.services.vmhost.backend import BackendError, DomainInfo


class FakeBackend:
    def __init__(self, *, cores=8, ram_total_mib=32768, ram_free_mib=30000, disk_total_gib=500,
                 disk_free_gib=400):
        self.stats = {"cores": cores, "load1": 0.5, "ram_total_mib": ram_total_mib,
                      "ram_free_mib": ram_free_mib, "disk_total_gib": disk_total_gib,
                      "disk_free_gib": disk_free_gib}
        self.domains: dict = {}          # uuid -> dict(name, state, vcpus, ram_mib, autostart, meta_xml, xml)
        self.calls: list = []
        self.fail: dict = {}             # method name -> BackendError to raise once
        self.gate: dict = {}             # method name -> asyncio.Event the call waits on
        self.vnc: dict = {}              # uuid -> (host, port)
        self.passwords: dict = {}
        # uuid -> [ {name, created, state, current, xml} ]: LIBVIRT snapshot metadata (made by hand with virsh, or
        # redefined by a migration). The app's own snapshots are offline qemu-img tags (img_snapshot_*), not these.
        self.snapshots: dict = {}
        self.img_data: dict = {}         # path -> [(tag, bytes at that snapshot)] — what `qemu-img snapshot -a` restores
        self.networks: list = ["default"]  # `virsh net-list --all --name`
        self.bridges: list = []            # Linux bridges in /sys/class/net
        self.addrs: dict = {}              # uuid -> [ip] (`domifaddr`)
        self.ups: dict = {}                # uuid -> seconds running (/proc)
        # host devices (devices.py): what sysfs would say, and what a running domain holds LIVE (hot-plugged
        # hostdevs, which libvirt reports with the resolved bus/device) on top of its saved definition.
        self.usb_devices: list = []        # [usb.UsbDevice]
        self.pci_devices: list = []        # [pci.PciDevice]
        self.device_checks_by_kind: dict = {
            "usb": [{"id": "qemu-usb", "ok": True, "label": "QEMU supports USB passthrough (usb-host)", "fix": ""}],
            "pci": [{"id": "iommu", "ok": True, "label": "IOMMU is on", "fix": ""},
                    {"id": "vfio", "ok": True, "label": "vfio-pci is available", "fix": ""},
                    {"id": "system", "ok": True, "label": "qemu:///system", "fix": ""},
                    {"id": "qemu-pci", "ok": True, "label": "QEMU supports PCI passthrough (vfio-pci)", "fix": ""}]}
        self.live_hostdevs: dict = {}      # uuid -> [hostdev xml]
        self.drop_attach = False           # accept attach-device and keep nothing (the read-back must catch it)
        self.drop_detach = False
        self.drop_attach_live = False      # keep the --config half of an attach, lose the --live half
        self.dumpxml_fail: set = set()     # uuids whose definition cannot be read

    async def _enter(self, name, *args):
        self.calls.append((name,) + args)
        ev = self.gate.get(name)
        if ev is not None:
            await ev.wait()
        err = self.fail.pop(name, None)
        if err is not None:
            raise err

    # ---- helpers for tests
    def add_domain(self, uuid, name, state="shutoff", vcpus=2, ram_mib=2048, meta=None, firmware="efi"):
        xml = domainxml.build_domain_xml(domainxml.DomainSpec(
            name=name, uuid=uuid, guest=(meta.guest if meta else "linux"), firmware=firmware, vcpus=vcpus,
            ram_mib=ram_mib, disk_path=f"/fake/{uuid}/disk-vda.qcow2", nvram_path=f"/fake/{uuid}/nvram.fd"))
        self.domains[uuid] = {"name": name, "state": state, "vcpus": vcpus, "ram_mib": ram_mib,
                              "autostart": False, "meta_xml": meta.to_xml(prefixed=False) if meta else None,
                              "xml": xml}

    def _info(self, u) -> DomainInfo:
        d = self.domains[u]
        return DomainInfo(uuid=u, name=d["name"], state=d["state"], vcpus=d["vcpus"], ram_mib=d["ram_mib"],
                          autostart=d["autostart"], meta=domainxml.parse_meta(d["meta_xml"]), disks=[],
                          nics=self._nics(d["xml"]))

    @staticmethod
    def _nics(xml) -> list:
        """What `domiflist --inactive` would say about this definition."""
        try:
            dev = ET.fromstring(xml).find("devices")
        except ET.ParseError:
            return []
        out = []
        for i in (dev.findall("interface") if dev is not None else []):
            n = domainxml.nic_of(i)
            m, mac = i.find("model"), i.find("mac")
            out.append({"type": n["type"], "source": n["name"], "model": m.get("type", "") if m is not None else "",
                        "mac": mac.get("address", "") if mac is not None else ""})
        return out

    async def list_networks(self):
        await self._enter("list_networks")
        return list(self.networks)

    async def list_bridges(self):
        await self._enter("list_bridges")
        return list(self.bridges)

    async def guest_addresses(self, vm_uuid):
        await self._enter("guest_addresses", vm_uuid)
        return list(self.addrs.get(vm_uuid, []))

    async def uptimes(self):
        await self._enter("uptimes")
        return dict(self.ups)

    # ---- Protocol
    async def available(self):
        await self._enter("available")
        return {"ok": True, "kvm": True, "libvirt": "fake 1.0", "uri": "test:///", "error": ""}

    async def host_stats(self, storage_root):
        await self._enter("host_stats", storage_root)
        return dict(self.stats)

    async def list_domains(self):
        await self._enter("list_domains")
        return [self._info(u) for u in list(self.domains)]

    async def get(self, vm_uuid):
        await self._enter("get", vm_uuid)
        return self._info(vm_uuid) if vm_uuid in self.domains else None

    async def define(self, xml, workdir):
        await self._enter("define", workdir)
        u = re.search(r"<uuid>([^<]+)</uuid>", xml).group(1)
        name = re.search(r"<name>([^<]+)</name>", xml).group(1)
        vcpus = int(re.search(r"<vcpu>(\d+)</vcpu>", xml).group(1))
        ram = int(re.search(r'<memory unit="MiB">(\d+)</memory>', xml).group(1))
        meta = domainxml.parse_meta(xml)
        # LIBVIRT GIVES EVERY ADAPTER A MAC ON DEFINE (52:54:00:… for QEMU) and dumpxml reports it from then
        # on. Without this the fake held adapters no real host has, and anything that names an adapter by
        # its MAC -- remove_nic, the add_nic read-back -- had nothing to name.
        if "<interface" in xml:
            root = ET.fromstring(xml)
            changed = False
            for iface in root.findall("devices/interface"):
                if iface.find("mac") is None:
                    self._mac_seq = getattr(self, "_mac_seq", 0) + 1
                    n = self._mac_seq
                    ET.SubElement(iface, "mac", {"address": "52:54:00:%02x:%02x:%02x" % ((n >> 16) & 255, (n >> 8) & 255, n & 255)})
                    changed = True
            if changed:
                xml = ET.tostring(root, encoding="unicode")
        with open(os.path.join(workdir, "domain.xml"), "w") as f:
            f.write(xml)
        prev = self.domains.get(u) or {}
        # A redefine of an existing domain keeps its run state and autostart, as libvirt does.
        self.domains[u] = {"name": name, "state": prev.get("state", "shutoff"), "vcpus": vcpus, "ram_mib": ram,
                           "autostart": prev.get("autostart", False),
                           "meta_xml": meta.to_xml(prefixed=False) if meta else None, "xml": xml}

    async def undefine(self, vm_uuid, keep_nvram=False):
        await self._enter("undefine", vm_uuid, keep_nvram)
        if vm_uuid not in self.domains:
            raise BackendError("domain not found")
        del self.domains[vm_uuid]

    async def start(self, vm_uuid):
        await self._enter("start", vm_uuid)
        self.domains[vm_uuid]["state"] = "running"
        root = ET.fromstring(self.domains[vm_uuid]["xml"])
        self.live_hostdevs[vm_uuid] = [self._resolve(ET.tostring(h, encoding="unicode"))
                                       for h in root.findall("devices/hostdev")]

    async def shutdown(self, vm_uuid):
        await self._enter("shutdown", vm_uuid)
        self.domains[vm_uuid]["state"] = "shutoff"
        self.live_hostdevs.pop(vm_uuid, None)

    async def destroy(self, vm_uuid):
        await self._enter("destroy", vm_uuid)
        self.domains[vm_uuid]["state"] = "shutoff"
        self.live_hostdevs.pop(vm_uuid, None)

    async def reboot(self, vm_uuid):
        await self._enter("reboot", vm_uuid)

    async def set_autostart(self, vm_uuid, on):
        await self._enter("set_autostart", vm_uuid, on)
        self.domains[vm_uuid]["autostart"] = bool(on)

    async def set_metadata(self, vm_uuid, meta, live):
        await self._enter("set_metadata", vm_uuid, live)
        self.domains[vm_uuid]["meta_xml"] = copy.deepcopy(meta).to_xml(prefixed=False)

    async def vnc_endpoint(self, vm_uuid):
        await self._enter("vnc_endpoint", vm_uuid)
        return self.vnc.get(vm_uuid, ("127.0.0.1", 5900))

    async def set_vnc_password(self, vm_uuid, password, expire_s):
        await self._enter("set_vnc_password", vm_uuid, expire_s)
        self.passwords[vm_uuid] = password

    async def img_create(self, path, size_gib):
        await self._enter("img_create", path, size_gib)
        with open(path, "wb") as f:
            f.write(b"QFI\xfb")

    # ---- phase 2
    def _reported_xml(self, vm_uuid) -> str:
        """The definition as `virsh dumpxml` reports it: the CURRENT metadata spliced in, and — like libvirt
        without `--security-info` — NO VNC `passwd`. Anything that defines this back must restore one."""
        d = self.domains[vm_uuid]
        xml = d["xml"]
        xml = re.sub(r"<metadata>.*?</metadata>", "", xml, flags=re.S)
        meta = domainxml.parse_meta(d["meta_xml"]) if d["meta_xml"] else None
        if meta:
            xml = xml.replace("</uuid>", "</uuid><metadata>" + meta.to_xml(prefixed=True) + "</metadata>", 1)
        return re.sub(r'\s(?:passwd|passwdValidTo)="[^"]*"', "", xml)

    async def dumpxml(self, vm_uuid, inactive=True):
        await self._enter("dumpxml", vm_uuid)
        if vm_uuid in self.dumpxml_fail:
            raise BackendError("internal error: cannot read the definition")
        if vm_uuid not in self.domains:
            raise BackendError("domain not found")
        xml = self._reported_xml(vm_uuid)
        if not inactive and self.domains[vm_uuid]["state"] in ("running", "paused"):
            xml = re.sub(r"<hostdev\b.*?</hostdev>", "", xml, flags=re.S)
            xml = xml.replace("</devices>", "".join(self.live_hostdevs.get(vm_uuid, [])) + "</devices>", 1)
        return xml

    # ---- host devices
    async def host_devices(self, kind):
        await self._enter("host_devices", kind)
        return list(self.usb_devices if kind == "usb" else self.pci_devices)

    async def device_checks(self, kind):
        await self._enter("device_checks", kind)
        return [dict(c) for c in self.device_checks_by_kind.get(kind, [])]

    def _resolve(self, hxml: str) -> str:
        """A hostdev as a RUNNING domain reports it: a USB one carries the bus/device libvirt resolved."""
        h = ET.fromstring(hxml)
        if h.get("type") == "usb":
            src = h.find("source")
            v, p = src.find("vendor"), src.find("product")
            if src.find("address") is None and v is not None:
                vid, pid = v.get("id")[2:], p.get("id")[2:]
                dev = next((d for d in self.usb_devices if d.vendor == vid and d.product == pid), None)
                if dev is None:
                    raise BackendError(f"Did not find USB device {vid}:{pid}")
                ET.SubElement(src, "address", {"bus": str(dev.bus), "device": str(dev.device)})
        return ET.tostring(h, encoding="unicode")

    @staticmethod
    def _same(a: str, b: str) -> bool:
        from app.services.vmhost import pci, usb
        ua, ub = usb.hostdevs(f"<domain><devices>{a}</devices></domain>"), usb.hostdevs(f"<domain><devices>{b}</devices></domain>")
        if ua and ub:
            x, y = ua[0], ub[0]
            if x["bus"] is not None and y["bus"] is not None:
                return (x["bus"], x["device"]) == (y["bus"], y["device"])
            return (x["vendor"], x["product"]) == (y["vendor"], y["product"])
        pa, pb = pci.hostdevs(f"<domain><devices>{a}</devices></domain>"), pci.hostdevs(f"<domain><devices>{b}</devices></domain>")
        return bool(pa and pb and pa[0] == pb[0])

    async def attach_device(self, vm_uuid, xml_path, *, live, config):
        await self._enter("attach_device", vm_uuid, live, config)
        with open(xml_path) as f:
            hxml = f.read()
        d = self.domains[vm_uuid]
        if live and d["state"] not in ("running", "paused"):
            raise BackendError("Requested operation is not valid: domain is not running")
        for other_uuid, others in self.live_hostdevs.items():
            if other_uuid != vm_uuid and any(self._same(o, hxml) for o in others):
                raise BackendError(f"USB device is in use by driver QEMU, domain {self.domains[other_uuid]['name']}")
        if self.drop_attach:
            return
        if live and not self.drop_attach_live:
            self.live_hostdevs.setdefault(vm_uuid, []).append(self._resolve(hxml))
        if config:
            d["xml"] = d["xml"].replace("</devices>", hxml + "</devices>", 1)

    async def detach_device(self, vm_uuid, xml_path, *, live, config):
        await self._enter("detach_device", vm_uuid, live, config)
        with open(xml_path) as f:
            hxml = f.read()
        d = self.domains[vm_uuid]
        if self.drop_detach:
            return
        if live:
            cur = self.live_hostdevs.get(vm_uuid, [])
            if not any(self._same(o, hxml) for o in cur):
                raise BackendError("device not found: host usb device not found")
            self.live_hostdevs[vm_uuid] = [o for o in cur if not self._same(o, hxml)]
        if config:
            found = [m.group(0) for m in re.finditer(r"<hostdev\b.*?</hostdev>", d["xml"], flags=re.S)
                     if self._same(m.group(0), hxml)]
            if not found:
                raise BackendError("device not found: host device not found in the configuration")
            for f_ in found:
                d["xml"] = d["xml"].replace(f_, "", 1)

    # ---- offline snapshots: qemu-img tags live INSIDE the (fake) image, so they travel with its bytes
    TAG_AT, TAG_LEN = 16, 1024

    def _tags(self, path) -> list:
        with open(path, "rb") as f:
            f.seek(self.TAG_AT)
            blob = f.read(self.TAG_LEN)
        if not blob.startswith(b"PCSNAP"):
            return []
        return json.loads(blob[6:].rstrip(b"\0").decode())

    def _write_tags(self, path, tags) -> None:
        blob = b"PCSNAP" + json.dumps(tags).encode()
        assert len(blob) <= self.TAG_LEN
        size = os.path.getsize(path)
        with open(path, "r+b") as f:
            if size < self.TAG_AT + self.TAG_LEN:
                f.truncate(self.TAG_AT + self.TAG_LEN)
            f.seek(self.TAG_AT)
            f.write(blob.ljust(self.TAG_LEN, b"\0"))

    def _require_qcow2(self, path):
        with open(path, "rb") as f:
            if f.read(4) != b"QFI\xfb":
                raise BackendError(f"qemu-img: Could not open '{path}': not a qcow2 image")

    async def img_snapshot_create(self, path, name):
        await self._enter("img_snapshot_create", path, name)
        self._require_qcow2(path)
        tags = self._tags(path)
        tags.append(name)                        # like qemu-img: a repeated tag is a SECOND snapshot, not an error
        self._write_tags(path, tags)
        with open(path, "rb") as f:
            data = f.read()
        self.img_data.setdefault(path, []).append((name, data[:self.TAG_AT] + data[self.TAG_AT + self.TAG_LEN:]))

    async def img_snapshot_apply(self, path, name):
        await self._enter("img_snapshot_apply", path, name)
        if name not in self._tags(path):
            raise BackendError(f"qemu-img: Could not apply snapshot '{name}': snapshot not found")
        saved = next((d for (n, d) in self.img_data.get(path, []) if n == name), None)
        if saved is not None:
            tags = self._tags(path)
            with open(path, "wb") as f:
                f.write(saved[:self.TAG_AT] + b"\0" * self.TAG_LEN + saved[self.TAG_AT:])
            self._write_tags(path, tags)

    async def img_snapshot_delete(self, path, name):
        await self._enter("img_snapshot_delete", path, name)
        tags = self._tags(path)
        if name not in tags:
            raise BackendError(f"qemu-img: Could not delete snapshot '{name}': snapshot not found")
        tags.remove(name)
        self._write_tags(path, tags)

    # ---- phase 3 (cold-migration primitives; tests/vmhost_migration_fake.py adds hooks on top)
    async def dumpxml_inactive(self, vm_uuid):
        await self._enter("dumpxml_inactive", vm_uuid)
        if vm_uuid not in self.domains:
            raise BackendError("domain not found")
        return self._reported_xml(vm_uuid)

    async def snapshot_names(self, vm_uuid):
        await self._enter("snapshot_names", vm_uuid)
        snaps = self.snapshots.get(vm_uuid, [])
        return [s["name"] for s in snaps], next((s["name"] for s in snaps if s.get("current")), "")

    async def snapshot_dumpxml(self, vm_uuid, name):
        await self._enter("snapshot_dumpxml", vm_uuid, name)
        return next(s["xml"] for s in self.snapshots[vm_uuid] if s["name"] == name)

    async def snapshot_redefine(self, vm_uuid, xml, workdir, current=False):
        await self._enter("snapshot_redefine", vm_uuid, current)
        if vm_uuid not in self.domains:
            raise BackendError("domain not found")
        el = ET.fromstring(xml)
        name = el.findtext("name")
        snaps = [s for s in self.snapshots.get(vm_uuid, []) if s["name"] != name]
        if current:
            for s in snaps:
                s["current"] = False
        snaps.append({"name": name, "xml": xml, "current": bool(current), "created": "",
                      "state": el.findtext("state") or "shutoff"})
        self.snapshots[vm_uuid] = snaps

    async def undefine_for_migration(self, vm_uuid, keep_nvram):
        await self._enter("undefine_for_migration", vm_uuid, keep_nvram)
        self.domains.pop(vm_uuid, None)
        self.snapshots.pop(vm_uuid, None)

    async def img_info(self, path):
        """What qemu-img would say about a (fake) image: the qcow2 magic and backing-file offset of the header."""
        await self._enter("img_info", path)
        with open(path, "rb") as f:
            hdr = f.read(16)
        if hdr[:4] == b"QFI\xfb":
            return {"format": "qcow2", "backing": "backing" if int.from_bytes(hdr[8:16], "big") else "",
                    "data_file": "", "virtual_size": 0, "snapshots": self._tags(path)}
        return {"format": "raw", "backing": "", "data_file": "", "virtual_size": 0, "snapshots": []}
