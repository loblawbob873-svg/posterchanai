"""An in-memory hypervisor for the VM-host tests.

Speaks the same Protocol as `VirshBackend`, keeps domains in a dict, writes disks as real (tiny)
files under the test's storage root so path confinement and deletion are exercised against a real
filesystem, and supports failure injection and a gate for making an operation slow (the busy-lock
tests). Metadata goes through `domainxml.VmMeta.to_xml` / `parse_meta` exactly as libvirt would store
and return it, so a round trip through the fake is a round trip through the real serialisation.
"""
from __future__ import annotations

import copy
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
        # uuid -> [ {name, created, state, current, xml, vm?} ]. ONE shape for both phases: phase 2's ops
        # (list/create/revert/delete) and phase 3's migration primitives (names/dumpxml/redefine) read the
        # same records, so a snapshot created by vm.snapshot.create is one a migration can carry.
        self.snapshots: dict = {}

    async def _enter(self, name, *args):
        self.calls.append((name,) + args)
        ev = self.gate.get(name)
        if ev is not None:
            await ev.wait()
        err = self.fail.pop(name, None)
        if err is not None:
            raise err

    # ---- helpers for tests
    def add_domain(self, uuid, name, state="shutoff", vcpus=2, ram_mib=2048, meta=None):
        xml = domainxml.build_domain_xml(domainxml.DomainSpec(
            name=name, uuid=uuid, guest=(meta.guest if meta else "linux"), firmware="efi", vcpus=vcpus,
            ram_mib=ram_mib, disk_path=f"/fake/{uuid}/disk-vda.qcow2", nvram_path=f"/fake/{uuid}/nvram.fd"))
        self.domains[uuid] = {"name": name, "state": state, "vcpus": vcpus, "ram_mib": ram_mib,
                              "autostart": False, "meta_xml": meta.to_xml(prefixed=False) if meta else None,
                              "xml": xml}

    def _info(self, u) -> DomainInfo:
        d = self.domains[u]
        return DomainInfo(uuid=u, name=d["name"], state=d["state"], vcpus=d["vcpus"], ram_mib=d["ram_mib"],
                          autostart=d["autostart"], meta=domainxml.parse_meta(d["meta_xml"]), disks=[])

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

    async def shutdown(self, vm_uuid):
        await self._enter("shutdown", vm_uuid)
        self.domains[vm_uuid]["state"] = "shutoff"

    async def destroy(self, vm_uuid):
        await self._enter("destroy", vm_uuid)
        self.domains[vm_uuid]["state"] = "shutoff"

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
        if vm_uuid not in self.domains:
            raise BackendError("domain not found")
        return self._reported_xml(vm_uuid)

    async def snapshot_list(self, vm_uuid):
        await self._enter("snapshot_list", vm_uuid)
        return [{"name": s["name"], "created": s.get("created", ""), "state": s.get("state", "shutoff")}
                for s in self.snapshots.get(vm_uuid, [])]

    async def snapshot_create(self, vm_uuid, name, description=""):
        await self._enter("snapshot_create", vm_uuid, name)
        lst = self.snapshots.setdefault(vm_uuid, [])
        if any(s["name"] == name for s in lst):
            raise BackendError("snapshot already exists")
        parent = next((s["name"] for s in lst if s.get("current")), "")
        for s in lst:
            s["current"] = False
        state = self.domains[vm_uuid]["state"]
        xml = (f"<domainsnapshot><name>{name}</name>" + (f"<parent><name>{parent}</name></parent>" if parent else "")
               + f"<state>{state}</state><disks><disk name='vda' snapshot='internal'/></disks>"
               + self._reported_xml(vm_uuid) + "</domainsnapshot>")
        lst.append({"name": name, "created": "2026-09-16 12:00:00 +0000", "state": state, "current": True,
                    "xml": xml, "vm": copy.deepcopy(self.domains[vm_uuid])})

    async def snapshot_revert(self, vm_uuid, name):
        await self._enter("snapshot_revert", vm_uuid, name)
        s = next((x for x in self.snapshots.get(vm_uuid, []) if x["name"] == name), None)
        if s is None:
            raise BackendError("snapshot not found")
        if s.get("vm") is not None:
            self.domains[vm_uuid] = copy.deepcopy(s["vm"])
        else:                                    # a REDEFINED snapshot: the definition travels in its XML
            dom = ET.fromstring(s["xml"]).find("domain")
            self.domains[vm_uuid]["xml"] = ET.tostring(dom, encoding="unicode")
            self.domains[vm_uuid]["state"] = s.get("state", "shutoff")

    async def snapshot_delete(self, vm_uuid, name):
        await self._enter("snapshot_delete", vm_uuid, name)
        lst = self.snapshots.get(vm_uuid, [])
        if not any(x["name"] == name for x in lst):
            raise BackendError("snapshot not found")
        self.snapshots[vm_uuid] = [x for x in lst if x["name"] != name]

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
