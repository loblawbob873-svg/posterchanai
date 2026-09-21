"""Phase 2 host ops that change a VM's hardware or its disk history: `vm.update` and `vm.snapshot.*`.

A mixin of `VmHostService` (service.py) — kept in its own module so the op table there only grows by
rows. Every op here is ADMIN-only, enforced by that table on the host, never by the client.

vm.update — the machine must be SHUT OFF, and that is checked under the VM lock, after a re-read. Every
field is validated and capacity-checked BEFORE anything is written; then the inactive definition is
edited as a tree (domainxml), defined back once, and read back: a Save that libvirt accepted but that
did not change the fields asked for is reported as an error, not as success. A new disk file is created
first and removed again if the define fails, so a refused Save never leaves an orphan qcow2 behind.

Snapshots are OFFLINE (see the snapshot section below): only while the VM is shut off, `qemu-img snapshot` on
every qcow2 disk plus a copy of the EFI variable store, recorded in `pc:vm`. A revert throws away every change since
the snapshot, so it demands `confirm: true`; it never touches the definition, so the CURRENT assignments stay. A VM
that is being migrated refuses all four ops (`_migration_guard`), because a snapshot taken mid-export is a history the
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
    """True when the VM's metadata carries a migration tag. The ops below do NOT use this alone: they call
    the service's `_migration_guard`, which also consults the migration journal (a migration in
    planned/quiescing/exporting has no tag yet) and ignores a tag whose migration already finished."""
    m = getattr(d, "meta", None)
    return bool(m is not None and getattr(m, "migration", None))


class HardwareOps:
    # ------------------------------------------------------------------------------ vm.update
    async def _op_vm_update(self, pk, role, args, progress):
        from .service import _int_arg
        d = await self._domain(pk, role, args)
        if d.meta is None or not self.storage.is_managed_dir(d.uuid):
            raise _err("unsupported", "this VM was not created by PosterChan — edit it with virsh")
        known = {"vm", "vcpus", "ram_mib", "autostart", "boot", "add_disk_gib", "add_nic", "media", "input", "network"}
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
        chosen = await self._resolve_network(want["network"]) if "network" in want else None
        net_name, net_bridge = chosen if chosen else (self.cfg.default_network, self.cfg.bridge)
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
                self._migration_guard(d)     # journal (authoritative) + pc:migration metadata, like start
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
                    if chosen:
                        domainxml.set_primary_nic(root, net_name, net_bridge, windows)
                    if want.get("add_nic"):
                        if len(root.findall("devices/interface")) >= MAX_NICS:
                            raise _err("insufficient_capacity", f"a VM may have at most {MAX_NICS} network adapters")
                        domainxml.add_nic(root, net_name, net_bridge, windows)
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
        if chosen and hw.get("net") != {"type": "bridge" if net_bridge else "network", "name": net_bridge or net_name}:
            wrong.append("network")
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

    # ------------------------------------------------------------------------------ snapshots (OFFLINE)
    # A snapshot is taken of a SHUT-OFF VM: `qemu-img snapshot -c` on every qcow2 disk plus a copy of the EFI variable
    # store (`snap-<name>.nvram.fd`), and a record in the VM's `pc:vm` metadata saying what it is made of. Revert is
    # `qemu-img snapshot -a` on the same disks plus the variable store copied back. See docs/VM_HOSTING.md §5.
    #
    # Why not libvirt's internal snapshots: measured LIVE, whether libvirt accepts one for an EFI guest depends on the
    # host's firmware descriptors (libvirt 12 with a qcow2 varstore took one of a RUNNING EFI VM; a raw OVMF varstore
    # is refused) — the same button would work on one host and fail on the next, and a migration would have to carry
    # two formats. Offline snapshots behave the same on every host, carry nothing but files, and a revert never
    # touches the domain definition, so assignments, hardware and the migration tag stay CURRENT by construction.
    async def _snap_domain(self, pk, role, args):
        d = await self._domain(pk, role, args)
        self._migration_guard(d)
        return d

    @staticmethod
    def _snap_name(args) -> str:
        import re
        n = args.get("name")
        if not isinstance(n, str) or not re.fullmatch(SNAPSHOT_NAME, n):
            raise _err("bad_request", "a snapshot name is letters, digits, . _ - (at most 48)")
        return n

    async def _snapshot_layout(self, d) -> tuple:
        """([{target, path}] of the VM's disks, nvram path or "") — every path inside the VM's own directory, every
        disk qcow2, the variable store readable by the app. Refuses what an offline snapshot cannot cover."""
        import os
        from .backend import BackendError
        if d.meta is None or not self.storage.is_managed_dir(d.uuid):
            raise _err("unsupported", "this VM was not created by PosterChan — snapshot it with virsh")
        try:
            xml = await self.backend.dumpxml(d.uuid, inactive=True)
            disks = domainxml.file_disks(xml)
            nv_path, _tmpl, _fmt = domainxml.nvram_seed(xml)
        except (domainxml.EditError, BackendError) as e:
            raise _err("backend_error", str(e))
        vm_dir = self.storage.vm_dir(d.uuid)
        out = []
        for dk in disks:
            p = dk["path"]
            if dk["type"] != "file" or not p or os.path.dirname(p) != str(vm_dir):
                raise _err("unsupported", f"disk {dk['target']} is not a file in the VM's own directory")
            if dk["format"] != "qcow2":
                raise _err("unsupported", f"disk {dk['target']} is {dk['format'] or 'not qcow2'} — only qcow2 disks can "
                                          "hold a snapshot")
            out.append({"target": dk["target"], "path": p})
        if not out:
            raise _err("unsupported", "this VM has no disks to snapshot")
        if nv_path:
            if os.path.dirname(nv_path) != str(vm_dir):
                raise _err("unsupported", "the VM's EFI variable store is not in its own directory")
            ok = await asyncio.to_thread(lambda: os.path.isfile(nv_path) and os.access(nv_path, os.R_OK | os.W_OK))
            if not ok and await asyncio.to_thread(os.path.lexists, nv_path):
                raise _err("unsupported", "the VM's EFI variable store is not readable by this app (libvirt created it "
                                          "as the qemu user) — see docs/VM_HOSTING.md, \"EFI variable store\"")
            if not ok:
                nv_path = ""                      # never started, nothing seeded: there is no variable state yet
        return out, nv_path

    async def _disk_tags(self, disks) -> dict:
        tags = {}
        for dk in disks:
            info = await self.backend.img_info(dk["path"])
            tags[dk["target"]] = list(info.get("snapshots") or [])
        return tags

    async def _snapshot_view(self, d, disks=None, tags=None) -> list:
        """The recorded snapshots, each checked against what the disks REALLY hold: `ok`, or `incomplete` (a disk or
        the variable-store copy is missing its half — it cannot be reverted, only deleted). A tag on a disk that no
        record names (a create that died half-way) is listed as `orphan`, so it can be deleted too."""
        import datetime
        import os
        if disks is None:
            try:
                disks, _nv = await self._snapshot_layout(d)
            except Exception:
                disks = []
        if tags is None:
            try:
                tags = await self._disk_tags(disks)
            except Exception as e:
                logger.debug("[vmhost] snapshot cross-check failed for %s: %s", d.uuid, e)
                tags = {}
        current = sorted(dk["target"] for dk in disks)
        out, named = [], set()
        for sn in (d.meta.snapshots if d.meta else []):
            named.add(sn["name"])
            state = "ok"
            if sorted(sn["disks"]) != current:
                state = "disks_changed"
            elif any(tags.get(t, []).count(sn["name"]) != 1 for t in sn["disks"]):
                state = "incomplete"
            elif sn["nvram"] and not await asyncio.to_thread(
                    os.path.isfile, self.storage.snapshot_nvram_path(d.uuid, sn["name"])):
                state = "incomplete"
            when = datetime.datetime.fromtimestamp(sn["created"], datetime.timezone.utc).strftime("%Y-%m-%d %H:%M UTC") \
                if sn["created"] else ""
            out.append({"name": sn["name"], "created": when, "created_at": sn["created"],
                        "description": sn["description"], "disks": list(sn["disks"]), "state": state})
        for t in sorted({x for v in tags.values() for x in v} - named):
            out.append({"name": t, "created": "", "created_at": 0, "description": "", "disks": sorted(
                k for k, v in tags.items() if t in v), "state": "orphan"})
        return out

    async def _op_vm_snapshot_list(self, pk, role, args, progress):
        d = await self._snap_domain(pk, role, args)
        return {"vm": d.uuid, "offline": True, "snapshots": await self._snapshot_view(d)}

    async def _snapshot_capacity(self) -> None:
        st = await self.backend.host_stats(str(self.storage.root))
        room = int(st.get("disk_free_gib") or 0) - self.cfg.reserve_disk_gib
        if room < 1:
            raise _err("insufficient_capacity", f"a snapshot needs about 1 GiB free; this host has {max(0, room)} GiB "
                                                "above its reserve")

    async def _locked_off(self, pk, role, args, verb: str):
        """(domain, lock) with the VM re-read under its lock, migration-guarded and SHUT OFF."""
        d = await self._snap_domain(pk, role, args)
        lock = self._vm_lock(d.uuid)
        await self._acquire(lock, "this VM")
        try:
            d = await self.backend.get(d.uuid)
            if d is None or not self._visible(d, role, pk):
                raise _err("not_found", "no such VM")
            self._migration_guard(d)
            if d.state != "shutoff":
                raise _err("conflict", f"shut the VM down to {verb} a snapshot — snapshots are taken of a stopped VM")
        except BaseException:
            lock.release()
            raise
        return d, lock

    async def _op_vm_snapshot_create(self, pk, role, args, progress):
        import os
        import time as _t
        name = self._snap_name(args)
        desc = args.get("description") or ""
        if not isinstance(desc, str):
            raise _err("bad_request", "description must be text")
        d, lock = await self._locked_off(pk, role, args, "take")
        made, nv_copy = [], None
        try:
            disks, nv_path = await self._snapshot_layout(d)
            if any(s["name"] == name for s in d.meta.snapshots):
                raise _err("conflict", f"a snapshot named {name} already exists")
            if len(d.meta.snapshots) >= MAX_SNAPSHOTS:
                raise _err("insufficient_capacity", f"a VM may keep at most {MAX_SNAPSHOTS} snapshots")
            tags = await self._disk_tags(disks)
            if any(name in v for v in tags.values()):
                # qemu-img happily makes a SECOND snapshot with the same tag; revert would then pick the older one.
                raise _err("conflict", f"leftover snapshot data named {name} is on this VM's disks — delete it first")
            await self._snapshot_capacity()
            if progress:
                await progress({"phase": "snapshot", "msg": "taking the snapshot"})
            nv_copy = self.storage.snapshot_nvram_path(d.uuid, name) if nv_path else None
            try:
                for dk in disks:
                    await self.backend.img_snapshot_create(dk["path"], name)
                    made.append(dk)
                if nv_copy is not None:
                    await asyncio.to_thread(_copy_private, nv_path, nv_copy)
                meta = d.meta
                meta.snapshots = list(meta.snapshots) + [domainxml.clean_snapshot_record({
                    "name": name, "created": int(_t.time()), "description": desc[:200],
                    "disks": [dk["target"] for dk in disks], "nvram": nv_copy is not None})]
                await self.backend.set_metadata(d.uuid, meta, live=False)
            except BaseException:
                # Leave nothing half-made: a tag with no record would block the name and be reverted to by nobody.
                for dk in made:
                    try:
                        await self.backend.img_snapshot_delete(dk["path"], name)
                    except Exception as e:
                        logger.warning("[vmhost] rollback of snapshot %s on %s failed: %s", name, dk["target"], e)
                if nv_copy is not None:
                    await asyncio.to_thread(lambda: os.path.lexists(nv_copy) and os.unlink(nv_copy))
                raise
            after = await self.backend.get(d.uuid) or d
            return {"vm": d.uuid, "offline": True, "snapshots": await self._snapshot_view(after, disks)}
        finally:
            lock.release()

    async def _op_vm_snapshot_revert(self, pk, role, args, progress):
        import os
        name = self._snap_name(args)
        if args.get("confirm") is not True:
            raise _err("bad_request", "reverting discards every change since the snapshot — send confirm: true")
        d, lock = await self._locked_off(pk, role, args, "revert to")
        try:
            disks, nv_path = await self._snapshot_layout(d)
            view = {s["name"]: s for s in await self._snapshot_view(d, disks)}
            sn = view.get(name)
            if sn is None or sn["state"] == "orphan":
                raise _err("not_found", "no such snapshot")
            if sn["state"] == "disks_changed":
                raise _err("conflict", "this VM's disks changed since the snapshot (a disk was added) — it cannot be "
                                       "reverted, only deleted")
            if sn["state"] != "ok":
                raise _err("conflict", "this snapshot is incomplete on disk and cannot be reverted — delete it")
            record = next(s for s in d.meta.snapshots if s["name"] == name)
            current = d.meta                               # WHO may use it and what it is, NOW
            self.consoles.revoke(d.uuid)
            if progress:
                await progress({"phase": "snapshot", "msg": "reverting"})
            for dk in disks:
                await self.backend.img_snapshot_apply(dk["path"], name)
            if record["nvram"]:
                target = nv_path or str(self.storage.nvram_path(d.uuid))
                await asyncio.to_thread(_copy_private, str(self.storage.snapshot_nvram_path(d.uuid, name)), target)
            elif nv_path:
                # Taken before the first start: there were no variables then, so there are none now — the next start
                # seeds a fresh store from the firmware template, exactly as it did the first time.
                await asyncio.to_thread(os.unlink, nv_path)
                await self._ensure_nvram(d.uuid)
            after = await self.backend.get(d.uuid) or d
            if after.meta is None or after.meta.to_xml(prefixed=False) != current.to_xml(prefixed=False):
                # An offline revert never touches the definition — but if anything did, the CURRENT access wins.
                await self.backend.set_metadata(d.uuid, current, live=False)
                after = await self.backend.get(d.uuid) or after
            self._set_index(after)
            return {"vm": self._vm_view(after, role, pk), "reverted": name}
        finally:
            lock.release()

    async def _op_vm_snapshot_delete(self, pk, role, args, progress):
        import os
        name = self._snap_name(args)
        d, lock = await self._locked_off(pk, role, args, "delete")
        try:
            disks, _nv = await self._snapshot_layout(d)
            tags = await self._disk_tags(disks)
            recorded = any(s["name"] == name for s in d.meta.snapshots)
            on_disk = [dk for dk in disks if name in tags.get(dk["target"], [])]
            if not recorded and not on_disk:
                raise _err("not_found", "no such snapshot")
            for dk in on_disk:
                for _ in range(tags[dk["target"]].count(name)):          # every copy of a repeated tag
                    await self.backend.img_snapshot_delete(dk["path"], name)
            copy = self.storage.snapshot_nvram_path(d.uuid, name)
            await asyncio.to_thread(lambda: os.path.lexists(copy) and os.unlink(copy))
            if recorded:
                meta = d.meta
                meta.snapshots = [s for s in meta.snapshots if s["name"] != name]
                await self.backend.set_metadata(d.uuid, meta, live=False)
            after = await self.backend.get(d.uuid) or d
            return {"vm": d.uuid, "offline": True, "snapshots": await self._snapshot_view(after, disks)}
        finally:
            lock.release()


def _copy_private(src: str, dst: str) -> None:
    """Copy a small file (a variable store) to `dst` atomically, 0600 — a reader never sees half of one."""
    import os
    import shutil
    dst = str(dst)
    tmp = dst + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as out, open(src, "rb") as inp:
        shutil.copyfileobj(inp, out, 1 << 20)
        out.flush()
        os.fsync(out.fileno())
    os.chmod(tmp, 0o600)
    os.replace(tmp, dst)
