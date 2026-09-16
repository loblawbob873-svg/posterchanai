"""Phase 2 host ops that change a VM's hardware or its disk history: `vm.update` and `vm.snapshot.*`.

A mixin of `VmHostService` (service.py) — kept in its own module so the op table there only grows by
rows. Every op here is ADMIN-only, enforced by that table on the host, never by the client.

vm.update — the machine must be SHUT OFF, and that is checked under the VM lock, after a re-read. Every
field is validated and capacity-checked BEFORE anything is written; then the inactive definition is
edited as a tree (domainxml), defined back once, and read back: a Save that libvirt accepted but that
did not change the fields asked for is reported as an error, not as success. A new disk file is created
first and removed again if the define fails, so a refused Save never leaves an orphan qcow2 behind.

Snapshots are libvirt internal snapshots (`snapshot-create-as --atomic`). A revert throws away every
change since the snapshot, so it demands `confirm: true`; a VM that is being migrated (phase 3 sets
`migration` on its metadata) refuses all four ops, because a snapshot taken mid-export is a history the
destination never receives.
"""
from __future__ import annotations

import asyncio
import logging

from . import domainxml
from .backend import SNAPSHOT_NAME
from .storage import PathEscape

logger = logging.getLogger(__name__)

MAX_NICS = 8
MAX_SNAPSHOTS = 32


def _err(code, msg):
    from .service import VmHostError
    return VmHostError(code, msg)


def is_migrating(d) -> bool:
    """Phase 3 records an in-flight migration on the VM's metadata. Read defensively so this module does
    not depend on that field existing yet."""
    m = getattr(d, "meta", None)
    return bool(m is not None and getattr(m, "migration", None))


class HardwareOps:
    # ------------------------------------------------------------------------------ vm.update
    async def _op_vm_update(self, pk, role, args, progress):
        from .service import _int_arg
        d = await self._domain(pk, role, args)
        if d.meta is None or not self.storage.is_managed_dir(d.uuid):
            raise _err("unsupported", "this VM was not created by PosterChan — edit it with virsh")
        known = {"vm", "vcpus", "ram_mib", "autostart", "boot", "add_disk_gib", "add_nic", "media", "input"}
        extra = sorted(set(args) - known)
        if extra:
            raise _err("bad_request", f"unknown field {extra[0]!r}")
        want = {k: args[k] for k in known - {"vm"} if k in args and args[k] is not None}
        if not want:
            raise _err("bad_request", "nothing to change")
        vcpus = _int_arg(want, "vcpus", 1, self.cfg.max_vcpus) if "vcpus" in want else None
        ram = _int_arg(want, "ram_mib", 256, self.cfg.max_ram_mib) if "ram_mib" in want else None
        add_gib = _int_arg(want, "add_disk_gib", 1, self.cfg.max_disk_gib) if "add_disk_gib" in want else None
        if "autostart" in want and not isinstance(want["autostart"], bool):
            raise _err("bad_request", "autostart must be true or false")
        if "boot" in want and want["boot"] not in ("disk", "cdrom"):
            raise _err("bad_request", "boot must be disk or cdrom")
        if "input" in want and want["input"] not in ("tablet", "mouse"):
            raise _err("bad_request", "input must be tablet or mouse")
        if "add_nic" in want and want["add_nic"] is not True:
            raise _err("bad_request", "add_nic must be true")
        media = want.get("media")
        iso_path = None
        if "media" in want:
            if media == "eject":
                pass
            elif isinstance(media, dict) and isinstance(media.get("iso"), str):
                try:
                    iso_path = str(await asyncio.to_thread(self.storage.iso_path, media["iso"]))
                except PathEscape:
                    raise _err("bad_request", "that is not an ISO from this host's library")
                except FileNotFoundError:
                    raise _err("not_found", "no such ISO in this host's library")
            else:
                raise _err("bad_request", 'media must be {"iso": "<id>"} or "eject"')

        await self._acquire(self._host_lock, "this host")
        try:
            lock = self._vm_lock(d.uuid)
            await self._acquire(lock, "this VM")
            new_disk = None
            try:
                d = await self.backend.get(d.uuid)
                if d is None:
                    raise _err("not_found", "no such VM")
                if is_migrating(d):
                    raise _err("migrating", "this VM is being migrated")
                if d.state != "shutoff":
                    raise _err("conflict", "shut the VM down before changing its hardware")
                domains = await self.backend.list_domains()
                st = await self.backend.host_stats(str(self.storage.root))
                cores = int(st.get("cores") or 0)
                if vcpus is not None and cores and vcpus > cores and not self.cfg.allow_overcommit:
                    raise _err("insufficient_capacity", f"this host has {cores} CPU cores")
                if ram is not None and ram > d.ram_mib and not self.cfg.allow_overcommit:
                    others = sum(x.ram_mib for x in domains if x.uuid != d.uuid)
                    room = int(st.get("ram_total_mib") or 0) - self.cfg.reserve_ram_mib - others
                    if ram > room:
                        raise _err("insufficient_capacity",
                                   f"only {max(0, room)} MiB of memory is uncommitted on this host")
                if add_gib is not None:
                    room = int(st.get("disk_free_gib") or 0) - self.cfg.reserve_disk_gib
                    if add_gib > room:
                        raise _err("insufficient_capacity", f"only {max(0, room)} GiB of disk is free on this host")
                    if d.meta.disk_gib + add_gib > self.cfg.max_disk_gib:
                        raise _err("insufficient_capacity",
                                   f"a VM may have at most {self.cfg.max_disk_gib} GiB of disk on this host")

                try:
                    root = domainxml.parse_domain(await self.backend.dumpxml(d.uuid, inactive=True))
                    windows = d.meta.guest == "windows"
                    if vcpus is not None or ram is not None:
                        domainxml.set_vcpus_ram(root, vcpus, ram)
                    if "boot" in want:
                        domainxml.set_boot(root, want["boot"])
                    if "input" in want:
                        domainxml.set_input(root, want["input"])
                    if want.get("add_nic"):
                        if len(root.findall("devices/interface")) >= MAX_NICS:
                            raise _err("insufficient_capacity", f"a VM may have at most {MAX_NICS} network adapters")
                        domainxml.add_nic(root, self.cfg.default_network, self.cfg.bridge, windows)
                    if "media" in want:
                        domainxml.set_media(root, iso_path)
                    meta = d.meta
                    if "media" in want:
                        meta.iso = media["iso"] if isinstance(media, dict) else ""
                    if add_gib is not None:
                        target = domainxml.next_disk_target(root, windows)
                        new_disk = self.storage.extra_disk_path(d.uuid, target)
                        if await asyncio.to_thread(new_disk.exists):
                            new_disk = None
                            raise _err("conflict", f"a disk file for {target} already exists on this host")
                        if progress:
                            await progress({"phase": "disk", "msg": f"creating a {add_gib} GiB disk"})
                        await self.backend.img_create(str(new_disk), add_gib)
                        domainxml.add_disk(root, str(new_disk), target)
                        meta.disk_gib = int(meta.disk_gib) + add_gib
                    domainxml.set_meta(root, meta)
                    domainxml.secure_vnc(root)       # dumpxml omits passwd; never define a display without one
                except domainxml.EditError as e:
                    raise _err("backend_error", str(e))
                await self.backend.define(domainxml.to_text(root), str(self.storage.vm_dir(d.uuid)))
                if "autostart" in want:
                    await self.backend.set_autostart(d.uuid, want["autostart"])
            except BaseException:
                if new_disk is not None:
                    try:
                        await asyncio.to_thread(new_disk.unlink)
                    except OSError:
                        pass
                raise
            finally:
                lock.release()
        finally:
            self._host_lock.release()

        # CONFIRM by reading back — the definition the host now holds, not what was sent.
        after = await self.backend.get(d.uuid)
        if after is None:
            raise _err("backend_error", "the VM disappeared after saving")
        hw = domainxml.read_hardware(await self.backend.dumpxml(d.uuid, inactive=True), str(self.storage.iso_dir))
        wrong = []
        if vcpus is not None and after.vcpus != vcpus:
            wrong.append("vCPUs")
        if ram is not None and after.ram_mib != ram:
            wrong.append("memory")
        if "autostart" in want and after.autostart != want["autostart"]:
            wrong.append("autostart")
        if "boot" in want and hw["boot"] != want["boot"]:
            wrong.append("boot order")
        if "input" in want and hw["input"] != want["input"]:
            wrong.append("pointer")
        if "media" in want and hw["media"] != (media["iso"] if isinstance(media, dict) else ""):
            wrong.append("installer disc")
        if wrong:
            raise _err("backend_error", "the host accepted the change but did not keep: " + ", ".join(wrong))
        self._set_index(after)
        logger.info("[vmhost] updated VM %s (%s) by %s: %s", after.name, after.uuid, pk[:12], sorted(want))
        view = self._vm_view(after, role, pk)
        view["hardware"] = hw
        return {"vm": view}

    async def _hardware(self, vm_uuid: str) -> dict:
        try:
            return domainxml.read_hardware(await self.backend.dumpxml(vm_uuid, inactive=True), str(self.storage.iso_dir))
        except Exception as e:
            logger.debug("[vmhost] hardware read failed for %s: %s", vm_uuid, e)
            return {}

    # ------------------------------------------------------------------------------ snapshots
    async def _snap_domain(self, pk, role, args):
        d = await self._domain(pk, role, args)
        if is_migrating(d):
            raise _err("migrating", "this VM is being migrated")
        return d

    @staticmethod
    def _snap_name(args) -> str:
        import re
        n = args.get("name")
        if not isinstance(n, str) or not re.fullmatch(SNAPSHOT_NAME, n):
            raise _err("bad_request", "a snapshot name is letters, digits, . _ - (at most 48)")
        return n

    async def _op_vm_snapshot_list(self, pk, role, args, progress):
        d = await self._snap_domain(pk, role, args)
        return {"vm": d.uuid, "snapshots": await self.backend.snapshot_list(d.uuid)}

    async def _op_vm_snapshot_create(self, pk, role, args, progress):
        name = self._snap_name(args)
        desc = args.get("description") or ""
        if not isinstance(desc, str):
            raise _err("bad_request", "description must be text")
        d = await self._snap_domain(pk, role, args)
        lock = self._vm_lock(d.uuid)
        await self._acquire(lock, "this VM")
        try:
            existing = await self.backend.snapshot_list(d.uuid)
            if any(s["name"] == name for s in existing):
                raise _err("conflict", f"a snapshot named {name} already exists")
            if len(existing) >= MAX_SNAPSHOTS:
                raise _err("insufficient_capacity", f"a VM may keep at most {MAX_SNAPSHOTS} snapshots")
            if progress:
                await progress({"phase": "snapshot", "msg": "taking the snapshot"})
            await self.backend.snapshot_create(d.uuid, name, desc[:200])
            return {"vm": d.uuid, "snapshots": await self.backend.snapshot_list(d.uuid)}
        finally:
            lock.release()

    async def _op_vm_snapshot_revert(self, pk, role, args, progress):
        name = self._snap_name(args)
        if args.get("confirm") is not True:
            raise _err("bad_request", "reverting discards every change since the snapshot — send confirm: true")
        d = await self._snap_domain(pk, role, args)
        lock = self._vm_lock(d.uuid)
        await self._acquire(lock, "this VM")
        try:
            if not any(s["name"] == name for s in await self.backend.snapshot_list(d.uuid)):
                raise _err("not_found", "no such snapshot")
            self.consoles.revoke(d.uuid)
            await self.backend.snapshot_revert(d.uuid, name)
            after = await self.backend.get(d.uuid) or d
            self._set_index(after)
            return {"vm": self._vm_view(after, role, pk), "reverted": name}
        finally:
            lock.release()

    async def _op_vm_snapshot_delete(self, pk, role, args, progress):
        name = self._snap_name(args)
        d = await self._snap_domain(pk, role, args)
        lock = self._vm_lock(d.uuid)
        await self._acquire(lock, "this VM")
        try:
            if not any(s["name"] == name for s in await self.backend.snapshot_list(d.uuid)):
                raise _err("not_found", "no such snapshot")
            await self.backend.snapshot_delete(d.uuid, name)
            return {"vm": d.uuid, "snapshots": await self.backend.snapshot_list(d.uuid)}
        finally:
            lock.release()
